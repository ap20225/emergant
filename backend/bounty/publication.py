"""One-shot, explicit human-approved draft PR publication. Never merge or retry writes."""
import re
import httpx
import os
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError
from .secrets import SecretVault
from .live_worker import artifact_hash
from .patches import parse_patch
from .store import now
import json


class Publisher:
    def __init__(self, store, github):
        self.store, self.github = store, github

    async def gate(self, job):
        c, s = await self.store.controls(), await self.store.settings()
        if any(c.get(k) for k in ['global_stop', 'agents_stopped', 'submissions_disabled', 'integrations_revoked']) or s['mode'] != 'ASSISTED':
            raise HTTPException(409, 'Publication stopped by policy')
        connection = await self.store.one('integration_connections', {'id': 'github'})
        if not connection or connection.get('status') != 'CONNECTED' or not SecretVault().has('github'):
            raise HTTPException(409, 'Verified GitHub connection required')
        same_repo = connection['repository'].lower() == str(job.get('execution_repo') or job['repository']).lower()
        if not same_repo:
            from .repos import RepoRegistry
            if not await RepoRegistry(self.store).write_authorized(job.get('execution_repo') or job['repository']):
                raise HTTPException(409, 'Execution/publication target is not AUTHORIZED for write in the repository registry')
        if job.get('simulated') or job.get('status') != 'APPROVED' or job.get('approval_status') != 'APPROVED':
            raise HTTPException(409, 'Explicit human approval of a real job is required')
        if job.get('test_status') != 'PASS' or job.get('security_status') != 'PASS' or job.get('review_status') != 'APPROVE':
            raise HTTPException(409, 'Tests and both reviews must pass')
        digest = artifact_hash(job.get('patch_files', {}))
        if digest != job.get('approved_artifact_hash') or digest != job.get('artifact_hash'):
            raise HTTPException(409, 'Artifact changed since approval; publication blocked')
        parse_patch(json.dumps({'files': job['patch_files']}))
        return connection

    async def write(self, job, path, payload):
        await self.gate(job)
        # Fixed, caller-generated paths only; no remote operation selected by model text.
        await self.store.audit('pr_manager', 'GitHub write attempted', path, job_id=job['id'], authorization='HUMAN_APPROVED')
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.post(os.environ['GITHUB_API_URL'] + path, json=payload, headers={
                    'Authorization': f"Bearer {SecretVault().get('github')}", 'Accept': 'application/vnd.github+json',
                    'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']})
            if response.status_code not in [200, 201]:
                detail = ''
                try:
                    body = response.json()
                    parts = [str(body.get('message') or '')]
                    for err in (body.get('errors') or []):
                        if isinstance(err, dict):
                            m = err.get('message') or f"{err.get('field', '')}:{err.get('code', '')}"
                            if m:
                                parts.append(str(m))
                    detail = '; '.join(p for p in parts if p)[:400]
                except ValueError:
                    detail = (response.text or '')[:300]
                await self.store.db.publication_attempts.update_one(
                    {'id': job['id']}, {'$set': {'last_error': f'HTTP {response.status_code} {path}: {detail}'}})
                await self.store.audit('pr_manager', 'GitHub write rejected', f'{path}; HTTP {response.status_code}: {detail}', 'BLOCKED', job_id=job['id'])
                raise HTTPException(409, f'GitHub publication HTTP {response.status_code}: {detail}; no automatic retry')
            data = response.json()
            await self.store.audit('pr_manager', 'GitHub write confirmed', path, job_id=job['id'], authorization='HUMAN_APPROVED')
            return data
        except (httpx.RequestError, ValueError) as e:
            await self.store.db.publication_attempts.update_one(
                {'id': job['id']}, {'$set': {'last_error': f'{path}: transport/parse error {type(e).__name__}'}})
            raise HTTPException(502, 'Publication outcome uncertain; no automatic retry')

    async def publish(self, job_id, actor):
        job = await self.store.one('jobs', {'id': job_id})
        if not job:
            raise HTTPException(404, 'Job not found')
        await self.gate(job)
        s = await self.store.settings()
        if await self.store.db.publication_attempts.count_documents({'created_at': {'$gte': now()[:10]}}) >= s['max_submissions_per_day']:
            raise HTTPException(409, 'Daily publication limit reached')
        from .repos import job_targets
        t = job_targets(job)
        push_base, pr_base = '/repos/' + t['push_repo'], '/repos/' + t['pr_repo']
        ref = await self.github.request('GET', pr_base + '/git/ref/heads/' + job['base_branch'])
        if ref['object']['sha'] != job['base_sha']:
            raise HTTPException(409, 'Base branch changed; rerun tests and obtain new approval before publication')
        pr = await self.store.one('pull_requests', {'id': job['pr_id']})
        if not pr:
            raise HTTPException(409, 'Reviewed PR artifact missing')
        # Durable uniqueness survives refreshes/restarts; a timeout consumes the attempt.
        try:
            await self.store.insert('publication_attempts', {'id': job_id, 'status': 'DISPATCHED_ONCE', 'actor': actor, 'artifact_hash': job['artifact_hash']})
        except DuplicateKeyError:
            raise HTTPException(409, 'Publication already attempted; use read-only status reconciliation, not a retry')
        try:
            base = await self.github.request('GET', push_base + '/git/commits/' + job['base_sha'])
            tree = await self.write(job, push_base + '/git/trees', {'base_tree': base['tree']['sha'], 'tree': [
                {'path': path, 'mode': '100644', 'type': 'blob', 'content': text} for path, text in job['patch_files'].items()]})
            commit = await self.write(job, push_base + '/git/commits', {'message': pr['title'], 'tree': tree['sha'], 'parents': [job['base_sha']]})
            await self.store.db.publication_attempts.update_one({'id': job_id}, {'$set': {'remote_commit': commit['sha']}})
            await self.write(job, push_base + '/git/refs', {'ref': 'refs/heads/' + job['branch'], 'sha': commit['sha']})
            response = await self.write(job, pr_base + '/pulls', {'title': pr['title'], 'body': pr['body'], 'head': t['head'], 'base': job['base_branch'], 'draft': True})
            if response.get('draft') is not True or not isinstance(response.get('number'), int):
                raise HTTPException(502, 'GitHub draft response not confirmed; reconcile manually')
            patch = {'external': True, 'url': response['html_url'], 'number': response['number'], 'status': 'DRAFT', 'remote_commit': commit['sha'], 'published_at': now()}
            await self.store.db.pull_requests.update_one({'id': pr['id']}, {'$set': patch})
            await self.store.db.jobs.update_one({'id': job_id}, {'$set': {'status': 'SUBMITTED', 'pr_url': response['html_url']}})
            await self.store.db.publication_attempts.update_one({'id': job_id}, {'$set': {'status': 'CONFIRMED', 'pr_number': response['number']}})
            await self.store.audit('pr_manager', 'Real draft PR created', response['html_url'], job_id=job_id, actor=actor, authorization='HUMAN_APPROVED')
            return {**pr, **patch}
        except Exception:
            await self.store.db.publication_attempts.update_one({'id': job_id}, {'$set': {'status': 'FAILED_OR_AMBIGUOUS'}})
            await self.store.audit('pr_manager', 'Publication requires reconciliation', 'Some writes may have succeeded; no retries or false PR success', 'BLOCKED', job_id)
            raise

    async def _adopt(self, job, pr, remote, actor, state, reason):
        head_sha = (remote.get('head') or {}).get('sha') or pr.get('remote_commit')
        patch = {'external': True, 'url': remote['html_url'], 'number': remote['number'],
                 'status': state, 'remote_commit': head_sha, 'published_at': now()}
        await self.store.db.pull_requests.update_one({'id': pr['id']}, {'$set': patch})
        job_status = 'SUBMITTED' if state in ['DRAFT', 'UNDER_REVIEW'] else state
        await self.store.db.jobs.update_one({'id': job['id']}, {'$set': {'status': job_status, 'pr_url': remote['html_url']}})
        await self.store.db.publication_attempts.update_one(
            {'id': job['id']}, {'$set': {'status': 'CONFIRMED', 'pr_number': remote['number'], 'last_error': None}})
        await self.store.audit('pr_manager', 'Publication reconciled', f"{reason}: {remote['html_url']}", 'SUCCESS',
                               job_id=job['id'], actor=actor, authorization='HUMAN_APPROVED')

    async def reconcile(self, job_id, actor):
        """Read-first recovery for an ambiguous publication. Never merges, never blind-retries.
        Inspects true remote state: adopt an existing PR, complete only a missing PR for an
        already-pushed branch, or clear a consumed lock when nothing persisted remotely."""
        job = await self.store.one('jobs', {'id': job_id})
        if not job:
            raise HTTPException(404, 'Job not found')
        await self.gate(job)  # all safety gates (approval, tests, security, review, artifact, connection) still enforced
        attempt = await self.store.one('publication_attempts', {'id': job_id})
        if not attempt:
            raise HTTPException(409, 'No publication attempt to reconcile; use the publish flow')
        if attempt.get('status') == 'CONFIRMED':
            raise HTTPException(409, 'Publication already confirmed; use synchronization for status')
        pr = await self.store.one('pull_requests', {'id': job['pr_id']})
        if not pr:
            raise HTTPException(409, 'Reviewed PR artifact missing')
        from .repos import job_targets
        t = job_targets(job)
        push_base, pr_base = '/repos/' + t['push_repo'], '/repos/' + t['pr_repo']
        # 1. A PR may already exist for our head branch on the publication (upstream) repo: adopt it, never duplicate.
        existing = await self.github.request('GET', f"{pr_base}/pulls?state=all&head={t['head']}&per_page=10")
        if existing:
            p = existing[0]
            state = 'MERGED' if p.get('merged') else 'REJECTED' if p.get('state') == 'closed' else 'DRAFT' if p.get('draft') else 'UNDER_REVIEW'
            await self._adopt(job, pr, p, actor, state, 'adopted existing remote PR')
            return {'reconciled': 'ADOPTED_EXISTING_PR', 'url': p['html_url'], 'number': p['number'], 'status': state}
        # 2. No PR, but the branch+commit persisted on the push (fork/execution) repo: complete ONLY the missing draft PR.
        remote_commit = attempt.get('remote_commit')
        try:
            ref = await self.github.request('GET', push_base + '/git/ref/heads/' + job['branch'])
        except HTTPException:
            ref = None
        if ref and remote_commit and ref.get('object', {}).get('sha') == remote_commit:
            response = await self.write(job, pr_base + '/pulls', {'title': pr['title'], 'body': pr['body'],
                                                                  'head': t['head'], 'base': job['base_branch'], 'draft': True})
            if response.get('draft') is not True or not isinstance(response.get('number'), int):
                await self.store.db.publication_attempts.update_one({'id': job_id}, {'$set': {'status': 'FAILED_OR_AMBIGUOUS'}})
                raise HTTPException(502, 'GitHub draft response not confirmed; reconcile again')
            await self._adopt(job, pr, response, actor, 'DRAFT', 'created missing PR for reconciled branch')
            return {'reconciled': 'CREATED_MISSING_PR', 'url': response['html_url'], 'number': response['number'], 'status': 'DRAFT'}
        # 3. Nothing persisted remotely: clearing the consumed lock makes a fresh publish safe.
        await self.store.db.publication_attempts.delete_one({'id': job_id})
        await self.store.audit('pr_manager', 'Publication reconciled: no remote mutation',
                               'Consumed attempt cleared; a fresh publish is safe', 'SUCCESS', job_id=job_id, actor=actor)
        return {'reconciled': 'NO_REMOTE_STATE', 'status': 'READY_FOR_PUBLISH'}

    async def sync(self, job_id, actor):
        job = await self.store.one('jobs', {'id': job_id})
        if not job or job.get('simulated'):
            raise HTTPException(409, 'A real job is required for GitHub synchronization')
        pr = await self.store.one('pull_requests', {'job_id': job_id})
        if not pr or not pr.get('external'):
            raise HTTPException(409, 'No confirmed external PR; inspect GitHub before resolving an ambiguous publication')
        actual = await self.github.pr_details(job['repository'], pr['number'])
        details = actual['pr']
        state = 'MERGED' if details.get('merged') else 'REJECTED' if details.get('state') == 'closed' else 'DRAFT' if details.get('draft') else 'UNDER_REVIEW'
        await self.store.db.pull_requests.update_one({'id': pr['id']}, {'$set': {'status': state, 'last_synced_at': now()}})
        await self.store.db.jobs.update_one({'id': job_id}, {'$set': {'status': state, 'payment_status': 'PENDING'}})
        await self.store.audit('outcome', 'Real PR status synchronized', f'{state}; no payment or revenue inferred', job_id=job_id, actor=actor)
        return {'status': state, 'url': pr['url'], 'payment_status': 'PENDING'}