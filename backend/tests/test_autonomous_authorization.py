"""Deterministic autonomous authorization + budget tests. Never authorizes while the flag is off; budgets are hard."""
import asyncio
import os
import sys
import uuid

from fastapi import HTTPException

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from motor.motor_asyncio import AsyncIOMotorClient
from bounty.store import Store, now
from bounty.repos import RepoRegistry
from bounty.authorization import AutonomousAuthorization, AutonomousBudget

REPO = 'auth/' + uuid.uuid4().hex[:8]


def _store():
    return Store(AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']])


def _opp(**over):
    o = {'id': 'op_' + uuid.uuid4().hex[:8], 'repository': REPO, 'issue_number': 1,
         'status': 'AWAITING_AUTHORIZATION', 'qualification_status': 'QUALIFIED', 'risk_status': 'CLEARED',
         'reward_status': 'VERIFIED', 'reward_verified': True, 'duplicate_key': 'dk_' + uuid.uuid4().hex[:6]}
    o.update(over)
    return o


async def _authorize_repo(s):
    reg = RepoRegistry(s)
    await reg.authorize(REPO, 'owner-user')
    await reg.record_capabilities(REPO, {'CREATE_PR': True, 'WRITE_BRANCH': True})


def test_unknown_reward_ineligible():
    async def run():
        s = _store()
        try:
            await _authorize_repo(s)
            res = await AutonomousAuthorization(s).evaluate(_opp(reward_status='UNKNOWN', reward_verified=False))
            return res
        finally:
            await s.db.repo_authorizations.delete_many({'id': REPO.lower()})
    res = asyncio.run(run())
    assert res['checks']['reward_verified'] is False
    assert res['authorized'] is False
    assert any('reward not VERIFIED' in r for r in res['reasons'])


def test_verified_reward_but_no_write_authorization_blocks():
    async def run():
        s = _store()
        return await AutonomousAuthorization(s).evaluate(_opp())  # repo not authorized
    res = asyncio.run(run())
    assert res['checks']['repo_write_authorized'] is False
    assert res['decision'] == 'BLOCKED_WRITE_PERMISSION'
    assert res['authorized'] is False


def test_never_authorized_while_flag_disabled():
    async def run():
        s = _store()
        flags = await s.one('system_settings', {'id': 'feature-flags'})
        assert not ((flags or {}).get('config') or {}).get('AUTONOMOUS_EXECUTION_ENABLED'), 'flag must be OFF for this test'
        try:
            await _authorize_repo(s)
            res = await AutonomousAuthorization(s).evaluate(_opp())
            return res
        finally:
            await s.db.repo_authorizations.delete_many({'id': REPO.lower()})
    res = asyncio.run(run())
    # every gate passes yet execution flag is off -> AWAITING_AUTHORIZATION, never AUTHORIZED
    assert res['checks']['execution_flag_enabled'] is False
    assert res['decision'] == 'AWAITING_AUTHORIZATION'
    assert res['authorized'] is False


def test_flagged_safety_rejected():
    async def run():
        s = _store()
        try:
            await _authorize_repo(s)
            return await AutonomousAuthorization(s).evaluate(_opp(risk_status='FLAGGED'))
        finally:
            await s.db.repo_authorizations.delete_many({'id': REPO.lower()})
    res = asyncio.run(run())
    assert res['checks']['safety_cleared'] is False and res['decision'] == 'REJECTED'


def test_autonomous_budget_per_job_and_daily_caps_atomic():
    async def run():
        s = _store()
        b = AutonomousBudget(s)
        d = now()[:10]
        await s.db.autonomous_budget.delete_many({'id': d})
        percap, daily = await b.per_job_cap(), await b.daily_cap()
        # per-job cap enforced
        try:
            await b.reserve('j0', percap + 1)
            over = 'no-error'
        except HTTPException:
            over = 'blocked'
        # reserve up to the daily cap in per-job chunks
        reserved, i = 0, 0
        while reserved + percap <= daily:
            await b.reserve(f'j{i}', percap)
            reserved += percap
            i += 1
        # next reservation must be refused (daily exhausted)
        try:
            await b.reserve('jN', percap)
            exhausted = 'no-error'
        except HTTPException:
            exhausted = 'blocked'
        st = await b.state()
        await s.db.autonomous_budget.delete_many({'id': d})
        return over, exhausted, st, percap, daily
    over, exhausted, st, percap, daily = asyncio.run(run())
    assert over == 'blocked'
    assert exhausted == 'blocked'
    assert st['reserved_nano_usd'] <= daily
    assert st['remaining_nano_usd'] < percap  # cannot fit another job


def test_budget_available_respects_remaining():
    async def run():
        s = _store()
        b = AutonomousBudget(s)
        d = now()[:10]
        await s.db.autonomous_budget.delete_many({'id': d})
        ok_small = await b.available(1000)
        ok_over = await b.available(await b.per_job_cap() + 1)  # exceeds per-job cap
        await s.db.autonomous_budget.delete_many({'id': d})
        return ok_small, ok_over
    ok_small, ok_over = asyncio.run(run())
    assert ok_small is True and ok_over is False
