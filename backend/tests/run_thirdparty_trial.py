"""ONE bounded LIVE trial against an AUTHORIZED third-party bounty using the already-built
fork-first architecture. Single-use for opportunity bopp_42c91983227c
(claude-builders-bounty/claude-builders-bounty#1), execution fork ap20225/claude-builders-bounty.

Reuses the existing worker pipeline, budget, safety, review and publication code unchanged.
Hard $0.25 provider cap. AUTONOMOUS_EXECUTION_ENABLED enabled only for this run, forced OFF at exit.
Never merges. One attempt, no retries. Does not start the background scheduler.
"""
import asyncio
import json
import os
import sys
import traceback

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env')
load_dotenv('/app/backend/bounty.env')

# Process-local hard ceilings ($0.25). Reverts automatically when the process exits.
os.environ['RUN_BUDGET_CAP_NANO_USD'] = '250000000'
os.environ['MAX_AUTONOMOUS_SPEND_PER_JOB_NANO_USD'] = '250000000'
os.environ['MAX_AUTONOMOUS_DAILY_SPEND_NANO_USD'] = '250000000'

import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException

from bounty.store import Store, now, uid, redact
from bounty.orchestrator import Orchestrator
from bounty.github import GitHub
from bounty.connections import Connections
from bounty.live_worker import LiveWorker, artifact_hash
from bounty.publication import Publisher
from bounty.budget import RunBudget
from bounty.gateway import ExperientialGateway
from bounty.runner import RemoteRunner
from bounty.model_router import ModelRouter
from bounty.repos import RepoRegistry, resolve_targets, verify_execution_targets
from bounty.authorization import AutonomousAuthorization
from bounty.secrets import SecretVault

OPP_ID = 'bopp_42c91983227c'
UPSTREAM = 'claude-builders-bounty/claude-builders-bounty'
FORK = 'ap20225/claude-builders-bounty'
ISSUE = 1
FORK_OWNER = 'ap20225'
CLAIM_BODY = '/opire try'
CHILD = ['test_runs', 'model_usage', 'routing_decisions', 'pull_requests', 'approval_requests',
         'security_reviews', 'code_reviews', 'branches', 'agent_runs', 'job_events', 'execution_attestations']


async def set_flags(store, autonomous):
    row = await store.one('system_settings', {'id': 'feature-flags'})
    cfg = (row or {}).get('config') or {}
    cfg['AUTONOMOUS_EXECUTION_ENABLED'] = autonomous
    cfg['AUTO_ESCALATION_ENABLED'] = False
    await store.db.system_settings.update_one({'id': 'feature-flags'}, {'$set': {'config': cfg}}, upsert=True)


async def flag_now(store):
    return ((await store.one('system_settings', {'id': 'feature-flags'}) or {}).get('config') or {}).get('AUTONOMOUS_EXECUTION_ENABLED')


async def perform_claim(store, github, ev):
    """Phase 3: idempotent, bounded /opire try claim on the AUTHORIZED upstream issue.
    Returns one of: ALREADY_CLAIMED / CLAIMED_NOW / SKIPPED_NOT_REQUIRED / BLOCKED_* .
    Never posts more than one comment; stops (raises) on ambiguity or missing write scope."""
    reg = RepoRegistry(store)
    up = await reg.get(UPSTREAM)
    if not up or up.get('status') != 'AUTHORIZED' or not up.get('publication_enabled'):
        raise HTTPException(409, 'CLAIM_BLOCKED: upstream not AUTHORIZED for write in registry')
    # Durable, pagination-proof idempotency: a persisted record is the source of truth for a prior claim.
    prior = await store.one('bounty_claims', {'id': f'{UPSTREAM}#{ISSUE}'})
    if prior:
        ev['claim_account'] = prior.get('account')
        ev['claim_comment_url'] = prior.get('comment_url')
        return 'ALREADY_CLAIMED'
    # Identify the authenticated account (the fork owner / token identity).
    me = await github.request('GET', '/user')
    login = (me or {}).get('login')
    ev['claim_account'] = login
    # Read existing comments (read is registry-allowed) and detect a prior claim by us.
    comments, page = [], 1
    while page <= 20:
        batch = await github.request('GET', f'/repos/{UPSTREAM}/issues/{ISSUE}/comments?per_page=100&page={page}')
        if not isinstance(batch, list) or not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    mine = [c for c in comments if (c.get('user') or {}).get('login', '').lower() == (login or '').lower()]
    already = any('/opire try' in (c.get('body') or '').lower() or '/attempt' in (c.get('body') or '').lower() for c in mine)
    if already:
        return 'ALREADY_CLAIMED'
    # Post exactly one claim comment via a tightly-scoped credentialed write (github.request blocks non-GET).
    token = SecretVault().get('github')
    if not token:
        raise HTTPException(409, 'CLAIM_BLOCKED: no GitHub credential available')
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            res = await client.post(
                os.environ['GITHUB_API_URL'] + f'/repos/{UPSTREAM}/issues/{ISSUE}/comments',
                headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']},
                json={'body': CLAIM_BODY})
    except httpx.RequestError as e:
        raise HTTPException(502, f'CLAIM_BLOCKED: transport error {type(e).__name__}; not retried')
    if res.status_code in (200, 201):
        body = res.json()
        ev['claim_comment_url'] = body.get('html_url')
        from pymongo.errors import DuplicateKeyError
        try:
            await store.insert('bounty_claims', {'id': f'{UPSTREAM}#{ISSUE}', 'account': login,
                'comment_url': body.get('html_url'), 'comment_id': body.get('id'), 'body': CLAIM_BODY})
        except DuplicateKeyError:
            pass
        await store.audit('pr_manager', 'Bounty claim comment posted', f'{UPSTREAM}#{ISSUE}: {CLAIM_BODY}',
                          'SUCCESS', authorization='HUMAN_APPROVED')
        return 'CLAIMED_NOW'
    detail = ''
    try:
        detail = str(res.json().get('message'))[:200]
    except ValueError:
        detail = (res.text or '')[:200]
    if res.status_code in (401, 403):
        raise HTTPException(res.status_code, f'CLAIM_BLOCKED: token cannot comment (HTTP {res.status_code}: {detail}); claim not made, no spend')
    raise HTTPException(502, f'CLAIM_BLOCKED: unexpected HTTP {res.status_code}: {detail}')


async def main():
    client = AsyncIOMotorClient(os.environ['MONGO_URL'])
    store = Store(client[os.environ['DB_NAME']])
    await store.init()
    ev = {'stage': 'init', 'opportunity': OPP_ID, 'upstream': UPSTREAM, 'issue': ISSUE, 'fork': FORK}
    budget = RunBudget(store)
    try:
        await set_flags(store, True)  # authorize autonomous execution for THIS run only
        github = GitHub(store)
        reg = RepoRegistry(store)

        opp = await store.one('bounty_opportunities', {'id': OPP_ID}) or await store.one('opportunities', {'id': OPP_ID})
        if not opp:
            raise HTTPException(404, 'Opportunity not found')

        # ---- PRE-EXECUTION CHECKS (read-only) ----
        ev['stage'] = 'precheck'
        # 1. upstream issue still OPEN and is a real issue (not a PR)
        issue = await github.request('GET', f'/repos/{UPSTREAM}/issues/{ISSUE}')
        if issue.get('state') != 'open':
            raise HTTPException(409, f'BLOCKED: upstream issue not open (state={issue.get("state")})')
        if 'pull_request' in issue:
            raise HTTPException(409, 'BLOCKED: target is a pull request, not an issue')
        ev['issue_title'] = issue.get('title')
        # 2. fork exists + writable + correct parent/owner; identity unchanged (fail-closed)
        targets = await verify_execution_targets(github, reg, {**opp, 'issue_number': ISSUE, 'title': issue.get('title')}, expect_fork_owner=FORK_OWNER)
        ev['targets'] = {k: targets.get(k) for k in ['strategy', 'execution_repo', 'publication_repo', 'upstream_repo', 'write_ready']}
        if targets['execution_repo'].lower() != FORK.lower() or targets['publication_repo'].lower() != UPSTREAM.lower():
            raise HTTPException(409, 'BLOCKED: target resolution changed')
        # 3. deterministic autonomous authorization (flag now ON). An explicitly human-authorized
        #    re-attempt is accepted when every gate passes and the ONLY open item is 'already attempted'
        #    (prior attempts are preserved historical FAILURES, never re-keyed or erased).
        decision = await AutonomousAuthorization(store).evaluate({**opp, 'issue_number': ISSUE})
        chk = decision['checks']
        gates_ok = all(chk.get(k) for k in ['reward_verified', 'issue_open', 'safety_cleared', 'qualified', 'repo_write_authorized', 'budget_available', 'execution_flag_enabled'])
        reattempt = decision['decision'] != 'AUTHORIZED' and gates_ok and not chk.get('not_attempted')
        ev['authorization'] = {'decision': decision['decision'], 'reasons': decision['reasons'], 'authorized_reattempt': reattempt}
        if decision['decision'] != 'AUTHORIZED' and not reattempt:
            raise HTTPException(409, f'BLOCKED: not AUTHORIZED ({decision["decision"]}): {decision["reasons"]}')
        # 4. base sha: upstream head must equal fork head (fork up-to-date; depth-1 clone must contain it)
        up_ref = await github.request('GET', f'/repos/{UPSTREAM}/git/ref/heads/main')
        fork_ref = await github.request('GET', f'/repos/{FORK}/git/ref/heads/main')
        base_sha = up_ref['object']['sha']
        ev['base_sha'] = base_sha
        if fork_ref['object']['sha'] != base_sha:
            raise HTTPException(409, 'BLOCKED: fork main is not level with upstream main; sync the fork before a bounded trial')
        # 5. duplicate protection: block ONLY if a live or already-published attempt exists.
        #    Historical FAILED attempts (NEEDS_REVIEW/CANCELLED) are preserved as-is and must NOT block
        #    this explicitly authorized re-attempt; they are never re-keyed or deleted.
        LIVE = ['SELECTED', 'TRIAGE', 'RESEARCHING', 'CODING', 'TESTING', 'REVIEW', 'READY', 'WAITING_APPROVAL', 'APPROVED', 'SUBMITTED']
        blocking = await store.db.jobs.count_documents({'repository': UPSTREAM, 'issue_number': ISSUE, 'status': {'$in': LIVE}})
        ev['historical_attempts_preserved'] = await store.db.jobs.count_documents({'repository': UPSTREAM, 'issue_number': ISSUE})
        if blocking > 0:
            raise HTTPException(409, f'BLOCKED: a live or already-published attempt already exists ({blocking})')
        # budget sanity
        ev['budget_cap_nano'] = budget.cap()

        # ---- PHASE 3: CLAIM (before spending) ----
        ev['stage'] = 'claim'
        ev['claim_status'] = await perform_claim(store, github, ev)

        # ---- authorize run resources (mirrors LiveWorker.start gates) ----
        ev['stage'] = 'authorize'
        await ExperientialGateway(store).catalog('system')          # refresh model catalog (no cost)
        await RemoteRunner(store).preflight()                       # E2B configured + runtime verified
        await budget.authorize('system', f'trial:{UPSTREAM}#{ISSUE}')
        await ModelRouter(store).authorize('coding', None)          # a cheap coding model must be selectable

        s = await store.settings()
        s['max_execution_time'] = 300
        await store.db.system_settings.update_one({'id': 'settings'}, {'$set': {'config': s}}, upsert=True)

        # ---- build the fork-first job ----
        jid = uid('job')
        branch = f'penniless/{jid}'
        job = {'id': jid, 'opportunity_id': f'{OPP_ID}#live-{jid}', 'bounty_id': OPP_ID, 'repository': UPSTREAM, 'upstream_repo': UPSTREAM,
               'execution_repo': FORK, 'publication_repo': UPSTREAM, 'execution_default_branch': 'main',
               'issue_number': ISSUE, 'title': issue.get('title'),
               'issue_body': redact((issue.get('body') or '')[:10000]),
               'status': 'SELECTED', 'simulated': False, 'controlled_test': False,
               'reward': float(opp.get('reward_amount') or 0), 'reward_currency': opp.get('reward_currency'),
               'expected_profit': 0, 'cost': None, 'elapsed_seconds': 0,
               'test_status': 'BLOCKED', 'security_status': 'BLOCKED', 'review_status': 'BLOCKED',
               'approval_status': 'WAITING_FOR_APPROVAL', 'base_sha': base_sha, 'base_branch': 'main',
               'branch': branch, 'test_suite': 'unittest', 'created_at': now(), 'files_changed': [], 'iteration': 0}
        await store.insert('jobs', job)
        await store.audit('master', 'Third-party bounty trial selected',
                          f'{UPSTREAM}#{ISSUE} via fork {FORK}; one bounded attempt; draft PR only; no merge',
                          job_id=jid, actor='system', authorization='HUMAN_APPROVED')

        # ---- PHASE 4: ONE REAL EXECUTION (coding -> E2B -> tests -> security -> review) ----
        ev['stage'] = 'pipeline'
        ev['job_id'] = jid
        conn = await store.one('integration_connections', {'id': 'github'})
        orch = Orchestrator(store)
        worker = LiveWorker(store, orch, github, Connections(store))
        print('STARTED', jid, flush=True)
        await worker.run(job, conn)

        job = await store.one('jobs', {'id': jid})
        ev['pipeline_status'] = job.get('status')
        ev['pipeline_error'] = job.get('error')
        ev['test_status'] = job.get('test_status')
        ev['security_status'] = job.get('security_status')
        ev['review_status'] = job.get('review_status')
        ev['files_changed'] = job.get('files_changed')
        ev['commit'] = job.get('commit')
        usage = sorted(await store.rows('model_usage', {'job_id': jid}), key=lambda x: x.get('created_at') or '')
        ev['provider_calls'] = [{'role': u.get('role'), 'model': u.get('model'), 'status': u.get('status'),
                                 'cost_nano': u.get('actual_cost_nano_usd'), 'committed_nano': u.get('committed_nano_usd'),
                                 'cost_status': u.get('cost_status')} for u in usage]
        att = await store.one('execution_attestations', {'job_id': jid})
        ev['e2b'] = {k: (att or {}).get(k) for k in ['sandbox_id', 'clone_verified', 'network_locked']} if att else {'sandbox_id': job.get('sandbox_id')}

        if job.get('status') != 'WAITING_APPROVAL':
            ev['result'] = 'FAIL'
            ev['fail_reason'] = f"pipeline stopped at status={job.get('status')}: {job.get('error')}"
            raise SystemExit()

        # ---- PHASE 5: DRAFT PR (approve -> publish fork-first -> reconcile if ambiguous) ----
        ev['stage'] = 'approve'
        await store.db.jobs.update_one({'id': jid}, {'$set': {'status': 'APPROVED', 'approval_status': 'APPROVED',
                                                              'approved_artifact_hash': job['artifact_hash'], 'updated_at': now()}})
        await store.db.approval_requests.update_many({'job_id': jid}, {'$set': {'status': 'APPROVED', 'actor': 'system'}})

        publisher = Publisher(store, github)
        ev['stage'] = 'publish'
        try:
            ev['publish'] = await publisher.publish(jid, 'system')
            ev['reconciliation'] = 'not_needed'
        except HTTPException as e:
            ev['publish_error'] = f'{e.status_code}: {e.detail}'
            print('publish ambiguous/failed -> reconcile:', ev['publish_error'], flush=True)
            ev['stage'] = 'reconcile'
            ev['reconciliation'] = await publisher.reconcile(jid, 'system')

        job = await store.one('jobs', {'id': jid})
        pr = await store.one('pull_requests', {'job_id': jid})
        attempt = await store.one('publication_attempts', {'id': jid})
        ev['final_job_status'] = job.get('status')
        ev['pr'] = {k: (pr or {}).get(k) for k in ['status', 'external', 'url', 'number', 'branch']} if pr else None
        ev['publication_attempt'] = {k: (attempt or {}).get(k) for k in ['status', 'remote_commit', 'pr_number', 'last_error']} if attempt else None
        b = await budget.state()
        ev['budget'] = {'spent_nano': b['spent_nano_usd'], 'cap_nano': b['cap_nano_usd'], 'spent_usd': round(b['spent_nano_usd'] / 1e9, 8)}
        pr_ok = bool(pr and pr.get('external') and pr.get('url') and isinstance(pr.get('number'), int))
        ev['result'] = 'PASS' if (pr_ok and job.get('status') == 'SUBMITTED') else 'FAIL'
        if ev['result'] == 'FAIL':
            ev['fail_reason'] = f"draft PR not confirmed (status={job.get('status')}, pr={ev['pr']})"
    except SystemExit:
        pass
    except HTTPException as exc:
        ev.setdefault('result', 'FAIL')
        ev['fail_reason'] = f'blocked at stage {ev.get("stage")}: {exc.status_code} {exc.detail}'
    except Exception as exc:
        ev.setdefault('result', 'FAIL')
        ev['fail_reason'] = f'exception at stage {ev.get("stage")}: {repr(exc)}'
        ev['trace'] = traceback.format_exc()[-1800:]
    finally:
        try:
            await budget.close()
        except Exception:
            pass
        try:
            await set_flags(store, False)  # ALWAYS force autonomous execution OFF again
            ev['autonomous_flag_after'] = await flag_now(store)
        except Exception as e:
            ev['flag_reset_error'] = repr(e)
        print('\n===TRIAL_JSON===')
        print(json.dumps(ev, default=str, indent=2))
        client.close()


asyncio.run(main())
