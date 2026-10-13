"""Static/unit verification for the production-hardening pass.

NEVER executes a bounty, makes a paid model call, or touches real credentials. Pure logic +
fake in-memory store. Covers: (1) credential env fallback, (2) production origin policy,
(3) 24/7 flag env overlay, (4) orphaned-QUEUED recovery + budget release, (5) candidate backoff.
"""
import asyncio
import os

import pytest


# ---------------------------------------------------------------- fakes
class FakeCollection:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def _rows(self):
        return self.store.data.setdefault(self.name, [])

    def _match(self, doc, query):
        for k, v in query.items():
            if isinstance(v, dict):
                if '$in' in v and doc.get(k) not in v['$in']:
                    return False
                if '$gt' in v and not (doc.get(k) is not None and doc.get(k) > v['$gt']):
                    return False
                if '$ne' in v and doc.get(k) == v['$ne']:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    async def update_one(self, query, update, upsert=False):
        rows = self._rows()
        for doc in rows:
            if self._match(doc, query):
                for k, val in update.get('$set', {}).items():
                    doc[k] = val
                for k, val in update.get('$inc', {}).items():
                    doc[k] = doc.get(k, 0) + val
                return
        if upsert:
            doc = {}
            for k, v in query.items():
                if not isinstance(v, dict):
                    doc[k] = v
            for k, val in update.get('$set', {}).items():
                doc[k] = val
            for k, val in update.get('$inc', {}).items():
                doc[k] = doc.get(k, 0) + val
            rows.append(doc)

    async def delete_one(self, query):
        rows = self._rows()
        for i, doc in enumerate(rows):
            if self._match(doc, query):
                rows.pop(i)
                return

    async def count_documents(self, query):
        return sum(1 for d in self._rows() if self._match(d, query))


class FakeStore:
    def __init__(self):
        self.data = {}
        self.audits = []

    class _DB:
        def __init__(self, store):
            self._store = store

        def __getattr__(self, name):
            return FakeCollection(self._store, name)

    @property
    def db(self):
        return FakeStore._DB(self)

    async def one(self, collection, query):
        for d in self.data.get(collection, []):
            if FakeCollection(self, collection)._match(d, query):
                return d
        return None

    async def rows(self, collection, query=None, limit=250):
        query = query or {}
        return [d for d in self.data.get(collection, []) if FakeCollection(self, collection)._match(d, query)][:limit]

    async def insert(self, collection, doc):
        self.data.setdefault(collection, []).append(doc)
        return doc

    async def audit(self, *a, **k):
        self.audits.append((a, k))


# ---------------------------------------------------------------- 1. credential env fallback
def test_secret_vault_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv('PENNILESS_SECRETS_DIR', str(tmp_path))
    monkeypatch.setenv('GITHUB_TOKEN', 'env-github-value')
    monkeypatch.setenv('E2B_API_KEY', 'env-e2b-value')
    from bounty.secrets import SecretVault
    v = SecretVault()
    assert v.has('github') and v.has('e2b')
    assert v.get('github') == 'env-github-value'
    assert v.get('e2b') == 'env-e2b-value'


def test_secret_vault_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv('PENNILESS_SECRETS_DIR', str(tmp_path))
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    from bounty.secrets import SecretVault
    v = SecretVault()
    assert v.has('github') is False
    v.put('github', 'file-stored-token')
    assert v.has('github') is True
    assert v.get('github') == 'file-stored-token'


def test_experiential_key_env_fallback(monkeypatch):
    monkeypatch.setenv('EXPLABS_API_KEY', 'env-explabs')
    from bounty.secrets import experiential_key
    assert experiential_key() == 'env-explabs'


# ---------------------------------------------------------------- 2. production origin policy
def test_production_origin_exact_and_slug():
    from bounty.origin_policy import origin_allowed, cors_origin_regex
    import re
    prod = ['https://bounty-hunter.emergent.host']
    assert origin_allowed('https://bounty-hunter.emergent.host', prod)                 # exact
    assert origin_allowed('https://bounty-hunter.emergent.cloud', prod)                # sibling prod form
    assert origin_allowed('https://bounty-hunter.preview.emergentagent.com', prod)     # sibling preview form
    assert origin_allowed('https://evil.example.com', prod) is False
    assert origin_allowed('https://other-app.emergent.host', prod) is False            # different slug
    rx = cors_origin_regex(prod)
    assert re.match(rx, 'https://bounty-hunter.emergent.host')
    assert re.match(rx, 'https://bounty-hunter.cluster-7.preview.emergentcf.cloud')
    assert re.match(rx, 'https://evil.example.com') is None


def test_no_wildcard_when_unconfigured():
    from bounty.origin_policy import cors_origin_regex, configured_origins
    assert cors_origin_regex([]) is None


# ---------------------------------------------------------------- 3. 24/7 flag env overlay
def test_flag_env_overlay(monkeypatch):
    monkeypatch.setenv('AUTONOMOUS_EXECUTION_ENABLED', 'true')
    monkeypatch.setenv('SCHEDULER_BACKGROUND_ENABLED', 'true')
    from bounty.autonomy import _env_flag_defaults
    d = _env_flag_defaults()
    assert d['AUTONOMOUS_EXECUTION_ENABLED'] is True
    assert d['SCHEDULER_BACKGROUND_ENABLED'] is True


def test_db_toggle_overrides_env(monkeypatch):
    monkeypatch.setenv('AUTONOMOUS_EXECUTION_ENABLED', 'true')
    from bounty.autonomy import get_flags
    store = FakeStore()
    store.data['system_settings'] = [{'id': 'feature-flags', 'config': {'AUTONOMOUS_EXECUTION_ENABLED': False}}]
    flags = asyncio.get_event_loop().run_until_complete(get_flags(store))
    assert flags['AUTONOMOUS_EXECUTION_ENABLED'] is False  # owner DB toggle wins over env


# ---------------------------------------------------------------- 4. budget release + orphan recovery
def test_budget_release_floor(monkeypatch):
    monkeypatch.setenv('MAX_AUTONOMOUS_SPEND_PER_JOB_NANO_USD', '500000000')
    monkeypatch.setenv('MAX_AUTONOMOUS_DAILY_SPEND_NANO_USD', '1000000000')
    from bounty.authorization import AutonomousBudget
    from bounty.store import now
    store = FakeStore()
    d = now()[:10]
    store.data['autonomous_budget'] = [{'id': d, 'reserved_nano_usd': 300, 'spent_nano_usd': 0}]
    b = AutonomousBudget(store)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(b.release('job1', 500, date=d))     # cannot release more than reserved
    assert store.data['autonomous_budget'][0]['reserved_nano_usd'] == 0
    loop.run_until_complete(b.release('job1', 500, date=d))     # idempotent / floored
    assert store.data['autonomous_budget'][0]['reserved_nano_usd'] == 0


def test_orphaned_queued_recovery(monkeypatch):
    from bounty.execution_controller import recover_orphaned_executions
    from bounty.store import now
    store = FakeStore()
    d = now()[:10]
    store.data['autonomous_budget'] = [{'id': d, 'reserved_nano_usd': 250000000, 'spent_nano_usd': 0}]
    store.data['jobs'] = [
        {'id': 'job_queued', 'status': 'QUEUED', 'budget_reservation_nano_usd': 250000000,
         'budget_reservation_date': d, 'created_at': now()},
        {'id': 'job_live', 'status': 'CODING'},
    ]
    n = asyncio.get_event_loop().run_until_complete(recover_orphaned_executions(store))
    assert n == 1
    queued = next(j for j in store.data['jobs'] if j['id'] == 'job_queued')
    assert queued['status'] == 'NEEDS_REVIEW'
    assert store.data['autonomous_budget'][0]['reserved_nano_usd'] == 0           # reservation released
    assert next(j for j in store.data['jobs'] if j['id'] == 'job_live')['status'] == 'CODING'  # untouched


# ---------------------------------------------------------------- 5. candidate backoff
def test_candidate_backoff_cooldown_grows_and_clears():
    from bounty.autonomous_driver import AutonomousDriver, BACKOFF_BASE_SECONDS

    class _Orch:
        tasks = {}

    store = FakeStore()
    drv = AutonomousDriver(store, controller=None, orchestrator=_Orch())
    loop = asyncio.get_event_loop()

    loop.run_until_complete(drv._record_failure('opp1', 'ISSUE_NOT_OPEN'))
    row1 = loop.run_until_complete(store.one('driver_candidate_backoff', {'id': 'opp1'}))
    assert row1['failures'] == 1
    cooldown1 = str(row1['cooldown_until'])   # snapshot (fake store returns live refs)
    assert 'opp1' in loop.run_until_complete(drv._backoff_ids())   # currently cooling down

    loop.run_until_complete(drv._record_failure('opp1', 'ISSUE_NOT_OPEN'))
    row2 = loop.run_until_complete(store.one('driver_candidate_backoff', {'id': 'opp1'}))
    assert row2['failures'] == 2
    assert row2['cooldown_until'] > cooldown1         # exponential growth, not permanent

    loop.run_until_complete(drv._clear_failure('opp1'))
    assert loop.run_until_complete(store.one('driver_candidate_backoff', {'id': 'opp1'})) is None
    assert 'opp1' not in loop.run_until_complete(drv._backoff_ids())
