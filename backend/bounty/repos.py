"""Per-repository authorization registry + capability model + fork-first target resolution.

Replaces the old single-fixed-repository restriction. Reads are allowed to any repository that
is not REVOKED/BLOCKED (public, read-only token). WRITES require an explicit AUTHORIZED entry with
publication capability. Newly discovered repositories default to READ_ONLY and NEVER auto-gain write.
"""
import asyncio
import os

import httpx
from pymongo.errors import DuplicateKeyError
from fastapi import HTTPException

from .store import now

READ_CAPS = ['READ_REPO', 'READ_ISSUE', 'READ_ISSUES', 'READ_CONTENT']
WRITE_CAPS = ['WRITE_BRANCH', 'WRITE_CONTENT', 'CREATE_PR']
STATUSES = ['DISCOVERED', 'READ_ONLY', 'AUTHORIZED', 'PAUSED', 'REVOKED', 'BLOCKED']


def _key(repo):
    return str(repo).strip().lower()


class RepoRegistry:
    def __init__(self, store):
        self.store = store

    async def get(self, repo):
        if not repo:
            return None
        return await self.store.one('repo_authorizations', {'id': _key(repo)})

    async def register_discovered(self, repo, account_identity=None, actor='scheduler'):
        existing = await self.get(repo)
        if existing:
            return existing
        key = _key(repo)
        owner, _, name = key.partition('/')
        doc = {'id': key, 'owner': owner, 'repo': name, 'full_name': repo, 'account_identity': account_identity,
               'permission_scope': 'read', 'execution_enabled': False, 'publication_enabled': False,
               'status': 'READ_ONLY', 'fork_strategy': 'FORK_AND_PR', 'fork_repo': None,
               'capabilities': {}, 'capability_checked_at': None,
               'authorized_at': None, 'authorized_by': None, 'expires_at': None, 'created_at': now()}
        try:
            await self.store.insert('repo_authorizations', doc)
            await self.store.audit('scout', 'Repository registered', f'{repo}: READ_ONLY (no write access granted)', actor=actor)
        except DuplicateKeyError:
            return await self.get(repo)
        return doc

    async def can_read(self, repo):
        r = await self.get(repo)
        return (r is None) or (r.get('status') not in ('REVOKED', 'BLOCKED'))

    def write_ok(self, r):
        if not r or r.get('status') != 'AUTHORIZED' or not r.get('publication_enabled'):
            return False
        exp = r.get('expires_at')
        if exp and isinstance(exp, str) and len(exp) >= 10 and exp < now():
            return False
        return bool((r.get('capabilities') or {}).get('CREATE_PR'))

    async def write_authorized(self, repo):
        return self.write_ok(await self.get(repo))

    async def authorize(self, repo, by, execution=True, publication=True, expires_at=None,
                        fork_strategy='FORK_AND_PR', account_identity=None):
        await self.register_discovered(repo, account_identity, actor=by)
        upd = {'status': 'AUTHORIZED', 'execution_enabled': bool(execution), 'publication_enabled': bool(publication),
               'permission_scope': 'write', 'fork_strategy': fork_strategy, 'authorized_at': now(),
               'authorized_by': by, 'expires_at': expires_at}
        if account_identity:
            upd['account_identity'] = account_identity
        await self.store.db.repo_authorizations.update_one({'id': _key(repo)}, {'$set': upd})
        await self.store.audit('master', 'Repository authorized', f'{repo}: execution={execution} publication={publication}',
                               actor=by, authorization='HUMAN_APPROVED')
        return await self.get(repo)

    async def set_status(self, repo, status, by):
        if status not in STATUSES:
            raise ValueError('invalid status')
        clear_write = status in ('PAUSED', 'REVOKED', 'BLOCKED', 'READ_ONLY', 'DISCOVERED')
        upd = {'status': status, 'updated_at': now()}
        if clear_write:
            upd.update({'execution_enabled': False, 'publication_enabled': False, 'permission_scope': 'read'})
        await self.store.db.repo_authorizations.update_one({'id': _key(repo)}, {'$set': upd})
        await self.store.audit('master', 'Repository status changed', f'{repo}: {status}', actor=by, authorization='HUMAN_APPROVED')
        return await self.get(repo)

    async def set_fork(self, repo, fork_repo, by):
        await self.store.db.repo_authorizations.update_one({'id': _key(repo)}, {'$set': {'fork_repo': fork_repo, 'updated_at': now()}})
        return await self.get(repo)

    async def record_capabilities(self, repo, caps):
        await self.store.db.repo_authorizations.update_one(
            {'id': _key(repo)}, {'$set': {'capabilities': caps, 'capability_checked_at': now()}})


async def check_capabilities(github, repo):
    """Read-only capability probe from GitHub repo permissions. Never mutates the repository."""
    caps = {c: False for c in READ_CAPS + WRITE_CAPS}
    meta = await github.request('GET', f'/repos/{repo}')
    perms = meta.get('permissions') or {}
    for c in READ_CAPS:
        caps[c] = True  # public repositories are readable with the read-only token
    if perms.get('push') or perms.get('maintain') or perms.get('admin'):
        for c in WRITE_CAPS:
            caps[c] = True
    return caps, {'private': meta.get('private'), 'permissions': perms, 'fork': meta.get('fork'),
                  'parent': meta.get('parent'), 'default_branch': meta.get('default_branch')}


async def resolve_targets(registry, opportunity):
    """Phase 3/4: keep source/target/execution/fork/publication repos distinct. Fork-first for third-party repos."""
    upstream = opportunity.get('repository')
    reg = await registry.get(upstream)
    result = {'source_repo': opportunity.get('source') or upstream, 'target_repo': upstream, 'upstream_repo': upstream,
              'execution_repo': None, 'fork_repo': None, 'publication_repo': None,
              'strategy': None, 'write_ready': False, 'reason': None}
    if reg and registry.write_ok(reg):
        result.update({'execution_repo': upstream, 'publication_repo': upstream, 'strategy': 'DIRECT_WRITE', 'write_ready': True})
        return result
    fork = (reg or {}).get('fork_repo')
    if fork:
        fork_reg = await registry.get(fork)
        if registry.write_ok(fork_reg):
            result.update({'execution_repo': fork, 'fork_repo': fork, 'publication_repo': upstream,
                           'strategy': 'FORK_AND_PR', 'write_ready': True})
            return result
    result.update({'strategy': (reg or {}).get('fork_strategy') or 'FORK_AND_PR',
                   'write_ready': False, 'reason': 'BLOCKED_WRITE_PERMISSION'})
    return result


def job_targets(job):
    """Resolve push/PR repos from a persisted job. Defaults to same-repo (legacy) when fork fields are absent.
    `head` is always `owner:branch` (valid for both same-repo and cross-fork PRs and the GitHub head filter)."""
    upstream = job.get('publication_repo') or job.get('upstream_repo') or job['repository']
    execution = job.get('execution_repo') or job['repository']
    same = str(execution).lower() == str(upstream).lower()
    head = f"{str(execution).split('/')[0]}:{job['branch']}"
    return {'push_repo': execution, 'pr_repo': upstream, 'head': head, 'same_repo': same}


async def verify_execution_targets(github, registry, opportunity, expect_fork_owner=None):
    """Fail-closed pre-execution validation of fork-first targets. Returns the resolved target dict or raises."""
    targets = await resolve_targets(registry, opportunity)
    if not targets['write_ready']:
        raise HTTPException(409, 'BLOCKED_WRITE_TARGET: no authorized write target or authorized fork')
    upstream, execution = targets['upstream_repo'], targets['execution_repo']
    up = await github.request('GET', f'/repos/{upstream}')
    if not up.get('full_name'):
        raise HTTPException(409, 'BLOCKED_WRITE_TARGET: upstream repository missing')
    issue_number = opportunity.get('issue_number')
    if issue_number:
        issue = await github.request('GET', f'/repos/{upstream}/issues/{issue_number}')
        if issue.get('state') != 'open':
            raise HTTPException(409, 'BLOCKED_WRITE_TARGET: issue no longer open')
        if opportunity.get('title') and issue.get('title') and opportunity['title'][:80] != issue['title'][:80]:
            raise HTTPException(409, 'BLOCKED_WRITE_TARGET: opportunity identity changed')
    if targets['strategy'] == 'FORK_AND_PR':
        fork = await github.request('GET', f'/repos/{execution}')
        if not fork.get('full_name'):
            raise HTTPException(409, 'BLOCKED_WRITE_TARGET: fork repository missing')
        if not fork.get('fork'):
            raise HTTPException(409, 'BLOCKED_WRITE_TARGET: execution repository is not a fork')
        parent = (fork.get('parent') or {}).get('full_name')
        if parent and parent.lower() != upstream.lower():
            raise HTTPException(409, 'BLOCKED_WRITE_TARGET: fork parent is not the authorized upstream')
        reg = await registry.get(execution)
        expected_owner = expect_fork_owner or (reg or {}).get('account_identity')
        owner = (fork.get('owner') or {}).get('login')
        if expected_owner and owner and expected_owner.lower() != owner.lower():
            raise HTTPException(409, 'BLOCKED_WRITE_TARGET: fork owner is not the authorized account')
        perms = fork.get('permissions') or {}
        if not (perms.get('push') or perms.get('maintain') or perms.get('admin')):
            raise HTTPException(409, 'BLOCKED_WRITE_TARGET: fork is not writable')
    return targets


async def create_and_authorize_fork(store, github, upstream, actor, expires_at=None):
    """Ensure a write target exists for `upstream`: create the owner's fork if missing (narrow,
    owner-approved, audited GitHub write — forks only) and authorize it FORK_AND_PR. Idempotent.
    Fail-closed: the fork must be a real fork the connected account can push to."""
    from .secrets import SecretVault
    conn = await store.one('integration_connections', {'id': 'github'})
    login = (conn or {}).get('login')
    if not login:
        raise HTTPException(409, 'GitHub not connected; cannot create a fork write target')
    fork_repo = f"{login}/{upstream.split('/')[1]}"
    registry = RepoRegistry(store)
    await registry.register_discovered(upstream, actor=actor)
    exists = None
    try:
        exists = await github.request('GET', f'/repos/{fork_repo}')
    except HTTPException:
        exists = None
    if not exists:
        controls = await store.controls()
        if any(controls.get(k) for k in ('integrations_revoked', 'global_stop', 'submissions_disabled')):
            raise HTTPException(409, 'Integrations disabled by emergency policy')
        token = SecretVault().get('github')
        if not token:
            raise HTTPException(409, 'No GitHub credential to create the fork')
        await store.audit('master', 'GitHub fork create attempted', f'{upstream} -> {fork_repo}', actor=actor, authorization='HUMAN_APPROVED')
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(os.environ['GITHUB_API_URL'] + f'/repos/{upstream}/forks',
                                    headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                             'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']}, json={})
        if res.status_code not in (200, 201, 202):
            raise HTTPException(res.status_code if res.status_code in (401, 403, 404) else 502, f'Fork creation failed: HTTP {res.status_code}')
        for _ in range(20):
            await asyncio.sleep(3)
            try:
                if await github.request('GET', f'/repos/{fork_repo}'):
                    break
            except HTTPException:
                continue
    caps, meta = await check_capabilities(github, fork_repo)
    if not meta.get('fork'):
        raise HTTPException(409, f'{fork_repo} is not a GitHub fork')
    if not caps.get('CREATE_PR'):
        raise HTTPException(409, f'The connected GitHub account cannot push to {fork_repo}')
    account_identity = fork_repo.split('/')[0]
    await registry.register_discovered(fork_repo, account_identity=account_identity, actor=actor)
    await registry.record_capabilities(fork_repo, caps)
    await registry.authorize(fork_repo, actor, execution=True, publication=True, expires_at=expires_at,
                             fork_strategy='FORK_AND_PR', account_identity=account_identity)
    await registry.set_fork(upstream, fork_repo, actor)
    await store.audit('master', 'GitHub fork authorized as write target', f'{upstream} -> {fork_repo}', 'SUCCESS', actor=actor, authorization='HUMAN_APPROVED')
    return fork_repo
