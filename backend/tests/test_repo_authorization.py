"""Deterministic per-repository authorization + capability + fork-first + cross-repo write rejection tests."""
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
from bounty.repos import RepoRegistry, check_capabilities, resolve_targets
from bounty.publication import Publisher

R1 = 'octo/' + uuid.uuid4().hex[:8]
FORK = 'me/' + uuid.uuid4().hex[:8]


def _store():
    return Store(AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']])


class FakeGitHub:
    def __init__(self, perms):
        self.perms = perms

    async def request(self, method, path):
        return {'permissions': self.perms, 'private': False, 'fork': False, 'default_branch': 'main'}


async def _clean(s, *repos):
    for r in repos:
        await s.db.repo_authorizations.delete_many({'id': r.lower()})


def test_discovered_repo_defaults_read_only_no_write():
    async def run():
        s = _store(); reg = RepoRegistry(s)
        try:
            doc = await reg.register_discovered(R1, actor='test')
            return doc, await reg.can_read(R1), await reg.write_authorized(R1)
        finally:
            await _clean(s, R1)
    doc, can_read, can_write = asyncio.run(run())
    assert doc['status'] == 'READ_ONLY'
    assert doc['execution_enabled'] is False and doc['publication_enabled'] is False
    assert can_read is True
    assert can_write is False  # write NEVER auto-granted


def test_authorize_requires_capability_for_write():
    async def run():
        s = _store(); reg = RepoRegistry(s)
        try:
            await reg.authorize(R1, 'owner-user', execution=True, publication=True)
            before = await reg.write_authorized(R1)  # AUTHORIZED but no CREATE_PR capability yet
            await reg.record_capabilities(R1, {'CREATE_PR': True, 'WRITE_BRANCH': True})
            after = await reg.write_authorized(R1)
            return before, after
        finally:
            await _clean(s, R1)
    before, after = asyncio.run(run())
    assert before is False  # authorized but capability not proven
    assert after is True


def test_revoke_blocks_read_and_write():
    async def run():
        s = _store(); reg = RepoRegistry(s)
        try:
            await reg.authorize(R1, 'owner-user')
            await reg.record_capabilities(R1, {'CREATE_PR': True})
            await reg.set_status(R1, 'REVOKED', 'owner-user')
            return await reg.can_read(R1), await reg.write_authorized(R1)
        finally:
            await _clean(s, R1)
    can_read, can_write = asyncio.run(run())
    assert can_read is False and can_write is False


def test_capability_probe_maps_permissions():
    async def run():
        caps_rw, _ = await check_capabilities(FakeGitHub({'push': True}), R1)
        caps_ro, _ = await check_capabilities(FakeGitHub({'pull': True}), R1)
        return caps_rw, caps_ro
    caps_rw, caps_ro = asyncio.run(run())
    assert caps_rw['CREATE_PR'] is True and caps_rw['READ_ISSUE'] is True
    assert caps_ro['CREATE_PR'] is False and caps_ro['READ_ISSUE'] is True


def test_fork_first_resolution():
    async def run():
        s = _store(); reg = RepoRegistry(s)
        try:
            # upstream NOT writable; an authorized fork exists -> FORK_AND_PR
            await reg.register_discovered(R1, actor='test')
            await reg.authorize(FORK, 'owner-user')
            await reg.record_capabilities(FORK, {'CREATE_PR': True})
            await reg.set_fork(R1, FORK, 'owner-user')
            t_fork = await resolve_targets(reg, {'repository': R1})
            # direct write when upstream authorized
            await reg.authorize(R1, 'owner-user')
            await reg.record_capabilities(R1, {'CREATE_PR': True})
            t_direct = await resolve_targets(reg, {'repository': R1})
            # nothing authorized -> blocked
            t_blocked = await resolve_targets(reg, {'repository': 'nobody/' + uuid.uuid4().hex[:6]})
            return t_fork, t_direct, t_blocked
        finally:
            await _clean(s, R1, FORK)
    t_fork, t_direct, t_blocked = asyncio.run(run())
    assert t_fork['strategy'] == 'FORK_AND_PR' and t_fork['execution_repo'] == FORK and t_fork['publication_repo'] == R1 and t_fork['write_ready']
    assert t_direct['strategy'] == 'DIRECT_WRITE' and t_direct['write_ready']
    assert t_blocked['write_ready'] is False and t_blocked['reason'] == 'BLOCKED_WRITE_PERMISSION'


def test_publication_gate_rejects_unauthorized_cross_repo_write():
    async def run():
        s = _store()
        conn = await s.one('integration_connections', {'id': 'github'})
        if not conn:
            import pytest
            pytest.skip('no github connection present')
        # a job targeting a DIFFERENT, unauthorized repo must be blocked by the publication gate
        job = {'id': 'x', 'repository': R1, 'simulated': False, 'status': 'APPROVED', 'approval_status': 'APPROVED',
               'test_status': 'PASS', 'security_status': 'PASS', 'review_status': 'APPROVE',
               'patch_files': {'a.py': 'x=1\n'}, 'approved_artifact_hash': 'h', 'artifact_hash': 'h'}
        try:
            await RepoRegistry(s).register_discovered(R1, actor='test')  # READ_ONLY, not writable
            await Publisher(s, None).gate(job)
            return 'no-error'
        except HTTPException as e:
            return e.status_code
        finally:
            await _clean(s, R1)
    code = asyncio.run(run())
    assert code == 409  # cross-repo write to a non-authorized repo rejected
