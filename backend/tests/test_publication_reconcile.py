"""Deterministic publication-reconcile tests. NO network / GitHub / provider / E2B.
Stubs the GitHub client and Publisher.write(). Uses real Mongo (MONGO_URL/DB_NAME) and
cleans up its own seed data. Verifies read-first recovery, PR adoption, missing-PR completion,
no-remote clearing, idempotency, and that safety gates + no-merge invariants hold."""
import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorClient
from bounty.store import Store, now
from bounty.publication import Publisher
from bounty.live_worker import artifact_hash
from bounty.secrets import SecretVault

REMOTE = 'a' * 40
PATCH = {'reconcile_probe.py': 'def probe():\n    return 42\n'}


def _store():
    return Store(AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']])


async def _prereqs_ok(s):
    c, st = await s.controls(), await s.settings()
    if any(c.get(k) for k in ['global_stop', 'agents_stopped', 'submissions_disabled', 'integrations_revoked']):
        return None
    if st.get('mode') != 'ASSISTED':
        return None
    conn = await s.one('integration_connections', {'id': 'github'})
    if not conn or conn.get('status') != 'CONNECTED' or not SecretVault().has('github'):
        return None
    return conn['repository']


class FakeGitHub:
    def __init__(self, pulls=None, ref=None, raise_ref=False):
        self.pulls, self.ref, self.raise_ref = pulls or [], ref, raise_ref

    async def request(self, method, path):
        if '/pulls' in path:
            return self.pulls
        if '/git/ref/heads/' in path:
            if self.raise_ref:
                raise HTTPException(404, 'GitHub repository or issue not found or not authorized')
            return self.ref
        return {}


class StubPublisher(Publisher):
    async def write(self, job, path, payload):
        await self.gate(job)
        return {'draft': True, 'number': 4242, 'html_url': 'https://github.com/x/y/pull/4242', 'head': {'sha': REMOTE}}


async def _seed(s, repo, job_id, attempt_status='FAILED_OR_AMBIGUOUS', with_remote=True):
    digest = artifact_hash(PATCH)
    await s.insert('jobs', {'id': job_id, 'repository': repo, 'issue_number': 1, 'simulated': False,
                            'status': 'APPROVED', 'approval_status': 'APPROVED', 'test_status': 'PASS',
                            'security_status': 'PASS', 'review_status': 'APPROVE', 'branch': f'penniless/{job_id}',
                            'base_branch': 'main', 'base_sha': 'e' * 40, 'patch_files': PATCH,
                            'artifact_hash': digest, 'approved_artifact_hash': digest, 'pr_id': f'pr_{job_id}'})
    await s.insert('pull_requests', {'id': f'pr_{job_id}', 'job_id': job_id, 'status': 'AWAITING_PUBLICATION',
                                     'external': False, 'title': 'probe', 'body': 'probe body', 'branch': f'penniless/{job_id}'})
    doc = {'id': job_id, 'status': attempt_status, 'actor': 'test', 'artifact_hash': digest}
    if with_remote:
        doc['remote_commit'] = REMOTE
    await s.insert('publication_attempts', doc)


async def _cleanup(s, job_id):
    await s.db.jobs.delete_many({'id': job_id})
    await s.db.pull_requests.delete_many({'id': f'pr_{job_id}'})
    await s.db.publication_attempts.delete_many({'id': job_id})


def test_reconcile_adopts_existing_remote_pr():
    async def run():
        s = _store()
        repo = await _prereqs_ok(s)
        if not repo:
            pytest.skip('publication prerequisites (CONNECTED github + ASSISTED + secret) not present')
        jid = 'recjob_' + uuid.uuid4().hex[:8]
        try:
            await _seed(s, repo, jid)
            gh = FakeGitHub(pulls=[{'number': 7, 'html_url': 'https://github.com/x/y/pull/7',
                                    'draft': True, 'state': 'open', 'merged': False, 'head': {'sha': REMOTE}}])
            res = await StubPublisher(s, gh).reconcile(jid, 'test')
            job = await s.one('jobs', {'id': jid})
            att = await s.one('publication_attempts', {'id': jid})
            pr = await s.one('pull_requests', {'id': f'pr_{jid}'})
            return res, job, att, pr
        finally:
            await _cleanup(s, jid)
    res, job, att, pr = asyncio.run(run())
    assert res['reconciled'] == 'ADOPTED_EXISTING_PR' and res['number'] == 7
    assert job['status'] == 'SUBMITTED' and job['pr_url'].endswith('/pull/7')
    assert att['status'] == 'CONFIRMED'
    assert pr['external'] is True and pr['number'] == 7
    # never merged
    assert job['status'] != 'MERGED'


def test_reconcile_completes_missing_pr_for_pushed_branch():
    async def run():
        s = _store()
        repo = await _prereqs_ok(s)
        if not repo:
            pytest.skip('prereqs missing')
        jid = 'recjob_' + uuid.uuid4().hex[:8]
        try:
            await _seed(s, repo, jid)
            gh = FakeGitHub(pulls=[], ref={'object': {'sha': REMOTE}})  # branch exists at our commit, no PR yet
            res = await StubPublisher(s, gh).reconcile(jid, 'test')
            job = await s.one('jobs', {'id': jid})
            att = await s.one('publication_attempts', {'id': jid})
            return res, job, att
        finally:
            await _cleanup(s, jid)
    res, job, att = asyncio.run(run())
    assert res['reconciled'] == 'CREATED_MISSING_PR' and res['number'] == 4242
    assert job['status'] == 'SUBMITTED'
    assert att['status'] == 'CONFIRMED'


def test_reconcile_clears_lock_when_no_remote_state():
    async def run():
        s = _store()
        repo = await _prereqs_ok(s)
        if not repo:
            pytest.skip('prereqs missing')
        jid = 'recjob_' + uuid.uuid4().hex[:8]
        try:
            await _seed(s, repo, jid, with_remote=False)
            gh = FakeGitHub(pulls=[], raise_ref=True)  # branch does not exist
            res = await StubPublisher(s, gh).reconcile(jid, 'test')
            att = await s.one('publication_attempts', {'id': jid})
            return res, att
        finally:
            await _cleanup(s, jid)
    res, att = asyncio.run(run())
    assert res['reconciled'] == 'NO_REMOTE_STATE'
    assert att is None  # consumed lock cleared so a fresh publish is safe


def test_reconcile_refuses_already_confirmed():
    async def run():
        s = _store()
        repo = await _prereqs_ok(s)
        if not repo:
            pytest.skip('prereqs missing')
        jid = 'recjob_' + uuid.uuid4().hex[:8]
        try:
            await _seed(s, repo, jid, attempt_status='CONFIRMED')
            try:
                await StubPublisher(s, FakeGitHub()).reconcile(jid, 'test')
                return 'no-error'
            except HTTPException as e:
                return e.status_code
        finally:
            await _cleanup(s, jid)
    assert asyncio.run(run()) == 409


def test_reconcile_enforces_approval_gate():
    async def run():
        s = _store()
        repo = await _prereqs_ok(s)
        if not repo:
            pytest.skip('prereqs missing')
        jid = 'recjob_' + uuid.uuid4().hex[:8]
        try:
            await _seed(s, repo, jid)
            await s.db.jobs.update_one({'id': jid}, {'$set': {'status': 'WAITING_APPROVAL', 'approval_status': 'PENDING'}})
            try:
                await StubPublisher(s, FakeGitHub(pulls=[])).reconcile(jid, 'test')
                return 'no-error'
            except HTTPException as e:
                return e.status_code
        finally:
            await _cleanup(s, jid)
    assert asyncio.run(run()) == 409  # gate blocks: human approval of a real job is required
