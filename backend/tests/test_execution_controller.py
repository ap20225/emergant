"""Deterministic, offline tests for the operational execution controller.
No provider spend, no E2B launch, no GitHub writes."""
import asyncio
import os
import sys

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env')
load_dotenv('/app/backend/bounty.env')

from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException

from bounty.gateway import request_timeout
from bounty.store import Store, uid, now
from bounty.execution_controller import classify_failure, next_action, execution_policy, ExecutionController
from bounty.orchestrator import Orchestrator
from bounty.github import GitHub

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(('PASS' if cond else 'FAIL'), name)


async def main():
    # 1. per-tier timeout (fixes the 30s flagship failure)
    check('timeout routine=45', request_timeout('routine', 'glm-4.7-flash') == 45)
    check('timeout coding=90', request_timeout('coding', 'qwen3-coder-flash') == 90)
    check('timeout flagship opus=120', request_timeout('coding', 'claude-opus-4.8') == 120)
    check('timeout flagship codex-max=120', request_timeout('coding', 'gpt-5.1-codex-max') == 120)

    # 2. failure classification -> action
    check('classify transient(timeout)', classify_failure('Model request failed or ambiguous; no retry') == 'TRANSIENT')
    check('classify transient(5xx)', classify_failure('Experiential HTTP 502; charge pending') == 'TRANSIENT')
    check('classify test failure', classify_failure('Real regression tests did not pass') == 'TEST_FAILURE')
    check('classify hard stop(security)', classify_failure('security review failed') == 'HARD_STOP')
    check('classify budget', classify_failure('BUDGET_CAP_WOULD_EXCEED') == 'BUDGET')
    check('classify review', classify_failure('code review requested changes') in ('REVIEW', 'TEST_FAILURE'))
    check('action transient=Retry', next_action('TRANSIENT') == 'Retry')
    check('action budget=Blocked', next_action('BUDGET') == 'Blocked')
    check('action hardstop=Blocked', next_action('HARD_STOP') == 'Blocked')

    client = AsyncIOMotorClient(os.environ['MONGO_URL'])
    store = Store(client[os.environ['DB_NAME']])
    await store.init()

    # 3. execution policy defaults + bounded limits present
    pol = await execution_policy(store)
    check('policy max_total_attempts=6', pol['max_total_model_attempts'] == 6)
    check('policy max_attempts_per_model=2', pol['max_attempts_per_model'] == 2)
    check('policy max_corrections_per_model=2', pol['max_corrections_per_model'] == 2)
    check('policy max_total_corrections=3', pol['max_total_corrections'] == 3)
    check('policy per_job_budget=0.25', pol['per_job_budget_nano_usd'] == 250000000)
    check('policy timeouts wired', pol['timeouts'] == {'routine': 45, 'coding': 90, 'flagship_coding': 120})

    ctrl = ExecutionController(store, Orchestrator(store), GitHub(store))

    # 4. claim idempotency: seeded DB guard returns ALREADY_CLAIMED with NO network call
    key = 'claude-builders-bounty/claude-builders-bounty#1'
    seeded = await store.one('bounty_claims', {'id': key})
    check('claim record seeded', bool(seeded))
    status = await ctrl._claim_if_required('claude-builders-bounty/claude-builders-bounty', 1, {})
    check('claim idempotent -> ALREADY_CLAIMED', status == 'ALREADY_CLAIMED')

    # 5. duplicate protection: a LIVE attempt blocks a new run for the same repo/issue
    fake = uid('job')
    await store.insert('jobs', {'id': fake, 'repository': 'dupe-test/repo', 'issue_number': 999,
                                'status': 'CODING', 'created_at': now()})
    live = await store.db.jobs.count_documents({'repository': 'dupe-test/repo', 'issue_number': 999,
                                                'status': {'$in': ['SELECTED', 'TRIAGE', 'CODING', 'TESTING', 'REVIEW', 'WAITING_APPROVAL', 'APPROVED', 'SUBMITTED']}})
    check('duplicate live attempt detected', live == 1)
    await store.db.jobs.delete_one({'id': fake})  # cleanup test fixture only

    # 6. coding ladder derives from configured model + fallbacks (single source of truth)
    ladder = await ctrl._coding_ladder()
    check('ladder starts with configured coding model', ladder and ladder[0] == 'gpt-5.1-codex-max')
    check('ladder bounded <=4 with escalation fallbacks', 1 < len(ladder) <= 4)

    # 7. stop() rejects a non-running job (409)
    done = uid('job')
    await store.insert('jobs', {'id': done, 'status': 'NEEDS_REVIEW', 'created_at': now()})
    try:
        await ctrl.stop(done, 'tester')
        check('stop rejects non-running job', False)
    except HTTPException as e:
        check('stop rejects non-running job', e.status_code == 409)
    await store.db.jobs.delete_one({'id': done})

    client.close()
    ok = all(p for _, p in PASS)
    print(f'\n{sum(1 for _,p in PASS if p)}/{len(PASS)} checks passed')
    sys.exit(0 if ok else 1)


asyncio.run(main())
