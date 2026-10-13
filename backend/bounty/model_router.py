"""Cheapest-first routing under an app-enforced single-run spend cap. No paid retries."""
import hashlib
import json
from fastapi import HTTPException
from .routing_policy import get_policy, ordered_models, role_group
from .budget import RunBudget


class ModelRouter:
    def __init__(self, store):
        self.store = store

    async def authorize(self, role, job_id, messages=None, failure_reason=None, max_tokens=1024):
        controls = await self.store.controls()
        policy = await get_policy(self.store)
        config = policy['policy']
        catalog = await self.store.one('integration_connections', {'id': 'experiential'}) or {}
        ids = catalog.get('model_ids', [])
        estimates = catalog.get('pricing_estimates', {})
        task_key = f'{job_id}:{role}'
        previous = await self.store.rows('model_usage', {'task_key': task_key}, limit=3) if job_id else []
        prefs = ordered_models(config[role_group(role)], ids)

        def rate(m):
            e = estimates.get(m, {})
            return (e.get('input_nano_usd_per_million') or 0) + (e.get('output_nano_usd_per_million') or 0)

        used = {p['model'] for p in previous}
        candidates = [m for m in sorted(prefs, key=lambda m: (rate(m), prefs.index(m))) if m not in used]
        fingerprint = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest() if messages else None
        budget = RunBudget(self.store)
        prompt_chars = sum(len(m.get('content', '')) for m in messages) if messages else 0
        reservation = budget.worst_case(prompt_chars, max_tokens)
        st = await budget.state()
        result, model = 'NO_SUITABLE_MODEL', None
        if any(controls.get(k) for k in ['global_stop', 'agents_stopped', 'spending_stopped', 'integrations_revoked']):
            result = 'STOPPED_BY_POLICY'
        elif not st.get('authorized'):
            result = 'RUN_NOT_AUTHORIZED'
        elif not catalog.get('catalog_verified_at') or not ids:
            result = 'CATALOG_UNVERIFIED'
        elif any(p.get('task_result') in {'security_or_policy_failure', 'repeated_output'} for p in previous):
            result = 'FATAL_TASK_FAILURE'
        elif previous:
            result = 'NO_PAID_RETRY'
        elif not candidates:
            result = 'NO_SUITABLE_MODEL'
        elif reservation > (st['cap_nano_usd'] - st['spent_nano_usd']):
            result = 'BUDGET_CAP_WOULD_EXCEED'
        else:
            result, model = 'SELECTED', candidates[0]
        decision = await self.store.insert('routing_decisions', {'role': role, 'job_id': job_id, 'task_key': task_key,
            'model': model, 'estimated_cost_nano_usd': reservation if model else None, 'reservation_nano_usd': reservation,
            'model_price_nano_usd_per_million': rate(model) if model else None,
            'prompt_tokens': None, 'completion_tokens': None, 'escalation_reason': failure_reason,
            'paid_execution': 'CAPPED_AUTHORIZED', 'cap_nano_usd': st.get('cap_nano_usd'), 'spent_nano_usd': st.get('spent_nano_usd'),
            'result': result, 'attempt_number': len(previous) + 1, 'approach_hash': fingerprint,
            'policy_version': policy['updated_at'] or 'cheapest-first-v1', 'catalog_verified_at': catalog.get('catalog_verified_at')})
        await self.store.audit('finance', 'Cheapest-first routing decision',
            f'{role}: {result}; model={model or "none"}; reservation={reservation} nano-USD; spent={st.get("spent_nano_usd")}/{st.get("cap_nano_usd")}', result, job_id)
        if not model:
            raise HTTPException(409, f'{result}: no inference sent; cumulative cap ${int(st.get("cap_nano_usd", 0)) / 1e9:.2f}, spent ${int(st.get("spent_nano_usd", 0)) / 1e9:.6f}')
        return decision

    async def preflight(self, role, estimated_cost, job_id):
        return await self.authorize(role, job_id)

    async def report_failure(self, role, job_id, reason):
        rows = await self.store.rows('model_usage', {'task_key': f'{job_id}:{role}'}, limit=1)
        if not rows:
            return
        usage = rows[0]
        await self.store.db.model_usage.update_one({'id': usage['id']}, {'$set': {'task_result': reason}})
        if usage.get('routing_decision_id'):
            await self.store.db.routing_decisions.update_one({'id': usage['routing_decision_id']}, {'$set': {'result': reason.upper(), 'escalation_reason': reason}})
        await self.store.audit(role, 'Model task outcome', f'{usage["model"]}: {reason}; no automatic retry', 'BLOCKED', job_id)
