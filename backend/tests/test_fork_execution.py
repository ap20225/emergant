"""Deterministic fork-first execution wiring tests. NO E2B, NO provider, NO GitHub mutation.
Stubs the GitHub client. Proves: execution/branch/push/PR targets, upstream/fork separation,
cross-repo write protection, and the full 'verified opportunity + authorized fork' acceptance path
while AUTONOMOUS_EXECUTION_ENABLED stays false."""
import asyncio
import os
import sys
import uuid

from fastapi import HTTPException

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env'); load_dotenv('/app/backend/bounty.env')
from motor.motor_asyncio import AsyncIOMotorClient
from bounty.store import Store
from bounty.repos import RepoRegistry, job_targets, resolve_targets, verify_execution_targets
from bounty.runner import clone_spec
from bounty.authorization import AutonomousAuthorization

UP = 'upstream/' + uuid.uuid4().hex[:8]
FORK = 'me/' + uuid.uuid4().hex[:8]
ACCOUNT = 'me'


def _store():
    return Store(AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']])


class FakeGitHub:
    """Returns canned repo/issue metadata by path. Never mutates."""
    def __init__(self, repos=None, issues=None):
        self.repos = repos or {}
        self.issues = issues or {}

    async def request(self, method, path):
        parts = path.split('/')
        if '/issues/' in path:
            repo = '/'.join(parts[2:4])
            return self.issues.get(repo, {'state': 'open', 'title': 'match'})
        if path.startswith('/repos/'):
            repo = '/'.join(parts[2:4])
            return self.repos.get(repo, {})
        return {}


async def _clean(s):
    await s.db.repo_authorizations.delete_many({'id': {'$in': [UP.lower(), FORK.lower()]}})


async def _authorized_fork(s):
    reg = RepoRegistry(s)
    await reg.register_discovered(UP, actor='test')                 # upstream READ_ONLY
    await reg.authorize(FORK, 'owner-user', account_identity=ACCOUNT)  # fork AUTHORIZED
    await reg.record_capabilities(FORK, {'CREATE_PR': True, 'WRITE_BRANCH': True, 'WRITE_CONTENT': True})
    await reg.set_fork(UP, FORK, 'owner-user')
    return reg


# ---- target resolution ----

def test_job_targets_fork_first_head_and_bases():
    job = {'repository': UP, 'upstream_repo': UP, 'publication_repo': UP, 'execution_repo': FORK, 'branch': 'penniless/j1'}
    t = job_targets(job)
    assert t['push_repo'] == FORK          # push/branch target = fork
    assert t['pr_repo'] == UP              # PR base = upstream
    assert t['head'] == f'{ACCOUNT}:penniless/j1'  # PR head = fork owner:branch
    assert t['same_repo'] is False


def test_job_targets_legacy_same_repo():
    job = {'repository': 'ap/x', 'branch': 'penniless/j2'}
    t = job_targets(job)
    assert t['push_repo'] == 'ap/x' and t['pr_repo'] == 'ap/x' and t['head'] == 'ap:penniless/j2' and t['same_repo'] is True


def test_clone_spec_targets_execution_fork():
    job = {'repository': UP, 'execution_repo': FORK, 'base_branch': 'main'}
    repo, branch = clone_spec(job, {'repository': UP, 'default_branch': 'main'})
    assert repo == FORK  # E2B clones the AUTHORIZED FORK, never the upstream we cannot write
    # legacy controlled flow falls back to the connected repo
    repo2, _ = clone_spec({'repository': 'ap/x', 'base_branch': 'main'}, {'repository': 'ap/x', 'default_branch': 'main'})
    assert repo2 == 'ap/x'


def test_resolve_targets_fork_first():
    async def run():
        s = _store()
        try:
            await _authorized_fork(s)
            return await resolve_targets(RepoRegistry(s), {'repository': UP})
        finally:
            await _clean(s)
    t = asyncio.run(run())
    assert t['strategy'] == 'FORK_AND_PR' and t['write_ready'] is True
    assert t['execution_repo'] == FORK and t['fork_repo'] == FORK and t['publication_repo'] == UP


# ---- pre-execution target verification (fail-closed) ----

def test_verify_targets_happy_path():
    async def run():
        s = _store()
        try:
            reg = await _authorized_fork(s)
            gh = FakeGitHub(
                repos={UP: {'full_name': UP, 'default_branch': 'main'},
                       FORK: {'full_name': FORK, 'fork': True, 'parent': {'full_name': UP},
                              'owner': {'login': ACCOUNT}, 'permissions': {'push': True}}},
                issues={UP: {'state': 'open', 'title': 'Fix the bug'}})
            return await verify_execution_targets(gh, reg, {'repository': UP, 'issue_number': 5, 'title': 'Fix the bug'})
        finally:
            await _clean(s)
    t = asyncio.run(run())
    assert t['write_ready'] and t['execution_repo'] == FORK and t['publication_repo'] == UP


def test_verify_targets_blocks_wrong_fork_owner():
    async def run():
        s = _store()
        try:
            reg = await _authorized_fork(s)
            gh = FakeGitHub(
                repos={UP: {'full_name': UP}, FORK: {'full_name': FORK, 'fork': True, 'parent': {'full_name': UP},
                       'owner': {'login': 'someone-else'}, 'permissions': {'push': True}}},
                issues={UP: {'state': 'open', 'title': 'match'}})
            try:
                await verify_execution_targets(gh, reg, {'repository': UP, 'issue_number': 5, 'title': 'match'})
                return 'no-error'
            except HTTPException as e:
                return e.detail
        finally:
            await _clean(s)
    detail = asyncio.run(run())
    assert 'BLOCKED_WRITE_TARGET' in detail and 'owner' in detail


def test_verify_targets_blocks_non_fork_and_readonly_fork():
    async def run():
        s = _store()
        out = {}
        try:
            reg = await _authorized_fork(s)
            # execution repo is not actually a fork
            gh1 = FakeGitHub(repos={UP: {'full_name': UP}, FORK: {'full_name': FORK, 'fork': False,
                             'owner': {'login': ACCOUNT}, 'permissions': {'push': True}}},
                             issues={UP: {'state': 'open', 'title': 'match'}})
            try:
                await verify_execution_targets(gh1, reg, {'repository': UP, 'issue_number': 5, 'title': 'match'})
                out['nonfork'] = 'no-error'
            except HTTPException as e:
                out['nonfork'] = e.detail
            # fork present but not writable
            gh2 = FakeGitHub(repos={UP: {'full_name': UP}, FORK: {'full_name': FORK, 'fork': True, 'parent': {'full_name': UP},
                             'owner': {'login': ACCOUNT}, 'permissions': {'pull': True}}},
                             issues={UP: {'state': 'open', 'title': 'match'}})
            try:
                await verify_execution_targets(gh2, reg, {'repository': UP, 'issue_number': 5, 'title': 'match'})
                out['readonly'] = 'no-error'
            except HTTPException as e:
                out['readonly'] = e.detail
            return out
        finally:
            await _clean(s)
    out = asyncio.run(run())
    assert 'not a fork' in out['nonfork']
    assert 'not writable' in out['readonly']


def test_verify_targets_blocks_closed_issue_and_identity_change():
    async def run():
        s = _store()
        out = {}
        try:
            reg = await _authorized_fork(s)
            base_repos = {UP: {'full_name': UP}, FORK: {'full_name': FORK, 'fork': True, 'parent': {'full_name': UP},
                          'owner': {'login': ACCOUNT}, 'permissions': {'push': True}}}
            gh_closed = FakeGitHub(repos=base_repos, issues={UP: {'state': 'closed', 'title': 'match'}})
            try:
                await verify_execution_targets(gh_closed, reg, {'repository': UP, 'issue_number': 5, 'title': 'match'})
                out['closed'] = 'no-error'
            except HTTPException as e:
                out['closed'] = e.detail
            gh_changed = FakeGitHub(repos=base_repos, issues={UP: {'state': 'open', 'title': 'a totally different title now'}})
            try:
                await verify_execution_targets(gh_changed, reg, {'repository': UP, 'issue_number': 5, 'title': 'original title'})
                out['changed'] = 'no-error'
            except HTTPException as e:
                out['changed'] = e.detail
            return out
        finally:
            await _clean(s)
    out = asyncio.run(run())
    assert 'issue no longer open' in out['closed']
    assert 'identity changed' in out['changed']


def test_verify_targets_blocks_when_no_write_target():
    async def run():
        s = _store()
        try:
            reg = RepoRegistry(s)
            await reg.register_discovered(UP, actor='test')  # upstream READ_ONLY, no fork
            gh = FakeGitHub(repos={UP: {'full_name': UP}})
            try:
                await verify_execution_targets(gh, reg, {'repository': UP, 'issue_number': 5})
                return 'no-error'
            except HTTPException as e:
                return e.detail
        finally:
            await _clean(s)
    detail = asyncio.run(run())
    assert 'BLOCKED_WRITE_TARGET' in detail


# ---- FINAL ACCEPTANCE: verified opportunity + authorized fork reaches correct targets, flag stays off ----

def test_acceptance_verified_opp_reaches_fork_targets_flag_off():
    async def run():
        s = _store()
        flags = (await s.one('system_settings', {'id': 'feature-flags'}) or {}).get('config') or {}
        assert not flags.get('AUTONOMOUS_EXECUTION_ENABLED'), 'flag must remain OFF'
        try:
            reg = await _authorized_fork(s)
            opp = {'id': 'op_' + uuid.uuid4().hex[:6], 'repository': UP, 'issue_number': 9,
                   'status': 'AWAITING_AUTHORIZATION', 'qualification_status': 'QUALIFIED', 'risk_status': 'CLEARED',
                   'reward_status': 'VERIFIED', 'reward_verified': True, 'title': 'Fix', 'duplicate_key': 'dk9'}
            decision = await AutonomousAuthorization(s).evaluate(opp)
            targets = decision['targets']
            # build the job the worker would create from resolved targets
            job = {'repository': targets['upstream_repo'], 'upstream_repo': targets['upstream_repo'],
                   'execution_repo': targets['execution_repo'], 'fork_repo': targets['fork_repo'],
                   'publication_repo': targets['publication_repo'], 'branch': 'penniless/jACC', 'base_branch': 'main'}
            t = job_targets(job)
            clone_repo, _ = clone_spec(job, None)
            return decision, targets, t, clone_repo
        finally:
            await _clean(s)
    decision, targets, t, clone_repo = asyncio.run(run())
    # every gate passes AND write target ready, but the flag is off -> AWAITING_AUTHORIZATION, never AUTHORIZED
    assert decision['checks']['reward_verified'] is True
    assert decision['checks']['repo_write_authorized'] is True
    assert decision['checks']['execution_flag_enabled'] is False
    assert decision['decision'] == 'AWAITING_AUTHORIZATION' and decision['authorized'] is False
    # target routing is correct: E2B=fork, branch=fork, push=fork, PR head=fork, PR base=upstream
    assert targets['execution_repo'] == FORK and targets['publication_repo'] == UP
    assert clone_repo == FORK
    assert t['push_repo'] == FORK and t['pr_repo'] == UP and t['head'] == f'{ACCOUNT}:penniless/jACC'
