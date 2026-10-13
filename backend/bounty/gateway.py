"""Experiential-only HTTP gateway. No fallback, retries, or fabricated charges."""
import asyncio
import os
import time
import uuid
import hashlib
from datetime import datetime, timezone
import httpx
from fastapi import HTTPException
from .secrets import experiential_key
from .store import now


CAP_REASON = 'PROVIDER_CAP_UNVERIFIED: provider-enforced $1 ceiling and single-route billing cannot be established; no inference sent'

FLAGSHIP_CODING = ('opus', 'gpt-5.5', 'gpt-5.4', 'gpt-5.2', 'gpt-5.3', 'codex-max', 'sonnet', 'gemini-3-pro', 'gemini-3.1-pro', 'gpt-5.6')


def request_timeout(role, model=None):
    """Per-tier provider request timeout. Capable coding models are no longer killed by a single 30s limit.
    Financially independent of RunBudget; a longer wait never increases spend."""
    role = (role or '').lower()
    model = (model or '').lower()
    if role in ('coding', 'coder', 'implementation'):
        if any(tag in model for tag in FLAGSHIP_CODING):
            return int(os.environ.get('EXPLABS_TIMEOUT_FLAGSHIP_SECONDS', 120))
        return int(os.environ.get('EXPLABS_TIMEOUT_CODING_SECONDS', 90))
    return int(os.environ.get('EXPLABS_TIMEOUT_ROUTINE_SECONDS', 45))


def confirmed_nano_cost(data):
    # Only explicitly denominated authoritative fields; never infer dollars from usage.cost.
    usage = data.get('usage') or {}
    for source in (usage, data):
        for field, multiplier in [('cost_nano_usd', 1), ('cost_micro_usd', 1000)]:
            value = source.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value * multiplier
    # Experiential returns usage.cost as a float in USD; convert to nano-USD.
    cost = usage.get('cost')
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return round(float(cost) * 1_000_000_000)
    return None


class ExperientialGateway:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()

    async def policy(self):
        c = await self.store.controls()
        if any(c.get(x) for x in ['global_stop', 'integrations_revoked']):
            raise HTTPException(409, 'Model connection stopped by emergency policy')

    async def catalog(self, actor='system'):
        async with self.lock:
            await self.policy()
            key = experiential_key()
            if not key:
                raise HTTPException(409, 'EXPLABS_KEY_REQUIRED')
            try:
                async with httpx.AsyncClient(timeout=int(os.environ['EXPLABS_REQUEST_TIMEOUT_SECONDS']), follow_redirects=False) as client:
                    response = await client.get(os.environ['EXPLABS_BASE_URL'] + '/models', headers={'Authorization': f'Bearer {key}'})
                    if response.status_code != 200:
                        raise HTTPException(502, f'Experiential catalog HTTP {response.status_code}; inference not verified')
                    models = response.json().get('data', [])
                    ids = sorted({m['id'] for m in models if isinstance(m.get('id'), str)})
                    if not ids:
                        raise HTTPException(502, 'Experiential returned an empty catalog')
                    # Public pricing is estimated metadata, NOT an authorization or billing receipt.
                    estimates, eligibility = {}, {}
                    from .routing_policy import inspect_free_metadata
                    for offset in range(0, 2000, 400):
                        try:
                            r = await client.get(os.environ['EXPLABS_MANAGEMENT_URL'] + '/models', params={'limit': 400, 'offset': offset}, headers={'Authorization': f'Bearer {key}'})
                            payload = r.json()
                        except (httpx.RequestError, ValueError):
                            break
                        if r.status_code != 200:
                            break
                        for item in payload.get('models', []):
                            slug = item.get('model', {}).get('slug')
                            if slug in ids:
                                eligibility[slug] = inspect_free_metadata(item)
                            rates = [p for p in item.get('providers', []) if p.get('billing_source') == 'host_managed' and isinstance(p.get('input_nano_usd_per_million'), int) and isinstance(p.get('output_nano_usd_per_million'), int)]
                            if slug in ids and rates:
                                p = min(rates, key=lambda p: p['input_nano_usd_per_million'] + p['output_nano_usd_per_million'])
                                estimates[slug] = {'input_nano_usd_per_million': p['input_nano_usd_per_million'], 'output_nano_usd_per_million': p['output_nano_usd_per_million'], 'classification': 'ESTIMATED', 'pricing_source': p.get('pricing_source')}
                        if offset + 400 >= payload.get('total', 0):
                            break
            except (httpx.RequestError, ValueError, KeyError):
                await self._error(actor, 'Catalog unavailable or malformed')
                raise HTTPException(502, 'Experiential catalog unavailable; no inference sent')
            except HTTPException as exc:
                await self._error(actor, exc.detail)
                raise
            old = await self.store.one('integration_connections', {'id': 'experiential'}) or {}
            value = {'id': 'experiential', 'provider': 'Experiential Labs', 'status': 'CATALOG_CONNECTED',
                     'model_ids': ids, 'model_count': len(ids), 'pricing_estimates': estimates, 'free_eligibility': eligibility,
                     'catalog_verified_at': now(), 'inference_verified_at': old.get('inference_verified_at'),
                     'cap_verified': False, 'cap_reason': CAP_REASON}
            await self.store.db.integration_connections.update_one({'id': 'experiential'}, {'$set': value}, upsert=True)
            await self.store.audit('researcher', 'Experiential catalog verified', f'{len(ids)} authenticated model IDs; catalog only, inference not implied', actor=actor)
            return value

    async def _error(self, actor, reason):
        await self.store.db.integration_connections.update_one({'id': 'experiential'}, {'$set': {'status': 'PROVIDER_ERROR', 'last_error': reason, 'last_checked_at': now()}}, upsert=True)
        await self.store.audit('researcher', 'Experiential catalog failed', reason, 'BLOCKED', actor=actor)

    async def diagnostic(self, actor):
        await self.policy()
        used = await self.store.one('model_usage', {'id': 'experiential-diagnostic-once'})
        if used:
            raise HTTPException(409, 'DIAGNOSTIC_ALREADY_ATTEMPTED: no paid retries permitted')
        await self.store.audit('finance', 'Diagnostic inference blocked', CAP_REASON, 'BLOCKED', actor=actor)
        # Public provider contracts do not establish the required advance guarantee.
        # Neither a browser checkbox nor local token estimate can bypass this gate.
        raise HTTPException(409, CAP_REASON)

    async def invoke(self, role, messages, job_id, max_tokens=1024, failure_reason=None):
        from .model_router import ModelRouter
        if not job_id:
            raise HTTPException(409, 'A persistent task ID is required for bounded model attempts')
        decision = await ModelRouter(self.store).authorize(role, job_id, messages, failure_reason, max_tokens)
        content = await self._dispatch(decision['model'], messages, job_id, role, max_tokens, decision)
        if content is None:
            raise HTTPException(502, f'Model {decision["model"]} returned empty/null content; no retry')
        previous = await self.store.rows('model_usage', {'task_key': decision['task_key']}, limit=3)
        digest = hashlib.sha256(content.encode()).hexdigest()
        if any(p.get('attempt_number', 0) < decision['attempt_number'] and p.get('output_hash') == digest for p in previous):
            await ModelRouter(self.store).report_failure(role, job_id, 'repeated_output')
            raise HTTPException(409, 'REPEATED_OUTPUT_BLOCKED: no further retry')
        return content

    async def _dispatch(self, model, messages, job_id, role, max_tokens, decision=None):
        """Internal transport. Every request is bounded by the app-enforced single-run spend cap."""
        catalog = await self.store.one('integration_connections', {'id': 'experiential'}) or {}
        if (not decision or decision.get('model') != model or decision.get('job_id') != job_id
                or decision.get('role') != role or decision.get('result') != 'SELECTED'
                or model not in catalog.get('model_ids', [])
                or decision.get('catalog_verified_at') != catalog.get('catalog_verified_at')):
            raise HTTPException(409, 'ROUTING_DECISION_INVALID: dispatch requires a fresh authorized routing decision')
        await self.policy()
        controls = await self.store.controls()
        if any(controls.get(k) for k in ['spending_stopped', 'agents_stopped']):
            raise HTTPException(409, 'Model spending stopped')
        from .budget import RunBudget
        budget = RunBudget(self.store)
        reservation = int(decision.get('reservation_nano_usd') or 0)
        await budget.reserve(reservation)  # atomic pre-charge; race-safe hard cap
        request_id = str(uuid.uuid4())
        routing = {'id': 'attempt-' + hashlib.sha256(f"{decision['task_key']}:{decision['attempt_number']}".encode()).hexdigest(),
            'task_key': decision['task_key'], 'attempt_number': decision['attempt_number'], 'approach_hash': decision['approach_hash'],
            'routing_decision_id': decision['id'], 'escalation_reason': decision['escalation_reason'], 'policy_version': decision['policy_version']}
        from pymongo.errors import DuplicateKeyError
        try:
            usage = await self.store.insert('model_usage', {**routing, 'provider': 'Experiential Labs', 'model': model, 'job_id': job_id,
            'role': role, 'request_id': request_id, 'status': 'DISPATCHED_ONCE', 'prompt_tokens': None,
            'completion_tokens': None, 'actual_cost_nano_usd': None, 'cost_status': 'PENDING', 'estimated_cost_nano_usd': reservation})
        except DuplicateKeyError:
            await self.store.db.routing_decisions.update_one({'id': decision['id']}, {'$set': {'result': 'CONCURRENT_ATTEMPT_BLOCKED'}})
            raise HTTPException(409, 'Concurrent or duplicate model attempt blocked; no request sent')
        started = time.monotonic()
        posted = False
        try:
            async with httpx.AsyncClient(timeout=request_timeout(role, model), follow_redirects=False) as client:
                posted = True
                response = await client.post(os.environ['EXPLABS_BASE_URL'] + '/chat/completions',
                    headers={'Authorization': f'Bearer {experiential_key()}'},
                    json={'model': model, 'messages': messages, 'max_tokens': max_tokens, 'stream': False})
            data = response.json()
            if response.status_code != 200:
                raise HTTPException(502, f'Experiential HTTP {response.status_code}; no retry, charge pending')
            content = data['choices'][0]['message']['content']
            if not content:
                # Some models return null content with tool calls or empty responses
                content = data['choices'][0]['message'].get('reasoning_content') or data['choices'][0].get('text') or ''
                if not content:
                    raise HTTPException(502, f'Model {model} returned empty content; no retry')
            tokens = data.get('usage') or {}
            cost = confirmed_nano_cost(data)
            committed = cost if cost is not None else reservation
            await budget.reconcile(reservation, cost)
            result = {'status': 'SUCCEEDED', 'prompt_tokens': tokens.get('prompt_tokens'), 'completion_tokens': tokens.get('completion_tokens'),
                'actual_cost_nano_usd': cost, 'committed_nano_usd': committed,
                'provider_usage': {k: tokens.get(k) for k in ['cost', 'is_byok', 'total_tokens']},
                'cost_status': 'PROVIDER_CONFIRMED' if cost is not None else 'RESERVED_CHARGED',
                'latency_ms': round((time.monotonic() - started) * 1000), 'finished_at': now()}
            await self.store.db.model_usage.update_one({'id': usage['id']}, {'$set': {**result, 'output_hash': hashlib.sha256(content.encode()).hexdigest()}})
            await self.store.db.routing_decisions.update_one({'id': decision['id']}, {'$set': {**result, 'result': 'SUCCEEDED'}})
            await self.store.db.integration_connections.update_one({'id': 'experiential'}, {'$set': {'inference_verified_at': now()}})
            state = await budget.state()
            await self.store.audit(role, 'Model inference completed', f'{model}; cost {result["cost_status"]}; spent {state.get("spent_nano_usd")}/{state.get("cap_nano_usd")} nano-USD; request {request_id}', job_id=job_id, cost=committed / 1000000000)
            return content
        except (httpx.RequestError, ValueError, KeyError, IndexError, HTTPException):
            # Reservation was pre-charged atomically; on ambiguous failure it stays charged (conservative). No double charge, no retry.
            await self.store.db.model_usage.update_one({'id': usage['id']}, {'$set': {'status': 'FAILED_OR_AMBIGUOUS',
                'cost_status': 'RESERVED_ON_FAILURE' if posted else 'RESERVED_UNUSED', 'committed_nano_usd': reservation,
                'latency_ms': round((time.monotonic() - started) * 1000)}})
            if not posted:
                await budget.reconcile(reservation, 0)  # request never sent; release the reservation
            await self.store.db.routing_decisions.update_one({'id': decision['id']}, {'$set': {'result': 'FAILED_OR_AMBIGUOUS'}})
            await self.store.audit(role, 'Model request stopped', 'No retry; cap-conservative charge recorded', 'BLOCKED', job_id)
            raise HTTPException(502, 'Model request failed or ambiguous; no retry permitted')