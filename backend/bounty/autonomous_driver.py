"""Background autonomous bounty driver. Runs only while this app instance is alive.

It does NOT implement a second execution pipeline: it evaluates eligible opportunities
against the EXISTING backend gates (AutonomousAuthorization) and starts them through the
EXISTING ExecutionController.run_bounty (bounded retries/corrections/escalation, budget,
claim idempotency, review/publication safety, never merges). At most one active job.
Backend stays authoritative; the driver only reads state and calls run_bounty.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .authorization import AutonomousAuthorization, AutonomousBudget
from .autonomy import get_flags
from .execution_controls import get_budget
from .execution_controller import LIVE_STATES
from .store import now

DRIVER_INTERVAL_SECONDS = int(os.environ.get('AUTONOMOUS_DRIVER_INTERVAL_SECONDS', '15'))
BUSY_STATES = LIVE_STATES + ['QUEUED']
# Bounded, non-permanent failure backoff: a candidate that fails BEFORE a job row exists (closed
# issue, fork failure, external blocker) must not be re-selected every tick forever. We cool it
# down with capped exponential backoff and advance to the next eligible candidate; it is never
# permanently blacklisted — the cooldown expires and it becomes eligible again.
BACKOFF_BASE_SECONDS = int(os.environ.get('DRIVER_BACKOFF_BASE_SECONDS', '120'))
BACKOFF_MAX_SECONDS = int(os.environ.get('DRIVER_BACKOFF_MAX_SECONDS', '3600'))


class AutonomousDriver:
    def __init__(self, store, controller, orchestrator):
        self.store = store
        self.controller = controller
        self.orchestrator = orchestrator
        self._task = None

    async def _set_state(self, **kw):
        kw['updated_at'] = now()
        await self.store.db.system_settings.update_one(
            {'id': 'autonomous-driver-state'},
            {'$set': {f'config.{k}': v for k, v in kw.items()}}, upsert=True)

    async def state(self):
        row = await self.store.one('system_settings', {'id': 'autonomous-driver-state'})
        return (row or {}).get('config') or {'status': 'STOPPED', 'reason': 'DISABLED'}

    async def _active_job(self):
        return await self.store.one('jobs', {'status': {'$in': BUSY_STATES}})

    async def _backoff_ids(self):
        """Opportunity ids currently in failure cooldown (not yet expired)."""
        rows = await self.store.rows('driver_candidate_backoff', {'cooldown_until': {'$gt': now()}}, limit=1000)
        return {r['id'] for r in rows}

    async def _record_failure(self, opp_id, error):
        row = await self.store.one('driver_candidate_backoff', {'id': opp_id})
        failures = int((row or {}).get('failures', 0)) + 1
        delay = min(BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), BACKOFF_MAX_SECONDS)
        cooldown_until = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        from .store import redact
        await self.store.db.driver_candidate_backoff.update_one(
            {'id': opp_id},
            {'$set': {'id': opp_id, 'failures': failures, 'cooldown_until': cooldown_until,
                      'last_error': redact(str(error or ''))[:300], 'updated_at': now()}}, upsert=True)

    async def _clear_failure(self, opp_id):
        await self.store.db.driver_candidate_backoff.delete_one({'id': opp_id})

    async def _eligible(self):
        """Only opportunities passing EVERY backend gate. Never re-runs an attempted bounty, and
        skips candidates currently in failure cooldown so the driver advances to the next one.
        BLOCKED_WRITE_PERMISSION is NOT a disqualifier — run_bounty auto-creates forks.
        Platform-API bounties (trusted sources with authoritative rewards) are fetched first."""
        # Prioritize platform-API bounties from trusted sources (they have verified rewards)
        platform_opps = await self.store.rows('bounty_opportunities',
            {'status': {'$in': ['AWAITING_AUTHORIZATION', 'QUALIFIED']},
             'payout_source_type': 'platform_api'}, limit=100)
        other_opps = await self.store.rows('bounty_opportunities',
            {'status': {'$in': ['AWAITING_AUTHORIZATION', 'QUALIFIED']},
             'payout_source_type': {'$ne': 'platform_api'}}, limit=100)
        seen = {o['id'] for o in platform_opps}
        opps = platform_opps + [o for o in other_opps if o['id'] not in seen]
        cooling = await self._backoff_ids()
        auth = AutonomousAuthorization(self.store)
        eligible, blockers = [], {'REWARD_NOT_VERIFIED': 0, 'SAFETY_NOT_CLEARED': 0, 'REPO_NOT_AUTHORIZED': 0}
        for o in opps:
            if o.get('id') in cooling:
                continue
            if await self.store.db.jobs.count_documents({'bounty_id': o['id']}) > 0:
                continue
            ev = await auth.evaluate(o)
            if ev['decision'] == 'AUTHORIZED':
                eligible.append(o)
            elif ev['decision'] == 'BLOCKED_WRITE_PERMISSION' and all(
                ev['checks'].get(k) for k in ('reward_verified', 'safety_cleared', 'qualified',
                                                'issue_open', 'not_attempted', 'budget_available',
                                                'execution_flag_enabled')):
                eligible.append(o)
            elif not ev['checks'].get('reward_verified'):
                blockers['REWARD_NOT_VERIFIED'] += 1
            elif not ev['checks'].get('safety_cleared'):
                blockers['SAFETY_NOT_CLEARED'] += 1
            elif not ev['checks'].get('repo_write_authorized'):
                blockers['REPO_NOT_AUTHORIZED'] += 1
        return eligible, blockers

    async def tick(self):
        controls = await self.store.controls()
        if controls.get('global_stop') or controls.get('agents_stopped'):
            await self._set_state(status='STOPPED', reason='EMERGENCY_STOP', eligible_count=0, active_job=None)
            return
        flags = await get_flags(self.store)
        if not flags.get('AUTONOMOUS_EXECUTION_ENABLED'):
            await self._set_state(status='STOPPED', reason='DISABLED', eligible_count=0, active_job=None)
            return
        active = await self._active_job()
        if active or self.orchestrator.tasks:
            await self._set_state(status='RUNNING', reason='ACTIVE_JOB',
                                  active_job=(active or {}).get('id'))
            return
        budget = await AutonomousBudget(self.store).state()
        if budget['remaining_nano_usd'] < budget['per_job_cap_nano_usd']:
            await self._set_state(status='IDLE', reason='BUDGET_EXHAUSTED', eligible_count=0, active_job=None)
            return
        eligible, blockers = await self._eligible()
        if not eligible:
            reason = 'NO_ELIGIBLE_BOUNTIES'
            for key in ('REPO_NOT_AUTHORIZED', 'SAFETY_NOT_CLEARED', 'REWARD_NOT_VERIFIED'):
                if blockers[key]:
                    reason = key
                    break
            await self._set_state(status='IDLE', reason=reason, eligible_count=0, active_job=None)
            return
        await self._set_state(status='RUNNING', reason='STARTING', eligible_count=len(eligible))
        target = eligible[0]
        try:
            res = await self.controller.run_bounty(target['id'], 'autonomous-driver')
            await self._clear_failure(target['id'])
            await self._set_state(status='RUNNING', reason='ACTIVE_JOB', active_job=res.get('job_id'),
                                  eligible_count=len(eligible), last_execution_at=now(), last_error=None)
        except Exception as exc:
            msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
            # Pre-job failure (closed issue, fork failure, external blocker, etc.): cool this candidate
            # down (bounded, non-permanent) so the next tick advances to a different eligible candidate.
            await self._record_failure(target['id'], msg)
            await self._set_state(status='IDLE', reason='START_BLOCKED', last_error=str(msg)[:300])

    async def loop(self):
        while True:
            try:
                await asyncio.sleep(DRIVER_INTERVAL_SECONDS)
                await self.tick()
            except asyncio.CancelledError:
                await self._set_state(status='STOPPED', reason='SHUTDOWN')
                raise
            except Exception:
                continue  # a bad tick never kills the driver

    def start(self):
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self.loop())
        return self._task

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
