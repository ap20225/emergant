"""Autonomous authorization policy + hard autonomous budget. Deterministic; never merges; never self-increases.

A paid opportunity may become AUTHORIZED only when every condition passes AND AUTONOMOUS_EXECUTION_ENABLED
is true. Reward must be VERIFIED (or explicit allow_test_work). Discovery/qualification never grant authority.
"""
import os

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from .autonomy import get_flags
from .repos import RepoRegistry, resolve_targets
from .store import now


def _cap(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


class AutonomousBudget:
    """Per-job + per-day hard ceilings. Atomic reservation; reconciled with provider-confirmed cost."""
    def __init__(self, store):
        self.store = store

    async def per_job_cap(self):
        from .execution_controls import get_budget
        return int((await get_budget(self.store))['per_job_cap_nano_usd'])   # DB-backed, default $0.25

    async def daily_cap(self):
        from .execution_controls import get_budget
        return int((await get_budget(self.store))['daily_budget_nano_usd'])  # DB-backed, default $25.00

    async def _ensure_today(self):
        d = now()[:10]
        doc = await self.store.one('autonomous_budget', {'id': d})
        if not doc:
            doc = {'id': d, 'date': d, 'reserved_nano_usd': 0, 'spent_nano_usd': 0, 'created_at': now()}
            try:
                await self.store.insert('autonomous_budget', doc)
            except DuplicateKeyError:
                doc = await self.store.one('autonomous_budget', {'id': d})
        return doc

    async def state(self):
        doc = await self._ensure_today()
        daily = await self.daily_cap()
        per_job = await self.per_job_cap()
        reserved = int(doc.get('reserved_nano_usd', 0))
        return {'date': doc['id'], 'daily_cap_nano_usd': daily, 'per_job_cap_nano_usd': per_job,
                'reserved_nano_usd': reserved, 'spent_nano_usd': int(doc.get('spent_nano_usd', 0)),
                'remaining_nano_usd': daily - reserved}

    async def available(self, amount_nano):
        per_job = await self.per_job_cap()
        if amount_nano is None or amount_nano <= 0:
            amount_nano = per_job
        if amount_nano > per_job:
            return False
        s = await self.state()
        return s['remaining_nano_usd'] >= amount_nano

    async def reserve(self, job_id, amount_nano):
        """Atomic: only reserves if it keeps daily reserved <= the hard daily cap. Never self-increases the cap.
        A single job can never consume the whole master budget because amount is capped at per_job_cap."""
        per_job = await self.per_job_cap()
        if amount_nano > per_job:
            raise HTTPException(409, 'Per-job autonomous budget cap exceeded; no reservation')
        d = now()[:10]
        daily = await self.daily_cap()
        await self._ensure_today()
        res = await self.store.db.autonomous_budget.find_one_and_update(
            {'id': d, '$expr': {'$lte': [{'$add': ['$reserved_nano_usd', amount_nano]}, daily]}},
            {'$inc': {'reserved_nano_usd': int(amount_nano)}})
        if not res:
            raise HTTPException(409, 'Daily autonomous budget exhausted; no reservation made')
        await self.store.audit('treasurer', 'Autonomous budget reserved', f'{job_id}: {amount_nano} nano-USD reserved')
        return True

    async def reconcile(self, job_id, confirmed_nano):
        d = now()[:10]
        await self.store.db.autonomous_budget.update_one({'id': d}, {'$inc': {'spent_nano_usd': int(confirmed_nano or 0)}})
        await self.store.audit('treasurer', 'Autonomous budget reconciled', f'{job_id}: provider-confirmed {confirmed_nano} nano-USD')

    async def release(self, job_id, amount_nano, date=None):
        """Return an UNSPENT reservation to the daily master (floored at 0). Used only to reconcile
        orphaned QUEUED jobs after a restart — a reservation whose job never ran to completion.
        Never increases the cap; never lowers spent_nano_usd."""
        d = date or now()[:10]
        doc = await self.store.one('autonomous_budget', {'id': d})
        if not doc:
            return
        give_back = min(int(amount_nano or 0), int(doc.get('reserved_nano_usd', 0)))
        if give_back <= 0:
            return
        await self.store.db.autonomous_budget.update_one({'id': d}, {'$inc': {'reserved_nano_usd': -give_back}})
        await self.store.audit('treasurer', 'Autonomous budget reservation released', f'{job_id}: {give_back} nano-USD released (orphaned reservation)')


class AutonomousAuthorization:
    """Read-only policy evaluation. NEVER writes execution authority to an opportunity."""
    def __init__(self, store):
        self.store = store
        self.registry = RepoRegistry(store)
        self.budget = AutonomousBudget(store)

    async def evaluate(self, opportunity, estimated_cost_nano=None, allow_test_work=False):
        o = opportunity
        checks, reasons = {}, []
        flags = await get_flags(self.store)

        rstatus = o.get('reward_status') or ('VERIFIED' if o.get('reward_verified') else o.get('reward_verification_status') or 'UNKNOWN')
        # Platform API-confirmed rewards from known bounty sources are treated as verified —
        # the platform IS the reward authority for its own listings.
        TRUSTED_PLATFORM_SOURCES = {'opire', 'algora', 'taskbounty'}
        if rstatus != 'VERIFIED' and o.get('payout_source_type') == 'platform_api' and o.get('source') in TRUSTED_PLATFORM_SOURCES:
            auth_amount = o.get('authoritative_reward_amount') or o.get('reward_amount')
            if auth_amount and float(auth_amount) > 0:
                rstatus = 'VERIFIED'
        checks['reward_verified'] = (rstatus == 'VERIFIED')
        if rstatus != 'VERIFIED' and not allow_test_work:
            reasons.append(f'reward not VERIFIED (status={rstatus})')

        checks['issue_open'] = o.get('status') not in ('STALE_REQUIRES_RECHECK', 'REJECTED', 'COMPLETED')
        if not checks['issue_open']:
            reasons.append('issue not open/actionable')

        checks['safety_cleared'] = (o.get('risk_status') == 'CLEARED')
        if not checks['safety_cleared']:
            reasons.append('safety/prompt-injection checks not cleared')

        checks['qualified'] = (o.get('qualification_status') == 'QUALIFIED')
        if not checks['qualified']:
            reasons.append('opportunity not deterministically qualified')

        targets = await resolve_targets(self.registry, o)
        checks['repo_write_authorized'] = targets['write_ready']
        if not targets['write_ready']:
            reasons.append('BLOCKED_WRITE_PERMISSION: no authorized write target or authorized fork')

        attempted = (await self.store.db.jobs.count_documents({'opportunity_id': o.get('id')}) > 0)
        if not attempted and o.get('duplicate_key'):
            attempted = await self.store.db.jobs.count_documents({'opportunity_id': o.get('duplicate_key')}) > 0
        checks['not_attempted'] = not attempted
        if attempted:
            reasons.append('opportunity already attempted')

        est = estimated_cost_nano if estimated_cost_nano is not None else await self.budget.per_job_cap()
        checks['budget_available'] = await self.budget.available(est)
        if not checks['budget_available']:
            reasons.append('autonomous budget unavailable')

        checks['execution_flag_enabled'] = bool(flags['AUTONOMOUS_EXECUTION_ENABLED'])

        if not reasons and flags['AUTONOMOUS_EXECUTION_ENABLED']:
            decision = 'AUTHORIZED'
        elif not checks['safety_cleared'] or not checks['qualified'] or not checks['issue_open']:
            decision = 'REJECTED' if (o.get('risk_status') == 'FLAGGED') else 'AWAITING_AUTHORIZATION'
        elif not targets['write_ready']:
            decision = 'BLOCKED_WRITE_PERMISSION'
        else:
            decision = 'AWAITING_AUTHORIZATION'

        return {'decision': decision, 'authorized': decision == 'AUTHORIZED', 'checks': checks,
                'reasons': reasons, 'targets': targets, 'reward_status': rstatus, 'evaluated_at': now()}
