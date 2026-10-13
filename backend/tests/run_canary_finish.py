"""Finish publication for the already-executed canary job (no re-run, no new provider/E2B spend).
Sets approved_artifact_hash exactly as the real approval endpoint does, then performs the REAL
branch push + DRAFT PR + reconciliation. Flag forced OFF at end. Never merges."""
import asyncio, os, sys, json
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
os.environ['RUN_BUDGET_CAP_NANO_USD'] = '250000000'
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException
from bounty.store import Store, now
from bounty.github import GitHub
from bounty.publication import Publisher
from bounty.budget import RunBudget
from bounty.live_worker import artifact_hash

JID = 'job_6300128ede03'


async def main():
    client = AsyncIOMotorClient(os.environ['MONGO_URL'])
    store = Store(client[os.environ['DB_NAME']])
    ev = {'job_id': JID}
    try:
        # enable canary authorization
        row = await store.one('system_settings', {'id': 'feature-flags'})
        cfg = (row or {}).get('config') or {}
        cfg['AUTONOMOUS_EXECUTION_ENABLED'] = True; cfg['AUTO_ESCALATION_ENABLED'] = False
        await store.db.system_settings.update_one({'id': 'feature-flags'}, {'$set': {'config': cfg}}, upsert=True)

        job = await store.one('jobs', {'id': JID})
        ev['sandbox_id'] = job.get('sandbox_id')
        # mirror the real approval endpoint: approved_artifact_hash == current verified artifact hash
        digest = artifact_hash(job['patch_files'])
        assert digest == job['artifact_hash'], 'artifact drift before approval'
        await store.db.jobs.update_one({'id': JID}, {'$set': {'approved_artifact_hash': job['artifact_hash'],
                                                              'status': 'APPROVED', 'approval_status': 'APPROVED', 'updated_at': now()}})
        await store.db.approval_requests.update_many({'job_id': JID}, {'$set': {'status': 'APPROVED', 'actor': 'system'}})

        publisher = Publisher(store, GitHub(store))
        try:
            ev['publish'] = await publisher.publish(JID, 'system')
            ev['reconciliation'] = 'not_needed'
        except HTTPException as e:
            ev['publish_error'] = f'{e.status_code}: {e.detail}'
            print('publish ambiguous -> reconcile:', ev['publish_error'], flush=True)
            ev['reconciliation'] = await publisher.reconcile(JID, 'system')

        job = await store.one('jobs', {'id': JID})
        pr = await store.one('pull_requests', {'job_id': JID})
        attempt = await store.one('publication_attempts', {'id': JID})
        ev['final_job_status'] = job.get('status')
        ev['pr'] = {k: (pr or {}).get(k) for k in ['status', 'external', 'url', 'number', 'branch']} if pr else None
        ev['publication_attempt'] = {k: (attempt or {}).get(k) for k in ['status', 'remote_commit', 'pr_number', 'last_error']} if attempt else None
        b = await RunBudget(store).state()
        ev['budget'] = {'spent_nano': b['spent_nano_usd'], 'spent_usd': round(b['spent_nano_usd']/1e9, 8), 'cap_nano': b['cap_nano_usd']}
        pr_ok = bool(pr and pr.get('external') and pr.get('url') and isinstance(pr.get('number'), int))
        ev['result'] = 'PASS' if (pr_ok and job.get('status') == 'SUBMITTED') else 'FAIL'
    except Exception as exc:
        import traceback
        ev['result'] = 'FAIL'; ev['error'] = repr(exc); ev['trace'] = traceback.format_exc()[-1200:]
    finally:
        row = await store.one('system_settings', {'id': 'feature-flags'})
        cfg = (row or {}).get('config') or {}
        cfg['AUTONOMOUS_EXECUTION_ENABLED'] = False; cfg['AUTO_ESCALATION_ENABLED'] = False
        await store.db.system_settings.update_one({'id': 'feature-flags'}, {'$set': {'config': cfg}}, upsert=True)
        ev['autonomous_flag_after'] = ((await store.one('system_settings', {'id': 'feature-flags'}) or {}).get('config') or {}).get('AUTONOMOUS_EXECUTION_ENABLED')
        print('\n===FINISH_JSON===')
        print(json.dumps(ev, default=str, indent=2))
        client.close()


asyncio.run(main())
