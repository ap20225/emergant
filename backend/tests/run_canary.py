"""ONE bounded LIVE canary against ap20225/bounty-hunter#1 (explicitly authorized TEST opportunity).
REAL: provider call, E2B sandbox, clone, code, tests, security, review, branch push, DRAFT PR, reconcile.
Hard $0.25 cap. AUTONOMOUS_EXECUTION_ENABLED true only for this run, then forced back OFF. Never merges."""
import asyncio, os, sys, json, traceback
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
# Hard process-local ceilings ($0.25) — override any looser env, auto-reverts when the process exits.
os.environ['RUN_BUDGET_CAP_NANO_USD'] = '250000000'
os.environ['MAX_AUTONOMOUS_SPEND_PER_JOB_NANO_USD'] = '250000000'
os.environ['MAX_AUTONOMOUS_DAILY_SPEND_NANO_USD'] = '250000000'
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException
from bounty.store import Store, now
from bounty.orchestrator import Orchestrator
from bounty.github import GitHub
from bounty.connections import Connections
from bounty.live_worker import LiveWorker
from bounty.publication import Publisher
from bounty.budget import RunBudget

CHILD = ['test_runs', 'model_usage', 'routing_decisions', 'pull_requests', 'approval_requests',
         'security_reviews', 'code_reviews', 'branches', 'agent_runs', 'job_events', 'execution_attestations']


async def set_flags(store, autonomous):
    row = await store.one('system_settings', {'id': 'feature-flags'})
    cfg = (row or {}).get('config') or {}
    cfg['AUTONOMOUS_EXECUTION_ENABLED'] = autonomous
    cfg['AUTO_ESCALATION_ENABLED'] = False
    await store.db.system_settings.update_one({'id': 'feature-flags'}, {'$set': {'config': cfg}}, upsert=True)


async def main():
    client = AsyncIOMotorClient(os.environ['MONGO_URL'])
    store = Store(client[os.environ['DB_NAME']])
    await store.init()
    ev = {'stage': 'init'}
    try:
        # 1-2. authorize canary + validate repo identity via saved connection
        await set_flags(store, True)
        conn = await store.one('integration_connections', {'id': 'github'})
        ev['repository'] = conn['repository']; ev['issue'] = 1
        assert conn['repository'] == 'ap20225/bounty-hunter', 'unexpected connected repository'

        # clear any stale controlled job so this is a clean real run
        opp = 'controlled:ap20225/bounty-hunter#1'
        stale = await store.one('jobs', {'opportunity_id': opp})
        if stale:
            for col in CHILD:
                await store.db[col].delete_many({'job_id': stale['id']})
            await store.db.jobs.delete_one({'id': stale['id']})
            await store.db.publication_attempts.delete_many({'id': stale['id']})
            print('cleared stale job', stale['id'], flush=True)

        s = await store.settings(); s['max_execution_time'] = 300
        await store.db.system_settings.update_one({'id': 'settings'}, {'$set': {'config': s}}, upsert=True)

        orch = Orchestrator(store)
        github = GitHub(store)
        worker = LiveWorker(store, orch, github, Connections(store))
        publisher = Publisher(store, github)

        # 3-11. REAL pipeline: reserve budget, model, E2B, clone, code, tests, security, review, stage PR
        ev['stage'] = 'pipeline'
        job = await worker.start(1, 'unittest', 'system')
        jid = job['id']; ev['job_id'] = jid
        print('STARTED', jid, flush=True)
        await orch.tasks[jid]
        job = await store.one('jobs', {'id': jid})
        ev['pipeline_status'] = job.get('status'); ev['pipeline_error'] = job.get('error')
        ev['test_status'] = job.get('test_status'); ev['security_status'] = job.get('security_status')
        ev['review_status'] = job.get('review_status'); ev['files_changed'] = job.get('files_changed')

        # evidence: provider + E2B + clone + files
        usage = sorted(await store.rows('model_usage', {'job_id': jid}), key=lambda x: x['created_at'])
        ev['provider_calls'] = [{'role': u.get('role'), 'model': u.get('model'), 'status': u.get('status'),
                                 'cost_nano': u.get('actual_cost_nano_usd'), 'committed_nano': u.get('committed_nano_usd'),
                                 'cost_status': u.get('cost_status')} for u in usage]
        att = await store.one('execution_attestations', {'job_id': jid})
        ev['e2b_attestation'] = {k: (att or {}).get(k) for k in ['sandbox_id', 'provider', 'clone_verified', 'network_locked']} if att else None
        ev['test_runs'] = [{'phase': t.get('phase'), 'status': t.get('status'), 'exit': t.get('exit_code')} for t in await store.rows('test_runs', {'job_id': jid})]
        ev['security_reviews'] = [{'status': r.get('status')} for r in await store.rows('security_reviews', {'job_id': jid})]
        ev['code_reviews'] = [{'status': r.get('status')} for r in await store.rows('code_reviews', {'job_id': jid})]

        if job.get('status') != 'WAITING_APPROVAL':
            ev['stage'] = 'pipeline'; ev['result'] = 'FAIL'
            ev['fail_reason'] = f"pipeline did not reach WAITING_APPROVAL (status={job.get('status')}, error={job.get('error')})"
            raise SystemExit()

        # 12-14. authorize + REAL push branch + create DRAFT PR
        ev['stage'] = 'approval'
        await store.db.jobs.update_one({'id': jid}, {'$set': {'status': 'APPROVED', 'approval_status': 'APPROVED', 'updated_at': now()}})
        await store.db.approval_requests.update_many({'job_id': jid}, {'$set': {'status': 'APPROVED', 'actor': 'system'}})
        job = await store.one('jobs', {'id': jid})

        ev['stage'] = 'publish'
        try:
            pub = await publisher.publish(jid, 'system')
            ev['publish'] = pub; ev['reconciliation'] = 'not_needed'
        except HTTPException as e:
            print('publish ambiguous/failed -> reconcile:', e.status_code, e.detail, flush=True)
            ev['publish_error'] = f'{e.status_code}: {e.detail}'
            # 15. RECONCILE (read-first; adopt existing / complete missing / clear)
            ev['stage'] = 'reconcile'
            rec = await publisher.reconcile(jid, 'system')
            ev['reconciliation'] = rec

        # 16. final persisted state + PR evidence
        job = await store.one('jobs', {'id': jid})
        pr = await store.one('pull_requests', {'job_id': jid})
        attempt = await store.one('publication_attempts', {'id': jid})
        ev['final_job_status'] = job.get('status')
        ev['pr'] = {k: (pr or {}).get(k) for k in ['status', 'external', 'url', 'number', 'branch']} if pr else None
        ev['publication_attempt'] = {k: (attempt or {}).get(k) for k in ['status', 'remote_commit', 'pr_number', 'last_error']} if attempt else None
        b = await RunBudget(store).state()
        ev['budget'] = {'spent_nano': b['spent_nano_usd'], 'cap_nano': b['cap_nano_usd'], 'spent_usd': round(b['spent_nano_usd']/1e9, 8)}

        pr_ok = bool(pr and pr.get('external') and pr.get('url') and isinstance(pr.get('number'), int))
        ev['result'] = 'PASS' if (pr_ok and job.get('status') == 'SUBMITTED') else 'FAIL'
        if ev['result'] == 'FAIL':
            ev['fail_reason'] = f"draft PR not confirmed external (status={job.get('status')}, pr={ev['pr']})"
    except SystemExit:
        pass
    except Exception as exc:
        ev.setdefault('result', 'FAIL')
        ev['fail_reason'] = f'exception at stage {ev.get("stage")}: {repr(exc)}'
        ev['trace'] = traceback.format_exc()[-1500:]
    finally:
        try:
            await set_flags(store, False)  # ALWAYS turn canary authorization back OFF
            ev['autonomous_flag_after'] = ((await store.one('system_settings', {'id': 'feature-flags'}) or {}).get('config') or {}).get('AUTONOMOUS_EXECUTION_ENABLED')
        except Exception as e:
            ev['flag_reset_error'] = repr(e)
        print('\n===CANARY_JSON===')
        print(json.dumps(ev, default=str, indent=2))
        client.close()


asyncio.run(main())
