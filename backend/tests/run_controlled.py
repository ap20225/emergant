"""Controlled REAL pipeline run against issue #1 under the $1 cap. Stops at draft-PR staging."""
import asyncio, os, sys, json, traceback
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from motor.motor_asyncio import AsyncIOMotorClient
from bounty.store import Store
from bounty.orchestrator import Orchestrator
from bounty.github import GitHub
from bounty.connections import Connections
from bounty.live_worker import LiveWorker
from bounty.budget import RunBudget

CHILD = ['test_runs', 'model_usage', 'routing_decisions', 'pull_requests', 'approval_requests',
         'security_reviews', 'code_reviews', 'branches', 'agent_runs', 'job_events']


async def main():
    client = AsyncIOMotorClient(os.environ['MONGO_URL'])
    store = Store(client[os.environ['DB_NAME']])
    await store.init()
    opp = 'controlled:ap20225/bounty-hunter#1'
    stale = await store.one('jobs', {'opportunity_id': opp})
    if stale:
        jid = stale['id']
        for col in CHILD:
            await store.db[col].delete_many({'job_id': jid})
        await store.db.jobs.delete_one({'id': jid})
        print('cleared stale job', jid)
    # allow enough wall-clock for the real remote pipeline
    s = await store.settings()
    s['max_execution_time'] = 300
    await store.db.system_settings.update_one({'id': 'settings'}, {'$set': {'config': s}}, upsert=True)

    orch = Orchestrator(store)
    worker = LiveWorker(store, orch, GitHub(store), Connections(store))
    try:
        job = await worker.start(1, 'unittest', 'system')
        print('STARTED job', job['id'])
        await orch.tasks[job['id']]
    except Exception as exc:
        print('START/RUN ERROR:', repr(exc))
        traceback.print_exc()
        client.close(); return

    job = await store.one('jobs', {'id': job['id']})
    print('\n===== RESULT =====')
    print('status        :', job.get('status'))
    print('error         :', job.get('error'))
    print('triage        :', json.dumps(job.get('triage'))[:300])
    print('test_status   :', job.get('test_status'))
    print('security      :', job.get('security_status'))
    print('review        :', job.get('review_status'))
    print('files_changed :', job.get('files_changed'))
    print('elapsed_s     :', job.get('elapsed_seconds'))
    print('\n--- model_usage ---')
    for u in sorted(await store.rows('model_usage', {'job_id': job['id']}), key=lambda x: x['created_at']):
        print(f"  {u.get('role'):9} {u.get('model'):16} status={u.get('status')} "
              f"tok(in/out)={u.get('prompt_tokens')}/{u.get('completion_tokens')} "
              f"cost_nano={u.get('actual_cost_nano_usd')} committed={u.get('committed_nano_usd')} "
              f"cost_status={u.get('cost_status')} lat={u.get('latency_ms')}ms")
    print('\n--- test_runs ---')
    for t in await store.rows('test_runs', {'job_id': job['id']}):
        print(f"  {t.get('phase'):9} {t.get('status')} exit={t.get('exit_code')}")
    print('\n--- reviews ---')
    for r in await store.rows('security_reviews', {'job_id': job['id']}):
        print('  security', r.get('status'), r.get('findings'))
    for r in await store.rows('code_reviews', {'job_id': job['id']}):
        print('  code    ', r.get('status'), r.get('findings'))
    pr = await store.one('pull_requests', {'job_id': job['id']})
    print('\n--- pull_request ---')
    print('  ', {k: pr.get(k) for k in ['status', 'branch', 'commit', 'external', 'url', 'title']} if pr else None)
    b = await RunBudget(store).state()
    print('\n--- BUDGET ---')
    print('  spent_nano:', b['spent_nano_usd'], f"(${b['spent_nano_usd']/1e9:.8f})",
          '| cap:', b['cap_nano_usd'], '| remaining_nano:', b['cap_nano_usd'] - b['spent_nano_usd'])
    if pr and pr.get('patch_files' if False else 'body'):
        print('\n--- PR body ---\n', pr.get('body'))
    print('\n--- diff ---\n', (job.get('diff') or '')[:1500])
    client.close()


asyncio.run(main())
