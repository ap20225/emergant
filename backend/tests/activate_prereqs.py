"""Activate prerequisites: refresh routing policy + catalog, run REAL E2B runtime verification."""
import asyncio, os, sys, json
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from motor.motor_asyncio import AsyncIOMotorClient
from bounty.store import Store
from bounty.routing_policy import RoutingPolicy
from bounty.gateway import ExperientialGateway
from bounty.runner import RemoteRunner
from bounty.budget import RunBudget


async def main():
    client = AsyncIOMotorClient(os.environ['MONGO_URL'])
    store = Store(client[os.environ['DB_NAME']])
    await store.init()
    # 1. Reset stored routing policy to the cheapest-first defaults (glm/gemma).
    from bounty.store import now
    await store.db.system_settings.update_one({'id': 'model-routing'},
        {'$set': {'config': RoutingPolicy().model_dump(), 'updated_at': now()}}, upsert=True)
    policy = (await store.one('system_settings', {'id': 'model-routing'}))['config']
    print('ROUTING POLICY:', json.dumps({k: policy[k] for k in ['routine', 'coding', 'review']}))

    # 2. Refresh Experiential catalog (real, no inference).
    cat = await ExperientialGateway(store).catalog('system')
    est = cat.get('pricing_estimates', {})
    print('CATALOG models:', cat['model_count'], '| verified_at:', cat['catalog_verified_at'])
    for m in ['glm-4.7-flash', 'gemma-4-31b-it', 'glm-4.5-flash', 'gemma-4-26b-a4b-it']:
        print('  price', m, '=', est.get(m))

    # 3. REAL E2B runtime verification.
    attn = await RemoteRunner(store).verify('system')
    print('\nE2B ATTESTATION verified:', attn['verified'])
    for k in ['non_root', 'git', 'python', 'pip', 'node', 'dns_egress_blocked', 'raw_ip_egress_open']:
        print('  ', k, '=', attn[k])
    print('  checks:', json.dumps(attn['checks']))
    print('\nRUN_BUDGET:', await RunBudget(store).state())
    client.close()


asyncio.run(main())
