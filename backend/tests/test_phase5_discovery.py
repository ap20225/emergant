"""Phase 5 deterministic tests: adapter normalize, dedup hierarchy, live recheck, source health, lease. NO network/provider/E2B."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from motor.motor_asyncio import AsyncIOMotorClient
from bounty.store import Store
from bounty import autonomy

SRC = 'p5-' + uuid.uuid4().hex[:8]


def _store():
    return Store(AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']])


def test_github_adapter_normalize_is_deterministic():
    item = {'id': 999, 'number': 42, 'html_url': 'https://github.com/owner/repo/issues/42',
            'repository_url': 'https://api.github.com/repos/owner/repo', 'title': 'Fix bug',
            'body': 'details', 'state': 'open', 'labels': [{'name': 'bounty'}, {'name': 'help wanted'}],
            'created_at': 'x', 'updated_at': 'y'}
    n = autonomy.GitHubIssueAdapter(_store()).normalize(item)
    assert n['repository'] == 'owner/repo' and n['issue_number'] == 42
    assert n['source'] == 'github' and n['reward'] is None
    assert n['labels'] == ['bounty', 'help wanted']


def test_duplicate_key_hierarchy():
    k = autonomy.canonical_duplicate_key
    # repo#issue wins and ignores title/bounty changes
    assert k('github', 'Owner/Repo', 5, 'idA', 'urlA') == k('github', 'owner/repo', 5, 'idB', 'urlB')
    # url variants canonicalize
    assert k('web', None, None, None, 'https://x.io/b/1/') == k('web', None, None, None, 'https://x.io/b/1?ref=z#frag')
    # falls back to source-id when no repo/issue
    assert k('src', None, None, 'ID-7') == 'src:id:ID-7'


def test_live_recheck_transitions():
    async def run():
        s = _store()
        await autonomy.Discovery(s).ingest(SRC, [{'repository': 'owner/repo', 'issue_number': 1, 'title': 't', 'description': 'd'}])
        opp = (await s.rows('bounty_opportunities', {'source': SRC}))[0]
        rc = autonomy.LiveRecheck(s)

        async def missing(repo, n): return {'exists': False}
        async def closed(repo, n): return {'exists': True, 'state': 'closed'}
        async def fresh(repo, n): return {'exists': True, 'state': 'open', 'locked': False}
        r_missing = await rc.recheck(opp['id'], fetcher=missing)
        r_closed = await rc.recheck(opp['id'], fetcher=closed)
        r_fresh = await rc.recheck(opp['id'], fetcher=fresh)
        await s.db.bounty_opportunities.delete_many({'source': SRC})
        return r_missing, r_closed, r_fresh
    m, c, f = asyncio.run(run())
    assert m['fresh'] is False and m['status'] == 'STALE_REQUIRES_RECHECK'
    assert c['fresh'] is False and 'not open' in c['reason']
    assert f['fresh'] is True


def test_source_failure_is_isolated():
    async def run():
        s = _store()

        class FailingAdapter:
            source_name = SRC
            async def fetch(self, config):
                raise RuntimeError('rate limited')
        res = await autonomy.Discovery(s).run_source(FailingAdapter())
        health = await s.one('source_health', {'id': SRC})
        await s.db.source_health.delete_many({'id': SRC})
        return res, health
    res, health = asyncio.run(run())
    assert res['ok'] is False and res['created'] == 0        # no exception propagated
    assert health['consecutive_failures'] >= 1 and health['status'] in ('DEGRADED', 'FAILING')


def test_scheduler_lease_blocks_concurrent_run():
    async def run():
        s = _store()
        # hold the lease into the future
        horizon = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        await s.db.system_settings.update_one({'id': 'scheduler-state'},
            {'$set': {'config': {'lease_until': horizon, 'holder': 'other'}}}, upsert=True)
        res = await autonomy.Scheduler(s).run_once(adapters=[])
        # release for cleanliness
        await s.db.system_settings.update_one({'id': 'scheduler-state'}, {'$set': {'config.lease_until': datetime.now(timezone.utc).isoformat()}})
        return res
    res = asyncio.run(run())
    assert res.get('skipped') == 'LEASE_HELD'
