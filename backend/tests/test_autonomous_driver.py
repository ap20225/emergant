"""Deterministic tests for the autonomous execution layer (driver + DB-backed budget).

NO network / provider / E2B / real bounty execution: the driver's ExecutionController is
stubbed so run_bounty is never actually invoked against a live repo. Uses real Mongo
(MONGO_URL/DB_NAME) and cleans up its own seed data. Gate CORRECTNESS itself is covered by
test_autonomous_authorization.py; here we verify the DRIVER decisions and the budget math.
"""
import asyncio
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException

from bounty.store import Store
from bounty import autonomy
from bounty import autonomous_driver as ad
from bounty.authorization import AutonomousBudget
from bounty.budget import RunBudget
from bounty.execution_controls import get_budget, save_budget, DEFAULTS
from bounty.publication import Publisher

TAG = 'auto-' + uuid.uuid4().hex[:8]


def _store():
    return Store(AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']])


class FakeAuth:
    """Deterministic gate stand-in driven by opportunity fields (mirrors AutonomousAuthorization)."""
    def __init__(self, store):
        self.store = store

    async def evaluate(self, o):
        checks = {'reward_verified': o.get('reward_status') == 'VERIFIED',
                  'safety_cleared': o.get('risk_status') == 'CLEARED',
                  'repo_write_authorized': bool(o.get('_authz')),
                  'qualified': o.get('qualification_status') == 'QUALIFIED'}
        ok = all(checks.values())
        return {'decision': 'AUTHORIZED' if ok else 'AWAITING_AUTHORIZATION', 'checks': checks, 'reasons': []}


async def _mk_driver(store, run_bounty=None, tasks=None):
    controller = SimpleNamespace()
    calls = []
    async def _run(opp_id, actor):
        calls.append((opp_id, actor))
        if run_bounty:
            return await run_bounty(opp_id, actor)
        return {'job_id': 'job_' + uuid.uuid4().hex[:8], 'status': 'QUEUED'}
    controller.run_bounty = _run
    orch = SimpleNamespace(tasks=tasks if tasks is not None else {})
    driver = ad.AutonomousDriver(store, controller, orch)
    driver._calls = calls
    return driver


async def _seed_opp(store, **over):
    oid = f'{TAG}-{uuid.uuid4().hex[:6]}'
    doc = {'id': oid, 'created_at': autonomy.now(), 'status': 'AWAITING_AUTHORIZATION',
           'qualification_status': 'QUALIFIED', 'risk_status': 'CLEARED',
           'reward_status': 'VERIFIED', 'repository': f'{TAG}/repo', 'issue_number': 1,
           'reward_amount': 50.0, '_authz': True, 'duplicate_key': oid}
    doc.update(over)
    await store.db.bounty_opportunities.insert_one(dict(doc))
    return oid


async def _cleanup(store):
    await store.db.bounty_opportunities.delete_many({'id': {'$regex': f'^{TAG}'}})
    await store.db.jobs.delete_many({'bounty_id': {'$regex': f'^{TAG}'}})


async def _set_flag(store, value):
    await autonomy.set_flag(store, 'AUTONOMOUS_EXECUTION_ENABLED', value, 'test')


# ---------- Driver decision behaviors ----------

def test_1_autonomous_off_no_execution():
    async def run():
        s = _store()
        ad.AutonomousAuthorization = FakeAuth
        await _set_flag(s, False)
        d = await _mk_driver(s)
        try:
            await _seed_opp(s)
            await d.tick()
            st = await d.state()
            return st, d._calls
        finally:
            await _cleanup(s)
    st, calls = asyncio.run(run())
    assert st['status'] == 'STOPPED' and st['reason'] == 'DISABLED'
    assert calls == []


def test_2_on_no_eligible_stays_idle():
    async def run():
        s = _store()
        ad.AutonomousAuthorization = FakeAuth
        await _set_flag(s, True)
        d = await _mk_driver(s)
        try:
            # an opportunity that fails a gate -> not eligible
            await _seed_opp(s, reward_status='UNKNOWN')
            await d.tick()
            return await d.state(), d._calls
        finally:
            await _set_flag(s, False)
            await _cleanup(s)
    st, calls = asyncio.run(run())
    assert st['status'] == 'IDLE'
    assert st['reason'] in ('REWARD_NOT_VERIFIED', 'NO_ELIGIBLE_BOUNTIES')
    assert calls == []


def test_3_verified_cleared_authorized_starts():
    async def run():
        s = _store()
        ad.AutonomousAuthorization = FakeAuth
        await _set_flag(s, True)
        d = await _mk_driver(s)
        try:
            oid = await _seed_opp(s)
            await d.tick()
            return oid, d._calls, await d.state()
        finally:
            await _set_flag(s, False)
            await _cleanup(s)
    oid, calls, st = asyncio.run(run())
    assert len(calls) == 1 and calls[0][0] == oid and calls[0][1] == 'autonomous-driver'
    assert st['reason'] == 'ACTIVE_JOB'


@pytest.mark.parametrize('field,val,expect', [
    ('reward_status', 'UNKNOWN', 'REWARD_NOT_VERIFIED'),
    ('risk_status', 'FLAGGED', 'SAFETY_NOT_CLEARED'),
    ('_authz', False, 'REPO_NOT_AUTHORIZED'),
])
def test_4_5_6_gate_blocks(field, val, expect):
    async def run():
        s = _store()
        ad.AutonomousAuthorization = FakeAuth
        await _set_flag(s, True)
        d = await _mk_driver(s)
        try:
            await _seed_opp(s, **{field: val})
            await d.tick()
            return await d.state(), d._calls
        finally:
            await _set_flag(s, False)
            await _cleanup(s)
    st, calls = asyncio.run(run())
    assert calls == []
    assert st['status'] == 'IDLE' and st['reason'] == expect


def test_7_duplicate_bounty_blocked():
    async def run():
        s = _store()
        ad.AutonomousAuthorization = FakeAuth
        d = await _mk_driver(s)
        try:
            oid = await _seed_opp(s)
            await s.db.jobs.insert_one({'id': f'{TAG}-job', 'bounty_id': oid, 'status': 'SUBMITTED'})
            eligible, _ = await d._eligible()
            return [o['id'] for o in eligible], oid
        finally:
            await _cleanup(s)
    ids, oid = asyncio.run(run())
    assert oid not in ids


def test_8_budget_exhausted_blocks():
    async def run():
        s = _store()
        ad.AutonomousAuthorization = FakeAuth
        await _set_flag(s, True)
        prev = await s.one('system_settings', {'id': 'execution-controls'})
        d = await _mk_driver(s)
        try:
            # daily == per_job and reserve it fully -> remaining < per_job
            await save_budget(s, {'daily_budget_nano_usd': 250_000_000, 'per_job_cap_nano_usd': 250_000_000}, 'test')
            day = autonomy.now()[:10]
            await s.db.autonomous_budget.update_one({'id': day}, {'$set': {'id': day, 'reserved_nano_usd': 250_000_000, 'spent_nano_usd': 0}}, upsert=True)
            await _seed_opp(s)
            await d.tick()
            return await d.state(), d._calls
        finally:
            await s.db.autonomous_budget.delete_one({'id': autonomy.now()[:10]})
            if prev:
                await s.db.system_settings.update_one({'id': 'execution-controls'}, {'$set': {'config': prev['config']}})
            else:
                await s.db.system_settings.delete_one({'id': 'execution-controls'})
            await _set_flag(s, False)
            await _cleanup(s)
    st, calls = asyncio.run(run())
    assert st['reason'] == 'BUDGET_EXHAUSTED' and calls == []


def test_13_no_concurrent_when_active_job():
    async def run():
        s = _store()
        ad.AutonomousAuthorization = FakeAuth
        await _set_flag(s, True)
        d = await _mk_driver(s)
        try:
            await s.db.jobs.insert_one({'id': f'{TAG}-active', 'bounty_id': f'{TAG}-x', 'status': 'CODING'})
            await _seed_opp(s)
            await d.tick()
            return await d.state(), d._calls
        finally:
            await _set_flag(s, False)
            await _cleanup(s)
    st, calls = asyncio.run(run())
    assert st['reason'] == 'ACTIVE_JOB' and calls == []


# ---------- Budget math ----------

def test_9_per_job_cap_enforced():
    async def run():
        s = _store()
        prev = await s.one('system_settings', {'id': 'execution-controls'})
        try:
            await save_budget(s, {'daily_budget_nano_usd': 25_000_000_000, 'per_job_cap_nano_usd': 250_000_000}, 'test')
            ab = AutonomousBudget(s)
            raised = False
            try:
                await ab.reserve(f'{TAG}-over', 300_000_000)  # > per-job cap
            except HTTPException:
                raised = True
            return raised
        finally:
            await s.db.autonomous_budget.delete_one({'id': autonomy.now()[:10]})
            if prev:
                await s.db.system_settings.update_one({'id': 'execution-controls'}, {'$set': {'config': prev['config']}})
            else:
                await s.db.system_settings.delete_one({'id': 'execution-controls'})
    assert asyncio.run(run()) is True


def test_10_atomic_reservation_and_reconcile():
    async def run():
        s = _store()
        prev = await s.one('system_settings', {'id': 'execution-controls'})
        day = autonomy.now()[:10]
        await s.db.autonomous_budget.delete_one({'id': day})
        try:
            await save_budget(s, {'daily_budget_nano_usd': 25_000_000_000, 'per_job_cap_nano_usd': 250_000_000}, 'test')
            ab = AutonomousBudget(s)
            before = await ab.state()
            await ab.reserve(f'{TAG}-r', 250_000_000)
            after = await ab.state()
            await ab.reconcile(f'{TAG}-r', 100_000_000)
            final = await ab.state()
            return before, after, final
        finally:
            await s.db.autonomous_budget.delete_one({'id': day})
            if prev:
                await s.db.system_settings.update_one({'id': 'execution-controls'}, {'$set': {'config': prev['config']}})
            else:
                await s.db.system_settings.delete_one({'id': 'execution-controls'})
    before, after, final = asyncio.run(run())
    assert before['remaining_nano_usd'] == 25_000_000_000
    assert after['remaining_nano_usd'] == 25_000_000_000 - 250_000_000
    assert final['spent_nano_usd'] == 100_000_000


def test_daily_backed_defaults_and_runbudget_cap():
    async def run():
        s = _store()
        prev = await s.one('system_settings', {'id': 'execution-controls'})
        await s.db.system_settings.delete_one({'id': 'execution-controls'})
        try:
            b = await get_budget(s)
            cap_default = await RunBudget(s).cap()
            await save_budget(s, {'per_job_cap_nano_usd': 500_000_000}, 'test')
            cap_after = await RunBudget(s).cap()
            return b, cap_default, cap_after
        finally:
            if prev:
                await s.db.system_settings.update_one({'id': 'execution-controls'}, {'$set': {'config': prev['config']}})
            else:
                await s.db.system_settings.delete_one({'id': 'execution-controls'})
    b, cap_default, cap_after = asyncio.run(run())
    assert b['daily_budget_nano_usd'] == DEFAULTS['daily_budget_nano_usd'] == 25_000_000_000
    assert b['per_job_cap_nano_usd'] == 250_000_000 and cap_default == 250_000_000
    assert cap_after == 500_000_000


# ---------- Recovery / stop / escalation / merge / dashboard ----------

def test_11_restart_recovery_excludes_interrupted():
    async def run():
        s = _store()
        from bounty.orchestrator import Orchestrator
        ad.AutonomousAuthorization = FakeAuth
        d = await _mk_driver(s)
        try:
            oid = await _seed_opp(s)
            # an interrupted ACTIVE job for this bounty
            await s.db.jobs.insert_one({'id': f'{TAG}-int', 'bounty_id': oid, 'status': 'CODING', 'simulated': False})
            await Orchestrator(s).recover()
            job = await s.one('jobs', {'id': f'{TAG}-int'})
            eligible, _ = await d._eligible()
            return job['status'], [o['id'] for o in eligible], oid
        finally:
            await _cleanup(s)
    status, ids, oid = asyncio.run(run())
    assert status == 'NEEDS_REVIEW'          # recovered, not blindly restarted
    assert oid not in ids                    # already attempted -> never re-run


def test_12_stopped_bounty_not_reexecuted():
    async def run():
        s = _store()
        ad.AutonomousAuthorization = FakeAuth
        d = await _mk_driver(s)
        try:
            oid = await _seed_opp(s)
            await s.db.jobs.insert_one({'id': f'{TAG}-stp', 'bounty_id': oid, 'status': 'STOPPED'})
            eligible, _ = await d._eligible()
            return [o['id'] for o in eligible], oid
        finally:
            await _cleanup(s)
    ids, oid = asyncio.run(run())
    assert oid not in ids


def test_14_escalation_bounded_limits():
    # Autonomous escalation stays bounded even when enabled; disabled -> refused.
    async def run():
        s = _store()
        await autonomy.set_flag(s, 'AUTO_ESCALATION_ENABLED', False, 'test')
        dec = await autonomy.can_escalate(s, 'job_x', 'bounded_coding_failed')
        return dec
    dec = asyncio.run(run())
    assert dec['allowed'] is False and dec['reason'] == 'AUTO_ESCALATION_DISABLED'
    assert autonomy.ESCALATION_LIMITS['max_escalations_per_job'] == 2


def test_15_merge_never_available():
    # There is no merge capability anywhere in the publication path.
    assert not hasattr(Publisher, 'merge')
    src = open('/app/backend/bounty/execution_controller.py').read()
    assert '.merge(' not in src


def test_16_dashboard_reflects_driver_state():
    async def run():
        s = _store()
        from bounty.execution_routes import _controls_view
        await s.db.system_settings.update_one({'id': 'autonomous-driver-state'},
            {'$set': {'config': {'status': 'IDLE', 'reason': 'NO_ELIGIBLE_BOUNTIES', 'eligible_count': 0}}}, upsert=True)
        return await _controls_view(s)
    view = asyncio.run(run())
    for k in ['autonomous_execution', 'autonomous_escalation', 'daily_budget_nano_usd',
              'per_job_cap_nano_usd', 'max_concurrent_jobs', 'spent_today_nano_usd',
              'remaining_today_nano_usd', 'driver']:
        assert k in view
    assert view['driver']['status'] == 'IDLE' and view['driver']['reason'] == 'NO_ELIGIBLE_BOUNTIES'
