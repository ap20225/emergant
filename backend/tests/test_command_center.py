"""Command Center deterministic tests: read-only aggregation, honest NOT_TRACKED, safety-boundary invariants.
NO network / provider / E2B. Uses real Mongo (MONGO_URL/DB_NAME) and cleans up its own seed data."""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from motor.motor_asyncio import AsyncIOMotorClient
from bounty.store import Store
from bounty import autonomy
from bounty.command_center import command_center

SRC = 'cc-' + uuid.uuid4().hex[:8]


def _store():
    return Store(AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']])


def test_command_center_shape_and_read_only():
    async def run():
        s = _store()
        before = await s.db.bounty_opportunities.count_documents({})
        before_jobs = await s.db.jobs.count_documents({})
        before_budget = await s.one('system_settings', {'id': 'run-budget'})
        cc = await command_center(s)
        after = await s.db.bounty_opportunities.count_documents({})
        after_jobs = await s.db.jobs.count_documents({})
        after_budget = await s.one('system_settings', {'id': 'run-budget'})
        return cc, (before, after), (before_jobs, after_jobs), (before_budget, after_budget)
    cc, opp, jobs, budget = asyncio.run(run())
    # all six panels present + header
    for key in ['generated_at', 'system', 'sources', 'opportunities', 'finance', 'execution', 'safety']:
        assert key in cc, f'missing {key}'
    # reading the dashboard mutates nothing
    assert opp[0] == opp[1]
    assert jobs[0] == jobs[1]
    assert budget[0] == budget[1]


def test_system_reflects_disabled_flags_not_hardcoded():
    async def run():
        s = _store()
        return await command_center(s)
    cc = asyncio.run(run())
    cap = cc['system']['capability_matrix']
    flags = cc['system']['feature_flags']
    # Unattended execution + auto escalation must remain OFF in the surfaced state
    assert cap['UNATTENDED_EXECUTION'] == 'DISABLED'
    assert cap['AUTO_ESCALATION'] == 'DISABLED'
    assert flags['AUTONOMOUS_EXECUTION_ENABLED'] is False
    assert flags['AUTO_ESCALATION_ENABLED'] is False
    # scheduler section is populated from state/env, not fabricated
    assert 'background_enabled' in cc['system']['scheduler']
    assert isinstance(cc['system']['scheduler']['tick_seconds'], int)


def test_safety_distinguishes_zero_from_not_tracked():
    cc = asyncio.run(command_center(_store()))
    saf = cc['safety']
    # authoritative counts are integers (0 is a real value)
    for k in ['prompt_injection_rejects', 'unsafe_content_rejects', 'unsafe_patch_rejects',
              'publication_attempts', 'successful_publications', 'blocked_publications', 'blocked_events']:
        assert isinstance(saf[k], int), f'{k} should be an int count'
    # unbacked metrics are honestly NOT_TRACKED (never a fake 0)
    assert saf['secret_violations'] == 'NOT_TRACKED'
    assert saf['authorization_denials'] == 'NOT_TRACKED'


def test_opportunity_counts_and_qualified_boundary():
    async def run():
        s = _store()
        # seed one discovered opportunity and qualify it (deterministic, no spend)
        await autonomy.Discovery(s).ingest(SRC, [{'repository': 'owner/ccrepo', 'issue_number': 7,
                                                   'title': 'add helper', 'description': 'small fix'}], actor='test')
        opp = (await s.rows('bounty_opportunities', {'source': SRC}))[0]
        cc_before = await command_center(s)
        q = await autonomy.Qualification(s).qualify(opp['id'], actor='test')
        cc_after = await command_center(s)
        row = await s.one('bounty_opportunities', {'id': opp['id']})
        await s.db.bounty_opportunities.delete_many({'source': SRC})
        return cc_before, cc_after, q, row
    before, after, q, row = asyncio.run(run())
    # discovered counted before qualification
    assert before['opportunities']['counts']['DISCOVERED'] >= 1
    # qualification produces AWAITING_AUTHORIZATION, never AUTHORIZED
    assert q['status'] == 'AWAITING_AUTHORIZATION'
    assert row['authorization_state'] == 'NONE'
    assert after['opportunities']['counts']['AWAITING_AUTHORIZATION'] >= 1
    assert after['opportunities']['counts']['QUALIFIED'] >= 1


def test_finance_reserved_not_confused_with_confirmed():
    cc = asyncio.run(command_center(_store()))
    fin = cc['finance']
    for k in ['cap_nano_usd', 'reserved_nano_usd', 'provider_confirmed_spent_nano_usd', 'remaining_nano_usd']:
        assert isinstance(fin[k], int), f'{k} should be int nano-USD'
    # cap is the DB-backed per-job ceiling exposed by RunBudget; reserved/confirmed are separate lines
    from bounty.budget import RunBudget
    expected_cap = asyncio.run(RunBudget(_store()).state())['cap_nano_usd']
    assert fin['cap_nano_usd'] == expected_cap
    assert 'run_budget' in fin and 'authorized' in fin['run_budget']
    # revenue is a dedicated tracked field (not fabricated onto finance root)
    assert 'verified_payments' in fin['revenue']


def test_reward_never_fabricated_for_github_source():
    async def run():
        s = _store()
        await autonomy.Discovery(s).ingest(SRC, [{'repository': 'owner/ccrepo', 'issue_number': 8,
                                                   'title': 'no reward info', 'description': 'x'}], actor='test')
        cc = await command_center(s)
        await s.db.bounty_opportunities.delete_many({'source': SRC})
        return cc
    cc = asyncio.run(run())
    seeded = [o for o in cc['opportunities']['items'] if o['source'] == SRC]
    assert seeded and seeded[0]['reward'] is None
    assert seeded[0]['reward_verification_status'] == 'UNKNOWN'
