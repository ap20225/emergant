"""Autonomy infrastructure AROUND the verified worker. Deterministic, fail-closed.
Discovery/qualification/scheduling/briefing NEVER authorize spending or execution."""
import os
import re
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from .store import now, uid

FLAG_DEFAULTS = {
    'DISCOVERY_ENABLED': True,
    'QUALIFICATION_ENABLED': True,
    'NIGHTLY_BRIEFING_ENABLED': True,
    'AUTO_ESCALATION_ENABLED': False,
    'AUTONOMOUS_EXECUTION_ENABLED': False,
    'SCHEDULER_BACKGROUND_ENABLED': False,
}

# Escalation ceilings (bounded even when the flag is later enabled).
ESCALATION_LIMITS = {
    'max_escalations_per_job': 2,
    'max_total_model_calls_per_job': 8,
    'max_cost_per_job_nano_usd': 500_000_000,  # $0.50 hard per-job spend cap
}
ESCALATION_REASONS = {
    'bounded_coding_failed', 'tests_failed_after_implementation',
    'diagnosis_requires_stronger_reasoning', 'unresolved_ambiguity',
    'security_review_uncertainty', 'code_review_uncertainty',
}

# Deterministic unsafe/injection screening for untrusted opportunity text.
UNSAFE_TEXT = re.compile(
    r'(?i)(ignore (all|your|previous|the) (instructions|rules|policy))'
    r'|(reveal|exfiltrate|leak|print|expose).{0,25}(token|secret|api[_-]?key|credential|password|env)'
    r'|(disable|bypass|turn off|override).{0,25}(budget|security|approval|sandbox|e2b|policy)'
    r'|(publish|merge).{0,20}(immediately|without.{0,20}approval)'
    r'|rm\s+-rf|curl\s+.*\$\{?[A-Z_]+|wallet|private key|seed phrase|BEGIN [A-Z ]*PRIVATE KEY')
CREDENTIAL_REQUIRED = re.compile(r'(?i)(ssh key|api key required|provide.{0,15}(token|password|secret)|\.env file|service account)')


def _env_flag_defaults():
    """Production 24/7 config: AUTONOMOUS_EXECUTION_ENABLED / SCHEDULER_BACKGROUND_ENABLED can be
    armed via deployment environment so they survive restarts without a manual UI toggle. An owner
    toggle (stored in DB) and the emergency stop still override these at runtime."""
    d = dict(FLAG_DEFAULTS)
    for k in ('AUTONOMOUS_EXECUTION_ENABLED', 'SCHEDULER_BACKGROUND_ENABLED'):
        if os.environ.get(k, '').strip().lower() == 'true':
            d[k] = True
    return d


async def get_flags(store):
    row = await store.one('system_settings', {'id': 'feature-flags'})
    return {**_env_flag_defaults(), **((row or {}).get('config') or {})}


async def set_flag(store, name, value, actor):
    if name not in FLAG_DEFAULTS:
        raise HTTPException(404, f'Unknown feature flag: {name}')
    if not isinstance(value, bool):
        raise HTTPException(422, 'Flag value must be boolean')
    flags = await get_flags(store)
    flags[name] = value
    await store.db.system_settings.update_one({'id': 'feature-flags'}, {'$set': {'config': flags}}, upsert=True)
    await store.audit('master', 'Feature flag changed', f'{name}={value}; backend-authoritative; unattended execution stays opt-in', actor=actor)
    return flags


class Discovery:
    """Creates opportunity records only. No spend or execution authority whatsoever."""

    def __init__(self, store):
        self.store = store

    def duplicate_key(self, source, repository, issue_number, source_identifier, source_url=None):
        return canonical_duplicate_key(source, repository, issue_number, source_identifier, source_url)

    async def run_source(self, adapter, config=None, actor='scheduler'):
        """Fetch + ingest one source with isolated health tracking. One bad adapter never fails the scheduler."""
        source = adapter.source_name
        try:
            records = await adapter.fetch(config or {})
        except Exception as exc:
            await SourceHealth(self.store).record(source, ok=False, error=exc)
            await self.store.audit('scout', 'Discovery source failed', f'{source}: {str(exc)[:120]}; isolated, scheduler continues', 'BLOCKED', actor=actor)
            return {'source': source, 'ok': False, 'created': 0, 'duplicates': 0, 'error': str(exc)[:200]}
        result = await self.ingest(source, records, actor)
        await SourceHealth(self.store).record(source, ok=True, discovered=result['created'], duplicates=result['duplicates'])
        return {'source': source, 'ok': True, **result}

    async def ingest(self, source, records, actor='scheduler'):
        if not (await get_flags(self.store))['DISCOVERY_ENABLED']:
            raise HTTPException(409, 'DISCOVERY_DISABLED')
        created, duplicates = 0, 0
        from .rewards import extract_reward
        from .repos import RepoRegistry
        registry = RepoRegistry(self.store)
        for r in records:
            dk = self.duplicate_key(source, r.get('repository'), r.get('issue_number'), r.get('source_identifier'), r.get('source_url'))
            from .classification import canonical_identity
            canonical = canonical_identity(r.get('repository'), r.get('issue_number'))
            # Cross-source dedup: the same underlying repo#issue resolves to ONE record (evidence preserved on first).
            if canonical and await self.store.db.bounty_opportunities.count_documents({'canonical_key': canonical}) > 0:
                duplicates += 1
                continue
            src_meta = {'currency': r.get('currency'), 'expires_at': r.get('deadline')}
            if r.get('authoritative_reward') and r.get('reward') not in (None, ''):
                src_meta['bounty_amount'] = r.get('reward')
            rw = extract_reward(r.get('title') or '', r.get('description') or '', labels=r.get('labels'),
                                source_meta=src_meta, deadline=r.get('deadline'))
            doc = {'id': uid('bopp'), 'created_at': now(), 'status': 'DISCOVERED', 'source': source,
                   'source_identifier': r.get('source_identifier'), 'source_url': r.get('source_url'),
                   'repository': r.get('repository'), 'issue_number': r.get('issue_number'),
                   'title': (r.get('title') or '')[:200], 'description': (r.get('description') or '')[:2000],
                   'labels': r.get('labels') or [],
                   'reward': rw['reward_amount'], 'currency': rw['reward_currency'],
                   'reward_amount': rw['reward_amount'], 'reward_currency': rw['reward_currency'],
                   'authoritative_reward_amount': (r.get('reward') if (r.get('payout_source_type') == 'platform_api' and r.get('reward') not in (None, '')) else None),
                   'authoritative_reward_currency': r.get('currency'),
                   'reward_source': rw['reward_source'], 'reward_confidence': rw['reward_confidence'],
                   'reward_verified': rw['reward_verified'], 'reward_status': rw['reward_status'],
                   'reward_verified_at': rw['reward_verified_at'],
                   'reward_verification_status': ('VERIFIED' if rw['reward_verified']
                                                  else ('UNKNOWN' if rw['reward_status'] == 'UNKNOWN' else 'UNVERIFIED')),
                   'payout_mechanism': r.get('payout_mechanism'), 'payout_authority': r.get('payout_authority'),
                   'payout_conditions': r.get('payout_conditions'), 'payout_source_url': r.get('payout_source_url'),
                   'payout_source_type': r.get('payout_source_type'), 'payout_certainty': r.get('payout_certainty') or 'UNKNOWN',
                   'execution_authority': r.get('execution_authority') or 'UNKNOWN',
                   'reward_expires_at': r.get('deadline'), 'reward_last_checked_at': now(),
                   'issue_created_at': r.get('created_at'), 'reward_created_at': r.get('reward_created_at'),
                   'last_reward_change_at': r.get('reward_created_at'),
                   'solver_count': r.get('solver_count'), 'competing_pr_count': r.get('competing_pr_count'),
                   'attempted': False, 'claimed': False,
                   'requirements': r.get('requirements'), 'deadline': r.get('deadline'),
                   'discovered_at': now(), 'last_checked_at': now(),
                   'qualification_status': 'PENDING', 'qualification_reason': None,
                   'qualification_model': None, 'qualification_cost': 0,
                   'duplicate_key': dk, 'canonical_key': canonical, 'risk_status': 'UNSCREENED', 'authorization_state': 'NONE'}
            try:
                await self.store.db.bounty_opportunities.insert_one(dict(doc))
                created += 1
                if r.get('repository'):
                    await registry.register_discovered(r.get('repository'), actor=actor)
            except DuplicateKeyError:
                duplicates += 1
        await self.store.audit('scout', 'Discovery ingest', f'{source}: {created} new, {duplicates} duplicate; no spend/execution authority', actor=actor)
        return {'created': created, 'duplicates': duplicates}


class Qualification:
    """Deterministic-first qualification. Never authorizes execution; never sets RUNNING."""

    def __init__(self, store):
        self.store = store

    async def qualify(self, opportunity_id, actor='scheduler'):
        if not (await get_flags(self.store))['QUALIFICATION_ENABLED']:
            raise HTTPException(409, 'QUALIFICATION_DISABLED')
        o = await self.store.one('bounty_opportunities', {'id': opportunity_id})
        if not o:
            raise HTTPException(404, 'Opportunity not found')
        blob = f"{o.get('title', '')} {o.get('description', '')} {o.get('requirements', '')}"
        reasons = []
        if not o.get('repository') or o.get('issue_number') in (None, ''):
            reasons.append('missing repository or issue reference')
        if UNSAFE_TEXT.search(blob):
            reasons.append('unsafe or prompt-injection content detected in untrusted opportunity text')
        if CREDENTIAL_REQUIRED.search(blob):
            reasons.append('task appears to require credentials/secrets (out of policy)')
        risk = 'FLAGGED' if reasons else 'CLEARED'
        status = 'REJECTED' if reasons else 'QUALIFIED'
        new_state = 'REJECTED' if reasons else 'AWAITING_AUTHORIZATION'
        update = {'status': new_state, 'qualification_status': status,
                  'qualification_reason': '; '.join(reasons) or 'Deterministic checks passed',
                  'qualification_model': None, 'qualification_cost': 0,
                  'risk_status': risk, 'last_checked_at': now(),
                  'authorization_state': 'NONE'}  # qualification NEVER grants authorization
        await self.store.db.bounty_opportunities.update_one({'id': opportunity_id}, {'$set': update})
        await self.store.audit('evaluator', 'Opportunity qualified', f'{opportunity_id}: {status} ({update["qualification_reason"]}); no execution authorized', status, actor=actor)
        return {'id': opportunity_id, **update}


class Scheduler:
    """Restart-safe, lease-protected. Discovers and qualifies only; stops at AWAITING_AUTHORIZATION."""

    def __init__(self, store):
        self.store = store

    async def run_once(self, adapters=None, source_adapters=None, config=None, actor='scheduler'):
        await self.store.db.system_settings.update_one({'id': 'scheduler-state'}, {'$setOnInsert': {'id': 'scheduler-state', 'config': {}}}, upsert=True)
        horizon = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        lease = await self.store.db.system_settings.find_one_and_update(
            {'id': 'scheduler-state', '$or': [{'config.lease_until': {'$exists': False}}, {'config.lease_until': {'$lt': now()}}]},
            {'$set': {'config.lease_until': horizon, 'config.holder': actor, 'config.last_started_at': now()}},
            return_document=ReturnDocument.AFTER)
        if not lease:
            return {'skipped': 'LEASE_HELD', 'discovered': 0, 'qualified': 0, 'rejected': 0}
        result = {'discovered': 0, 'qualified': 0, 'rejected': 0, 'duplicates': 0}
        try:
            flags = await get_flags(self.store)
            if flags['DISCOVERY_ENABLED']:
                for source, records in (adapters or []):
                    r = await Discovery(self.store).ingest(source, records, actor)
                    result['discovered'] += r['created']
                    result['duplicates'] += r['duplicates']
                for adapter in (source_adapters or []):  # live adapters, isolated failures
                    r = await Discovery(self.store).run_source(adapter, config or {}, actor)
                    result['discovered'] += r.get('created', 0)
                    result['duplicates'] += r.get('duplicates', 0)
                if flags['QUALIFICATION_ENABLED']:
                    pending = await self.store.rows('bounty_opportunities', {'qualification_status': 'PENDING'}, limit=100)
                    for o in pending:
                        q = await Qualification(self.store).qualify(o['id'], actor)
                        result['qualified' if q['qualification_status'] == 'QUALIFIED' else 'rejected'] += 1
            # explicit invariant: the scheduler never creates jobs, budgets, or authorizations
            await self.store.insert('discovery_runs', {'result': result, 'finished_at': now(), 'actor': actor})
        finally:
            await self.store.db.system_settings.update_one({'id': 'scheduler-state'}, {'$set': {'config.lease_until': now(), 'config.last_finished_at': now()}})
        return result


class Briefing:
    """Deterministic DB aggregation. Uses NO model inference."""

    def __init__(self, store):
        self.store = store

    async def _counts(self, collection, field, values):
        out = {}
        for v in values:
            out[v] = await self.store.db[collection].count_documents({field: v})
        return out

    async def build(self, actor='scheduler'):
        if not (await get_flags(self.store))['NIGHTLY_BRIEFING_ENABLED']:
            raise HTTPException(409, 'NIGHTLY_BRIEFING_DISABLED')
        spent = 0
        async for u in self.store.db.model_usage.find({}, {'committed_nano_usd': 1, '_id': 0}):
            spent += int(u.get('committed_nano_usd') or 0)
        github = await self.store.one('integration_connections', {'id': 'github'}) or {}
        runner = await self.store.one('execution_attestations', {'id': 'runner-verification'}) or {}
        doc = await self.store.insert('briefings', {
            'generated_at': now(), 'generated_by': actor, 'model_used': None,
            'opportunities': await self._counts('bounty_opportunities', 'status',
                ['DISCOVERED', 'AWAITING_AUTHORIZATION', 'REJECTED', 'RUNNING', 'EXPIRED', 'DUPLICATE', 'CANCELLED']),
            'jobs': await self._counts('jobs', 'status',
                ['SELECTED', 'TRIAGE', 'RESEARCHING', 'CODING', 'TESTING', 'REVIEW', 'WAITING_APPROVAL', 'APPROVED', 'SUBMITTED', 'NEEDS_REVIEW', 'CANCELLED']),
            'pending_approvals': await self.store.db.approval_requests.count_documents({'status': 'WAITING_FOR_APPROVAL'}),
            'staged_prs': await self.store.db.pull_requests.count_documents({'status': {'$in': ['AWAITING_PUBLICATION', 'LOCAL_DRAFT']}}),
            'published_prs': await self.store.db.pull_requests.count_documents({'external': True}),
            'provider_confirmed_spend_nano_usd': spent,
            'escalations': await self.store.db.escalations.count_documents({}),
            'github_status': github.get('status', 'NOT_CONNECTED'),
            'github_write': github.get('write_permission', 'NOT_VERIFIED'),
            'e2b_runtime_verified': bool(runner.get('verified')),
            'e2b_network_note': runner.get('network_note'),
        })
        await self.store.audit('learning', 'Nightly briefing generated', 'Deterministic DB aggregation; no model spend', actor=actor)
        return doc


async def can_escalate(store, job_id, reason):
    """Bounded, machine-checkable escalation gate. Returns a decision; never escalates on its own."""
    flags = await get_flags(store)
    if not flags['AUTO_ESCALATION_ENABLED']:
        return {'allowed': False, 'reason': 'AUTO_ESCALATION_DISABLED'}
    if reason not in ESCALATION_REASONS:
        return {'allowed': False, 'reason': f'INVALID_ESCALATION_REASON:{reason}'}
    prior = await store.db.escalations.count_documents({'job_id': job_id})
    if prior >= ESCALATION_LIMITS['max_escalations_per_job']:
        return {'allowed': False, 'reason': 'MAX_ESCALATIONS_REACHED'}
    calls = await store.db.model_usage.count_documents({'job_id': job_id})
    if calls >= ESCALATION_LIMITS['max_total_model_calls_per_job']:
        return {'allowed': False, 'reason': 'MAX_MODEL_CALLS_REACHED'}
    from .budget import RunBudget
    st = await RunBudget(store).state()
    if not st.get('authorized') or (st['cap_nano_usd'] - st['spent_nano_usd']) <= 0:
        return {'allowed': False, 'reason': 'NO_REMAINING_BUDGET'}
    return {'allowed': True, 'reason': reason}


import hashlib
import httpx


def canonical_duplicate_key(source, repository, issue_number, source_identifier=None, source_url=None):
    """Deterministic identity hierarchy: repo#issue > source-id > canonical URL > hash. Never title alone."""
    if repository and issue_number not in (None, ''):
        return f"{source}:{repository.lower()}#{issue_number}"
    if source_identifier:
        return f"{source}:id:{source_identifier}"
    if source_url:
        u = re.sub(r'[#?].*$', '', str(source_url).strip().lower()).rstrip('/')
        return f"{source}:url:{u}"
    blob = f"{source}|{repository}|{issue_number}|{source_identifier}|{source_url}"
    return f"{source}:hash:{hashlib.sha256(blob.encode()).hexdigest()[:20]}"


class GitHubIssueAdapter:
    """Read-only GitHub issue discovery. Never mutates; never treats issue text as instructions."""
    source_name = 'github'
    source_version = '1'

    def __init__(self, store):
        self.store = store

    def normalize(self, item):
        """Pure, deterministic normalization of a GitHub search result item."""
        repo_url = item.get('repository_url', '') or ''
        repository = '/'.join(repo_url.split('/')[-2:]) if repo_url else None
        return {
            'source': self.source_name, 'source_identifier': str(item.get('id')),
            'source_url': item.get('html_url'), 'repository': repository,
            'issue_number': item.get('number'),
            'title': (item.get('title') or '')[:200],
            'description': (item.get('body') or '')[:2000],
            'labels': [l.get('name') for l in (item.get('labels') or []) if isinstance(l, dict)][:20],
            'reward': None, 'currency': None,
            'execution_authority': 'GITHUB_AUTHORIZED',
            'created_at': item.get('created_at'), 'updated_at': item.get('updated_at'),
            'source_state': item.get('state'),
        }

    async def fetch(self, config):
        """Live read-only GitHub issue search. Supports MULTIPLE bounty-signal queries (broadened coverage).
        Tolerant of errors; a failing query is skipped. Raises only if the very first query hard-fails."""
        from .secrets import SecretVault
        token = SecretVault().get('github')
        if not token:
            raise HTTPException(409, 'GITHUB_REQUIRED for discovery')
        queries = config.get('queries') or [config.get('query') or 'is:issue is:open label:bounty']
        if isinstance(queries, str):
            queries = [queries]
        per_page = min(int(config.get('per_page', 20)), 50)
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']}
        seen, out, first = set(), [], True
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            for q in queries[:6]:
                try:
                    res = await client.get(os.environ['GITHUB_API_URL'] + '/search/issues',
                                           params={'q': q, 'per_page': per_page, 'sort': 'updated'}, headers=headers)
                    if res.status_code != 200:
                        if first:
                            raise HTTPException(502, f'GitHub discovery search HTTP {res.status_code}')
                        continue
                    for i in (res.json().get('items') or []):
                        if 'pull_request' in i or i.get('id') in seen:
                            continue
                        seen.add(i.get('id'))
                        out.append(self.normalize(i))
                except HTTPException:
                    if first:
                        raise
                finally:
                    first = False
        return out


DEFAULT_GITHUB_QUERIES = [
    'is:issue is:open label:bounty',
    'is:issue is:open label:"💎 Bounty"',
    'is:issue is:open label:reward',
    'is:issue is:open "/bounty" in:comments',
    'is:issue is:open "💎 $" in:body',
]


class OpireAdapter:
    """Read-only Opire radar (api.opire.dev/rewards). Comment-based /claim flow => MANUAL_CLAIM certainty,
    NOT escrow-guaranteed, so rewards stay PARSED_UNVERIFIED (never auto-VERIFIED)."""
    source_name = 'opire'
    source_version = '1'

    def __init__(self, store):
        self.store = store

    def _repo_issue(self, url):
        m = re.search(r'github\.com/([^/]+/[^/]+)/issues/(\d+)', str(url or ''))
        return (m.group(1), int(m.group(2))) if m else (None, None)

    def normalize(self, it):
        repo, issue = self._repo_issue(it.get('url'))
        created = it.get('createdAt')
        try:
            reward_created = datetime.fromtimestamp(int(created) / 1000, timezone.utc).isoformat() if created else None
        except Exception:
            reward_created = None
        pp = it.get('pendingPrice') or {}
        val = pp.get('value') if isinstance(pp, dict) else None
        dollars = round(float(val) / 100, 2) if val not in (None, '') else None  # Opire pendingPrice is USD cents
        return {
            'source': self.source_name, 'source_identifier': str(it.get('id')),
            'source_url': it.get('url'), 'repository': repo, 'issue_number': issue,
            'title': (it.get('title') or '')[:200], 'description': '',
            'labels': it.get('programmingLanguages') or [],
            'reward': dollars, 'currency': 'USD', 'authoritative_reward': False,
            'payout_mechanism': 'COMMENT_CLAIM', 'payout_authority': 'opire',
            'payout_conditions': 'Opire /claim comment flow; sponsor/maintainer release, not escrow-guaranteed',
            'payout_source_url': it.get('url'), 'payout_source_type': 'platform_api',
            'payout_certainty': 'MANUAL_CLAIM',
            'execution_authority': 'HUMAN_CLAIM_REQUIRED',
            'reward_created_at': reward_created, 'solver_count': len(it.get('claimerUsers') or []),
            'source_state': 'open',
        }

    async def fetch(self, config):
        # api.opire.dev/rewards is page-based (~30/page). Walk all pages so the full live set is ingested,
        # not just page 1. Freshness is not a gate; GitHub issue open-state is verified downstream.
        max_pages = int(config.get('opire_max_pages', 30))
        max_total = int(config.get('opire_max_total', 1000))
        seen, items = set(), []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for page in range(1, max_pages + 1):
                res = await client.get('https://api.opire.dev/rewards', params={'page': page})
                if res.status_code != 200:
                    if page == 1:
                        raise HTTPException(502, f'Opire HTTP {res.status_code}')
                    break
                batch = res.json()
                batch = batch if isinstance(batch, list) else (batch.get('items') or [])
                fresh = [x for x in batch if x.get('id') and x['id'] not in seen]
                for x in fresh:
                    seen.add(x['id'])
                items.extend(fresh)
                if not batch or not fresh or len(items) >= max_total:
                    break
        out = [self.normalize(i) for i in items]
        return [o for o in out if o['repository']]  # radar needs a resolvable repo#issue


class AlgoraAdapter:
    """Read-only Algora escrow bounties (algora.io/api/bounties). Escrow => FUNDED_ESCROW, amount authoritative
    (USD cents). Often empty; non-JSON/empty responses yield [] (no supply, not a failure)."""
    source_name = 'algora'
    source_version = '1'

    def __init__(self, store):
        self.store = store

    def _repo_issue(self, url):
        m = re.search(r'github\.com/([^/]+/[^/]+)/issues/(\d+)', str(url or ''))
        return (m.group(1), int(m.group(2))) if m else (None, None)

    def normalize(self, it):
        issue = it.get('issue') or {}
        url = issue.get('html_url') or it.get('url')
        repo, num = self._repo_issue(url)
        amt = it.get('amount')
        if isinstance(amt, dict):
            amt = amt.get('amount')
        dollars = round(float(amt) / 100, 2) if amt not in (None, '') else None  # Algora amounts are USD cents
        return {
            'source': self.source_name, 'source_identifier': str(it.get('id')),
            'source_url': url, 'repository': repo, 'issue_number': num,
            'title': (issue.get('title') or it.get('title') or '')[:200],
            'description': (it.get('description') or issue.get('body') or '')[:2000], 'labels': [],
            'reward': dollars, 'currency': (it.get('currency') or 'USD').upper(),
            'authoritative_reward': dollars is not None,
            'payout_mechanism': 'PLATFORM_ESCROW', 'payout_authority': 'algora',
            'payout_conditions': 'Algora escrow; Stripe Express + KYC for payout',
            'payout_source_url': url, 'payout_source_type': 'platform_api',
            'payout_certainty': 'FUNDED_ESCROW',
            'execution_authority': 'GITHUB_AUTHORIZED',
            'reward_created_at': it.get('created_at'), 'solver_count': it.get('attempt_count'),
            'source_state': issue.get('state') or it.get('status'),
        }

    async def fetch(self, config):
        # algora.io/api/bounties now returns HTTP 406; the console tRPC endpoint is the live public feed.
        limit = int(config.get('per_page', 50))
        import json as _json
        params = {'input': _json.dumps({'json': {'limit': min(limit, 100)}})}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            res = await client.get('https://console.algora.io/api/trpc/bounty.list',
                                   params=params, headers={'Accept': 'application/json'})
        if res.status_code != 200:
            raise HTTPException(502, f'Algora HTTP {res.status_code}')
        try:
            data = res.json()
        except Exception:
            return []
        items = []
        if isinstance(data, list) and data:
            items = (((data[0] or {}).get('result') or {}).get('data') or {}).get('json') or {}
            items = items.get('items') if isinstance(items, dict) else []
        elif isinstance(data, dict):
            items = data.get('items') or []
        out = [self.normalize(i) for i in (items or [])[:limit]]
        return [o for o in out if o['repository']]


class TaskBountyAdapter:
    """Read-only TaskBounty radar (official GET /api/v1/tasks). Lists open, FUNDED bounties that TaskBounty
    verifies end-to-end in an E2B sandbox before releasing payout => PLATFORM_CONTROLLED_RELEASE with an
    authoritative amount (USD cents). Anonymous listing works; an API key is only needed to SUBMIT (never for
    discovery). Non-JSON/empty responses yield [] (no supply, not a failure)."""
    source_name = 'taskbounty'
    source_version = '1'

    def __init__(self, store):
        self.store = store

    def _repo_issue(self, url):
        m = re.search(r'github\.com/([^/]+/[^/]+)/issues/(\d+)', str(url or ''))
        return (m.group(1), int(m.group(2))) if m else (None, None)

    def normalize(self, it):
        issue_url = it.get('github_issue_url') or it.get('issue_url')
        repo_url = it.get('github_repo_url') or it.get('repo_url')
        repo, issue = self._repo_issue(issue_url)
        if not repo and repo_url:
            m = re.search(r'github\.com/([^/]+/[^/]+)', str(repo_url))
            repo = re.sub(r'\.git$', '', m.group(1)) if m else None
        cents = it.get('bounty_cents')
        if cents in (None, '') and it.get('reward') not in (None, ''):
            try:
                cents = round(float(it.get('reward')) * 100)
            except (TypeError, ValueError):
                cents = None
        dollars = round(float(cents) / 100, 2) if cents not in (None, '') else None
        return {
            'source': self.source_name,
            'source_identifier': str(it.get('task_id') or it.get('id') or it.get('slug') or ''),
            'source_url': issue_url or repo_url or it.get('url'),
            'repository': repo, 'issue_number': issue,
            'title': (it.get('title') or '')[:200], 'description': (it.get('description') or '')[:2000],
            'labels': [x for x in [it.get('language'), it.get('complexity_tag')] if x],
            'reward': dollars, 'currency': (it.get('currency') or 'USD').upper(),
            'authoritative_reward': dollars is not None,
            'payout_mechanism': 'PLATFORM_VERIFIED_RELEASE', 'payout_authority': 'taskbounty',
            'payout_conditions': 'TaskBounty funds the bounty and verifies the winning PR end-to-end in an E2B sandbox before releasing payout (1 business day)',
            'payout_source_url': issue_url or repo_url, 'payout_source_type': 'platform_api',
            'payout_certainty': 'PLATFORM_CONTROLLED_RELEASE',
            'execution_authority': 'GITHUB_AUTHORIZED',
            'reward_created_at': it.get('created_at'), 'solver_count': it.get('solver_count') or it.get('attempt_count'),
            'source_state': it.get('status') or 'open',
        }

    async def fetch(self, config):
        headers = {'Accept': 'application/json'}
        key = os.environ.get('TASKBOUNTY_API_KEY')  # optional; only required for submission, not discovery
        if key:
            headers['Authorization'] = f'Bearer {key}'
        base = os.environ.get('TASKBOUNTY_API_URL', 'https://www.task-bounty.com/api/v1').rstrip('/')
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            res = await client.get(base + '/tasks', headers=headers)
        if res.status_code != 200:
            raise HTTPException(502, f'TaskBounty HTTP {res.status_code}')
        try:
            data = res.json()
        except Exception:
            return []
        if isinstance(data, dict):
            items = data.get('data') or data.get('items') or data.get('tasks') or []
        else:
            items = data or []
        out = [self.normalize(i) for i in items[:int(config.get('per_page', 50))]]
        return [o for o in out if o['repository']]


class SuperteamAdapter:
    """Read-only Superteam Earn radar (public GET /api/listings, no auth). Superteam bounties are sponsor-funded
    and sponsor-selected, and payout ALWAYS requires a human wallet/KYC claim => SPONSOR_APPROVAL_REQUIRED +
    HUMAN_CLAIM_REQUIRED, so they surface in OWNER_APPROVAL (never AUTONOMOUS). agent_access is preserved for
    visibility. These are Solana listings without a GitHub repo/issue, so they are never code-executable."""
    source_name = 'superteam'
    source_version = '1'
    BASE = 'https://earn.superteam.fun'

    def __init__(self, store):
        self.store = store

    def normalize(self, it):
        slug = it.get('slug') or ''
        typ = it.get('type') or 'bounty'
        amount = it.get('rewardAmount')
        try:
            amount = round(float(amount), 2) if amount not in (None, '') else None
        except (TypeError, ValueError):
            amount = None
        agent_access = it.get('agentAccess') or 'HUMAN_ONLY'
        return {
            'source': self.source_name,
            'source_identifier': str(it.get('id') or slug),
            'source_url': f'{self.BASE}/listings/{typ}/{slug}' if slug else self.BASE,
            'repository': None, 'issue_number': None,
            'title': (it.get('title') or '')[:200], 'description': '',
            'labels': [x for x in [typ, (it.get('sponsor') or {}).get('name')] if x],
            'reward': amount, 'currency': (it.get('token') or 'USDC').upper(),
            'authoritative_reward': amount is not None,
            'payout_mechanism': 'SPONSOR_SELECTED_PAYOUT', 'payout_authority': 'superteam',
            'payout_conditions': 'Sponsor-funded listing; sponsor selects winners and a human must claim payout via wallet/KYC',
            'payout_source_url': f'{self.BASE}/listings/{typ}/{slug}' if slug else self.BASE, 'payout_source_type': 'platform_api',
            'payout_certainty': 'SPONSOR_APPROVAL_REQUIRED',
            'execution_authority': 'HUMAN_CLAIM_REQUIRED',
            'agent_access': agent_access,
            'reward_created_at': None, 'solver_count': ((it.get('_count') or {}).get('Submission')),
            'source_state': it.get('status') or 'OPEN', 'deadline': it.get('deadline'),
        }

    async def fetch(self, config):
        limit = int(config.get('per_page', 50))
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            res = await client.get(f'{self.BASE}/api/listings', params={'take': min(limit, 100)},
                                   headers={'Accept': 'application/json'})
        if res.status_code != 200:
            raise HTTPException(502, f'Superteam HTTP {res.status_code}')
        try:
            data = res.json()
        except Exception:
            return []
        items = data if isinstance(data, list) else (data.get('data') or data.get('listings') or [])
        out = []
        for it in items[:limit]:
            if str(it.get('status') or 'OPEN').upper() != 'OPEN' or it.get('isWinnersAnnounced'):
                continue
            out.append(self.normalize(it))
        return out




class BountyHubAdapter:
    """Best-effort, read-only BountyHub (bountyhub.dev) inventory. Unverified like any non-escrow source:
    its reward/authority claims NEVER override the authoritative checks. Silently yields [] if unavailable."""
    source_name = 'bountyhub'
    source_version = '1'
    BASE = os.environ.get('BOUNTYHUB_API_URL', 'https://bountyhub.dev')

    def __init__(self, store):
        self.store = store

    def _repo_issue(self, url):
        m = re.search(r'github\.com/([^/]+/[^/]+)/issues/(\d+)', str(url or ''))
        return (m.group(1), int(m.group(2))) if m else (None, None)

    def normalize(self, it):
        url = it.get('issueUrl') or it.get('url') or it.get('githubUrl') or it.get('link')
        repo, issue = self._repo_issue(url)
        amt = it.get('amount') or it.get('reward') or it.get('bounty')
        try:
            dollars = round(float(amt), 2) if amt not in (None, '') else None
        except (TypeError, ValueError):
            dollars = None
        return {
            'source': self.source_name, 'source_identifier': str(it.get('id') or it.get('slug') or url or ''),
            'source_url': url, 'repository': repo, 'issue_number': issue,
            'title': (it.get('title') or it.get('name') or '')[:200],
            'description': (it.get('description') or '')[:2000], 'labels': it.get('labels') or [],
            'reward': dollars, 'currency': (it.get('currency') or 'USD').upper(), 'authoritative_reward': False,
            'payout_mechanism': 'PLATFORM_LISTING', 'payout_authority': 'bountyhub',
            'payout_conditions': 'BountyHub listing; reward/authority unverified, human claim',
            'payout_source_url': url, 'payout_source_type': 'platform_api',
            'payout_certainty': 'MANUAL_CLAIM',
            'execution_authority': 'GITHUB_AUTHORIZED' if repo else 'HUMAN_CLAIM_REQUIRED',
            'reward_created_at': it.get('created_at') or it.get('createdAt'),
            'solver_count': None, 'source_state': it.get('status') or 'open',
        }

    async def fetch(self, config):
        items = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for path in ('/api/bounties', '/api/inventory', '/api/listings'):
                try:
                    res = await client.get(self.BASE + path, headers={'Accept': 'application/json'})
                    if res.status_code != 200:
                        continue
                    data = res.json()
                except Exception:
                    continue
                items = data if isinstance(data, list) else (data.get('data') or data.get('items') or data.get('bounties') or [])
                if items:
                    break
        out = [self.normalize(i) for i in (items or [])[:int(config.get('per_page', 50))]]
        return [o for o in out if o.get('repository')]


class SourceHealth:
    def __init__(self, store):
        self.store = store

    async def record(self, source, ok, discovered=0, duplicates=0, error=None):
        existing = await self.store.one('source_health', {'id': source}) or {}
        consecutive = 0 if ok else int(existing.get('consecutive_failures', 0)) + 1
        update = {'id': source, 'last_checked_at': now(),
                  'consecutive_failures': consecutive,
                  'total_discovered': int(existing.get('total_discovered', 0)) + discovered,
                  'total_duplicates': int(existing.get('total_duplicates', 0)) + duplicates}
        if ok:
            update['last_success_at'] = now()
            update['status'] = 'HEALTHY'
            update['last_error'] = None
        else:
            update['last_failure_at'] = now()
            update['last_error'] = str(error)[:300]
            update['status'] = 'DEGRADED' if consecutive < 3 else 'FAILING'
        await self.store.db.source_health.update_one({'id': source}, {'$set': update}, upsert=True)
        return update


class LiveRecheck:
    """Fresh source-state verification before an opportunity is treated as execution-ready. Read-only, no spend."""

    def __init__(self, store):
        self.store = store

    async def _github_fetch(self, repository, issue_number):
        from .secrets import SecretVault
        token = SecretVault().get('github')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            res = await client.get(f"{os.environ['GITHUB_API_URL']}/repos/{repository}/issues/{issue_number}", headers=headers)
        if res.status_code == 404:
            return {'exists': False}
        if res.status_code in (403, 401):
            return {'repo_inaccessible': True}
        if res.status_code != 200:
            raise HTTPException(502, f'GitHub recheck HTTP {res.status_code}')
        data = res.json()
        return {'exists': True, 'state': data.get('state'), 'locked': bool(data.get('locked'))}

    async def recheck(self, opportunity_id, fetcher=None, actor='scheduler'):
        o = await self.store.one('bounty_opportunities', {'id': opportunity_id})
        if not o:
            raise HTTPException(404, 'Opportunity not found')
        fetch = fetcher or self._github_fetch
        changed = []
        if not o.get('repository') or o.get('issue_number') in (None, ''):
            changed = ['missing source reference']
        else:
            try:
                state = await fetch(o['repository'], o['issue_number'])
            except Exception as exc:
                changed = [f'source unavailable: {str(exc)[:80]}']
                state = None
            if state is not None:
                if state.get('repo_inaccessible'):
                    changed.append('repository inaccessible')
                elif not state.get('exists'):
                    changed.append('issue no longer exists')
                elif state.get('state') != 'open':
                    changed.append('issue is not open')
                elif state.get('locked'):
                    changed.append('issue is locked')
        if changed:
            reason = '; '.join(changed)
            await self.store.db.bounty_opportunities.update_one({'id': opportunity_id},
                {'$set': {'status': 'STALE_REQUIRES_RECHECK', 'recheck_reason': reason, 'last_checked_at': now()}})
            await self.store.audit('scout', 'Opportunity stale on recheck', f'{opportunity_id}: {reason}; not executable, no spend', 'BLOCKED', actor=actor)
            return {'id': opportunity_id, 'fresh': False, 'status': 'STALE_REQUIRES_RECHECK', 'reason': reason}
        await self.store.db.bounty_opportunities.update_one({'id': opportunity_id}, {'$set': {'last_checked_at': now(), 'recheck_reason': None}})
        return {'id': opportunity_id, 'fresh': True, 'status': o.get('status')}


def all_source_adapters(store):
    return [OpireAdapter(store), AlgoraAdapter(store), TaskBountyAdapter(store),
            SuperteamAdapter(store), BountyHubAdapter(store)]


async def executable_count(store):
    """How many opportunities are executable-now (QUALIFIED + CLEARED + linked issue + known authority)."""
    from .classification import classify
    n = 0
    for o in await store.rows('bounty_opportunities',
                              {'qualification_status': 'QUALIFIED', 'risk_status': 'CLEARED'}, limit=1000):
        if classify(o)['executability'] == 'YES':
            n += 1
    return n


async def run_discovery_cycle(store, actor='scheduler-bg'):
    """One best-effort scout cycle across every source + broadened GitHub queries."""
    cfg_row = await store.one('system_settings', {'id': 'discovery-config'})
    cfg = dict((cfg_row or {}).get('config') or {})
    cfg.setdefault('queries', DEFAULT_GITHUB_QUERIES)
    cfg.setdefault('per_page', 50)
    adapters = all_source_adapters(store)
    adapters.append(GitHubIssueAdapter(store))  # GitHub-native broadened search always included
    return await Scheduler(store).run_once(source_adapters=adapters, config=cfg, actor=actor)


async def scheduler_loop(store):
    """Restart-safe background trigger. Keeps scouting while executable supply is ZERO (cooldown-gated),
    instead of letting the board go dead. Discovers/qualifies only; never executes."""
    import asyncio as _a
    interval = int(os.environ.get('SCHEDULER_TICK_SECONDS', '900'))
    zero_cooldown = int(os.environ.get('SCOUT_ZERO_COOLDOWN_SECONDS', '600'))
    last_zero_scout = 0.0
    tick = min(interval, 60)
    elapsed = 0
    while True:
        try:
            await _a.sleep(tick)
            elapsed += tick
            flags = await get_flags(store)
            periodic_due = elapsed >= interval
            # Auto-scout at zero executable supply (cooldown-gated) regardless of the periodic schedule.
            zero_due = False
            if flags['DISCOVERY_ENABLED'] and (_a.get_event_loop().time() - last_zero_scout) >= zero_cooldown:
                try:
                    zero_due = (await executable_count(store)) == 0
                except Exception:
                    zero_due = False
            if periodic_due:
                elapsed = 0
            if not (flags['SCHEDULER_BACKGROUND_ENABLED'] and periodic_due) and not zero_due:
                continue
            await run_discovery_cycle(store, actor=('scout-zero' if zero_due else 'scheduler-bg'))
            if zero_due:
                last_zero_scout = _a.get_event_loop().time()
            if flags['NIGHTLY_BRIEFING_ENABLED']:
                latest = await store.rows('briefings', {}, limit=1)
                if not latest or (latest[0].get('generated_at') or '')[:10] < now()[:10]:
                    await Briefing(store).build('scheduler-bg')
        except Exception:
            continue  # a bad tick never kills the loop
