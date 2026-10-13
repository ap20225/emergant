"""Owner-scoped connection state. Verification is GET-only at providers."""
import asyncio
import os
import re
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from .secrets import SecretVault
from .store import now


class GitHubConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    token: SecretStr = Field(min_length=20, max_length=300)
    repository: str = Field(pattern=r'^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$', max_length=180)
    issue_number: int = Field(gt=0)


class RunnerConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    api_key: SecretStr = Field(min_length=16, max_length=300)
    template_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,100}$')


class Connections:
    def __init__(self, store):
        self.store = store
        self.vault = SecretVault()
        self.lock = asyncio.Lock()

    async def policy(self):
        c = await self.store.controls()
        if c.get('global_stop') or c.get('integrations_revoked'):
            raise HTTPException(409, 'Connections blocked by emergency policy')

    async def github_get(self, path, token):
        await self.policy()
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.get(os.environ['GITHUB_API_URL'] + path, headers={
                    'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                    'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']})
            if response.status_code != 200:
                raise HTTPException(400, f'GitHub read verification failed (HTTP {response.status_code}). Check token, repository and issue access.')
            return response.json()
        except (httpx.RequestError, ValueError):
            raise HTTPException(502, 'GitHub unavailable; no connection verified')

    async def verify_github(self, token, repository, number, actor):
        identity = await self.github_get('/user', token)
        meta = await self.github_get(f'/repos/{repository}', token)
        if meta.get('private') or meta.get('archived') or meta.get('disabled'):
            raise HTTPException(403, 'Use an active public disposable repository')
        if meta.get('full_name', '').lower() != repository.lower():
            raise HTTPException(403, 'Repository redirect/name mismatch')
        if meta.get('owner', {}).get('login', '').lower() != identity.get('login', '').lower():
            raise HTTPException(403, 'The first repository must be owned by the authenticated GitHub user')
        issue = await self.github_get(f'/repos/{repository}/issues/{number}', token)
        if 'pull_request' in issue or issue.get('state') != 'open':
            raise HTTPException(422, 'Select a real open issue, not a pull request')
        return {'id': 'github', 'status': 'CONNECTED', 'repository': meta['full_name'],
                'login': identity['login'], 'default_branch': meta['default_branch'],
                'repository_size': meta.get('size'), 'issue_number': number,
                'issue_title': str(issue.get('title', ''))[:200], 'verified_at': now(),
                'issue_read': True, 'repository_read': True, 'owner_user_id': actor}

    async def connect_github(self, body, actor):
        async with self.lock:
            await self.policy()
            previous = await self.store.one('integration_connections', {'id': 'github'})
            if previous and previous.get('repository', '').lower() != body.repository.lower():
                raise HTTPException(409, 'Disconnect the existing repository before selecting another')
            try:
                value = await self.verify_github(body.token.get_secret_value(), body.repository, body.issue_number, actor)
            except HTTPException:
                await self.store.audit('security', 'GitHub connection rejected', 'Read-only verification failed; credential not recorded', 'BLOCKED', actor=actor)
                raise
            self.vault.put('github', body.token.get_secret_value())
            await self.store.db.integration_connections.update_one({'id': 'github'}, {'$set': value}, upsert=True)
            await self.store.db.integration_connections.update_one(
                {'id': 'github', 'write_permission': {'$exists': False}},
                {'$set': {'write_permission': 'NOT_VERIFIED', 'external_write_authorized': False}})
            await self.store.audit('security', 'GitHub reads verified', f"{value['repository']} issue #{body.issue_number}; identity and public reads only; no remote mutation", actor=actor)
            return value

    async def verify_saved_github(self, actor):
        saved = await self.store.one('integration_connections', {'id': 'github'})
        token = self.vault.get('github')
        if not saved or not token:
            raise HTTPException(409, 'GITHUB_REQUIRED: connect one repository first')
        try:
            value = await self.verify_github(token, saved['repository'], saved['issue_number'], actor)
        except HTTPException:
            await self.store.db.integration_connections.update_one({'id': 'github'}, {'$set': {'status': 'PROVIDER_ERROR', 'issue_read': False}})
            await self.store.audit('security', 'GitHub verification failed', 'Saved credential or repository access could not be verified', 'BLOCKED', actor=actor)
            raise
        await self.store.db.integration_connections.update_one({'id': 'github'}, {'$set': value})
        await self.store.audit('security', 'GitHub reads reverified', value['repository'], actor=actor)
        return value

    async def verify_github_write(self, actor):
        """Read-only effective-permission probe. Performs NO branch/commit/PR/merge/delete."""
        saved = await self.store.one('integration_connections', {'id': 'github'})
        token = self.vault.get('github')
        if not saved or not token:
            raise HTTPException(409, 'GITHUB_REQUIRED: connect one repository first')
        identity = await self.github_get('/user', token)
        meta = await self.github_get(f"/repos/{saved['repository']}", token)
        if meta.get('full_name', '').lower() != saved['repository'].lower():
            raise HTTPException(403, 'Repository identity mismatch; write verification aborted')
        perms = meta.get('permissions') or {}
        can_write = bool(perms.get('push') or perms.get('admin') or perms.get('maintain'))
        status = 'VERIFIED' if can_write else 'NOT_VERIFIED'
        effective = {k: bool(perms.get(k)) for k in ['admin', 'maintain', 'push', 'triage', 'pull']}
        update = {'write_permission': status, 'effective_permissions': effective,
                  'write_login': identity.get('login'), 'write_verified_at': now()}
        await self.store.db.integration_connections.update_one({'id': 'github'}, {'$set': update})
        await self.store.audit('security', 'GitHub write permission verified',
            f"{saved['repository']}: effective push={perms.get('push')} admin={perms.get('admin')} maintain={perms.get('maintain')}; read-only check, no mutation performed",
            status, actor=actor)
        return {'repository': saved['repository'], 'repository_read': True,
                'write_permission': status, 'effective_permissions': effective, 'write_login': identity.get('login'),
                'mutation_performed': False,
                'publication_ready': status == 'VERIFIED' and saved.get('status') == 'CONNECTED'}

    async def configure_runner(self, body, actor):
        await self.policy()
        self.vault.put('e2b', body.api_key.get_secret_value())
        value = {'id': 'runner', 'provider': 'E2B', 'template_id': body.template_id,
                 'status': 'CONFIGURED_UNVERIFIED', 'verified_at': None, 'configured_at': now()}
        await self.store.db.integration_connections.update_one({'id': 'runner'}, {'$set': value}, upsert=True)
        await self.store.audit('security', 'Runner credential configured', 'E2B configuration only; no sandbox created, no funding authorized', actor=actor)
        return value

    async def disconnect(self, name, actor):
        if name not in {'github', 'runner'}:
            raise HTTPException(404, 'Connection not found')
        self.vault.delete('e2b' if name == 'runner' else name)
        await self.store.db.integration_connections.delete_one({'id': name})
        await self.store.audit('security', 'Connection removed', f'{name}: app credential removed; provider revocation is separate', actor=actor)