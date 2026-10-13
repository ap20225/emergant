from typing import Literal

import asyncio
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from . import autonomy
from .budget import RunBudget
from .command_center import command_center
from .connections import Connections, GitHubConnectionInput, RunnerConnectionInput
from .gateway import ExperientialGateway
from .live_worker import LiveWorker
from .publication import Publisher
from .readiness import execution_status
from .routing_policy import RoutingPolicy, policy_view, save_policy
from .runner import RemoteRunner


async def _create_github_fork(store, github, upstream, fork_repo, actor):
    """Narrow, owner-approved, audited GitHub write: create a fork of `upstream` into the connected
    account. Forks ONLY — cannot merge, push code or open PRs. Idempotent (returns an existing fork).
    Blocked by emergency policy like every other write."""
    from .secrets import SecretVault
    controls = await store.controls()
    if any(controls.get(k) for k in ('integrations_revoked', 'global_stop', 'submissions_disabled')):
        raise HTTPException(409, 'Integrations disabled by emergency policy')
    token = SecretVault().get('github')
    if not token:
        raise HTTPException(409, 'No GitHub credential to create the fork')
    try:
        existing = await github.request('GET', f'/repos/{fork_repo}')
        if existing:
            return existing
    except HTTPException:
        pass
    await store.audit('master', 'GitHub fork create attempted', f'{upstream} -> {fork_repo}', actor=actor, authorization='HUMAN_APPROVED')
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        res = await client.post(os.environ['GITHUB_API_URL'] + f'/repos/{upstream}/forks',
                                headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                         'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']}, json={})
    if res.status_code not in (200, 201, 202):
        raise HTTPException(res.status_code if res.status_code in (401, 403, 404) else 502,
                            f'Fork creation failed: HTTP {res.status_code}')
    for _ in range(20):  # GitHub forks are created asynchronously; poll until queryable
        await asyncio.sleep(3)
        try:
            r = await github.request('GET', f'/repos/{fork_repo}')
            if r:
                await store.audit('master', 'GitHub fork created', f'{upstream} -> {fork_repo}', 'SUCCESS', actor=actor, authorization='HUMAN_APPROVED')
                return r
        except HTTPException:
            continue
    raise HTTPException(504, 'Fork created but not yet queryable; retry authorize shortly')


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                # Pydantic's default detail includes submitted values; never echo secrets.
                raise HTTPException(422, 'Invalid configuration fields. Check repository, issue number and credential format.')
        return handler


class ExecutionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    issue_number: int = Field(gt=0)
    test_suite: Literal['pytest', 'unittest', 'jest', 'vitest', 'go']
    confirm_execution: bool


class PublishInput(BaseModel):
    confirm_publication: bool


class FlagInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=60)
    value: bool


class DiscoveryRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository: str | None = Field(default=None, max_length=180)
    issue_number: int | None = None
    source_identifier: str | None = Field(default=None, max_length=200)
    source_url: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    reward: float | None = None
    currency: str | None = Field(default=None, max_length=10)
    requirements: str | None = Field(default=None, max_length=2000)
    deadline: str | None = Field(default=None, max_length=40)


class DiscoveryIngestInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str = Field(pattern=r'^[a-z0-9_-]{1,40}$')
    records: list[DiscoveryRecord] = Field(min_length=1, max_length=100)


class DiscoveryConfigInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(default='is:issue is:open label:bounty', min_length=1, max_length=256)
    per_page: int = Field(default=20, ge=1, le=50)
    enabled: bool = False


class RepoAuthInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
    execution_enabled: bool = True
    publication_enabled: bool = True
    fork_strategy: Literal['FORK_AND_PR', 'DIRECT_WRITE'] = 'FORK_AND_PR'
    expires_at: str | None = Field(default=None, max_length=40)
    account_identity: str | None = Field(default=None, max_length=120)


class RepoStatusInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
    status: Literal['DISCOVERED', 'READ_ONLY', 'AUTHORIZED', 'PAUSED', 'REVOKED', 'BLOCKED']


class RepoForkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
    fork_repo: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')


class RepoCapabilityInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')


class ForkAuthorizeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    upstream: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
    fork_repo: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
    expires_at: str | None = Field(default=None, max_length=40)
    create_fork: bool = False


class Response(BaseModel):
    model_config = ConfigDict(extra='allow')


async def _controls_view(store):
    from .execution_controls import get_budget
    from .authorization import AutonomousBudget
    from .autonomy import get_flags
    flags = await get_flags(store)
    budget = await get_budget(store)
    ab = await AutonomousBudget(store).state()
    row = await store.one('system_settings', {'id': 'autonomous-driver-state'})
    driver = (row or {}).get('config') or {'status': 'STOPPED', 'reason': 'DISABLED', 'eligible_count': 0}
    return {'autonomous_execution': flags['AUTONOMOUS_EXECUTION_ENABLED'],
            'autonomous_escalation': flags['AUTO_ESCALATION_ENABLED'],
            'background_discovery': flags['SCHEDULER_BACKGROUND_ENABLED'],
            'daily_budget_nano_usd': budget['daily_budget_nano_usd'],
            'per_job_cap_nano_usd': budget['per_job_cap_nano_usd'],
            'max_concurrent_jobs': budget['max_concurrent_jobs'],
            'spent_today_nano_usd': ab['spent_nano_usd'],
            'reserved_today_nano_usd': ab['reserved_nano_usd'],
            'remaining_today_nano_usd': ab['remaining_nano_usd'],
            'driver': driver}


def execution_router(store, orchestrator, github, owner):
    router = APIRouter(route_class=SecretSafeRoute)
    connections, gateway = Connections(store), ExperientialGateway(store)
    worker, publisher = LiveWorker(store, orchestrator, github, connections), Publisher(store, github)
    from .execution_controller import ExecutionController, execution_policy
    controller = ExecutionController(store, orchestrator, github)
    orchestrator.live_worker = worker

    class RunBountyInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        opportunity_id: str = Field(min_length=1, max_length=120)
        confirm: bool = True
        owner_approved: bool = False
        acknowledge_unverified_payout: bool = False

    class ExecutionPolicyInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        max_total_model_attempts: int = Field(default=6, ge=1, le=8)
        max_attempts_per_model: int = Field(default=2, ge=1, le=4)
        max_corrections_per_model: int = Field(default=2, ge=0, le=3)
        max_total_corrections: int = Field(default=3, ge=0, le=6)
        max_timeout_retries_per_model: int = Field(default=2, ge=0, le=3)
        max_model_escalations: int = Field(default=2, ge=0, le=4)

    class ExecutionControlsInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        autonomous_execution: bool | None = None
        autonomous_escalation: bool | None = None
        background_discovery: bool | None = None
        confirm_autonomous: bool = False
        daily_budget_nano_usd: int | None = Field(default=None, ge=0, le=1_000_000_000_000)
        per_job_cap_nano_usd: int | None = Field(default=None, ge=0, le=100_000_000_000)
        max_concurrent_jobs: int | None = Field(default=None, ge=1, le=1)

    @router.post('/execution/bounty/run', response_model=Response)
    async def run_bounty(body: RunBountyInput, user=Depends(owner)):
        if not body.confirm:
            raise HTTPException(409, 'Explicit RUN BOUNTY confirmation required')
        return await controller.run_bounty(body.opportunity_id, user.user_id,
                                            owner_approved=body.owner_approved,
                                            acknowledge_unverified_payout=body.acknowledge_unverified_payout)

    @router.get('/execution/bounty/{job_id}/state', response_model=Response)
    async def bounty_state(job_id: str, user=Depends(owner)):
        return await controller.job_state(job_id)

    @router.post('/execution/bounty/{job_id}/stop', response_model=Response)
    async def bounty_stop(job_id: str, user=Depends(owner)):
        return await controller.stop(job_id, user.user_id)

    @router.get('/execution/policy', response_model=Response)
    async def get_execution_policy(user=Depends(owner)):
        return await execution_policy(store)

    @router.put('/execution/policy', response_model=Response)
    async def put_execution_policy(body: ExecutionPolicyInput, user=Depends(owner)):
        cfg = body.model_dump()
        await store.db.system_settings.update_one({'id': 'execution-policy'}, {'$set': {'config': cfg}}, upsert=True)
        await store.audit('master', 'Execution policy saved', 'Bounded attempts/corrections updated; budget cap unchanged', actor=user.user_id)
        return await execution_policy(store)

    @router.get('/execution/controls', response_model=Response)
    async def get_execution_controls(user=Depends(owner)):
        return await _controls_view(store)

    @router.put('/execution/controls', response_model=Response)
    async def put_execution_controls(body: ExecutionControlsInput, user=Depends(owner)):
        from .execution_controls import get_budget, save_budget
        if body.autonomous_execution is True:
            flags = await autonomy.get_flags(store)
            if not flags['AUTONOMOUS_EXECUTION_ENABLED'] and not body.confirm_autonomous:
                raise HTTPException(409, 'Explicit confirmation required to enable autonomous execution (real provider spend and GitHub writes)')
        patch = {k: v for k, v in {'daily_budget_nano_usd': body.daily_budget_nano_usd,
                 'per_job_cap_nano_usd': body.per_job_cap_nano_usd,
                 'max_concurrent_jobs': body.max_concurrent_jobs}.items() if v is not None}
        if patch:
            b = await get_budget(store)
            daily = patch.get('daily_budget_nano_usd', b['daily_budget_nano_usd'])
            per_job = patch.get('per_job_cap_nano_usd', b['per_job_cap_nano_usd'])
            if per_job > daily:
                raise HTTPException(422, 'Per-job cap cannot exceed the daily budget; one job must never consume the whole master budget')
            await save_budget(store, patch, user.user_id)
        if body.autonomous_execution is not None:
            await autonomy.set_flag(store, 'AUTONOMOUS_EXECUTION_ENABLED', body.autonomous_execution, user.user_id)
        if body.autonomous_escalation is not None:
            await autonomy.set_flag(store, 'AUTO_ESCALATION_ENABLED', body.autonomous_escalation, user.user_id)
        if body.background_discovery is not None:
            await autonomy.set_flag(store, 'SCHEDULER_BACKGROUND_ENABLED', body.background_discovery, user.user_id)
        return await _controls_view(store)

    @router.get('/execution/status', response_model=Response)
    async def status(user=Depends(owner)):
        return await execution_status(store)

    @router.get('/command-center', response_model=Response)
    async def command_center_status(user=Depends(owner)):
        # Read-only aggregation of existing services/stores. No mutation, spend, or execution authority.
        return await command_center(store)

    @router.get('/routing-policy', response_model=Response)
    async def routing_status(user=Depends(owner)):
        return await policy_view(store)

    @router.put('/routing-policy', response_model=Response)
    async def routing_save(body: RoutingPolicy, user=Depends(owner)):
        await save_policy(store, body, user.user_id)
        return await policy_view(store)

    @router.post('/connections/github', response_model=Response)
    async def connect(body: GitHubConnectionInput, user=Depends(owner)):
        return await connections.connect_github(body, user.user_id)

    @router.post('/connections/github/verify', response_model=Response)
    async def verify(user=Depends(owner)):
        return await connections.verify_saved_github(user.user_id)

    @router.post('/connections/github/verify-write', response_model=Response)
    async def verify_write(user=Depends(owner)):
        return await connections.verify_github_write(user.user_id)

    @router.delete('/connections/{name}', response_model=Response)
    async def disconnect(name: str, user=Depends(owner)):
        await orchestrator.stop()
        await connections.disconnect(name, user.user_id)
        return {'status': 'DISCONNECTED'}

    @router.post('/connections/runner', response_model=Response)
    async def runner(body: RunnerConnectionInput, user=Depends(owner)):
        return await connections.configure_runner(body, user.user_id)

    @router.post('/connections/runner/verify', response_model=Response)
    async def runner_verify(user=Depends(owner)):
        return await RemoteRunner(store).verify(user.user_id)

    @router.post('/connections/model/catalog', response_model=Response)
    async def catalog(user=Depends(owner)):
        await gateway.catalog(user.user_id)
        return (await execution_status(store))['model']

    @router.post('/connections/model/diagnostic', response_model=Response)
    async def diagnostic(user=Depends(owner)):
        return await gateway.diagnostic(user.user_id)

    @router.post('/execution/start', response_model=Response)
    async def start(body: ExecutionInput, user=Depends(owner)):
        if not body.confirm_execution:
            raise HTTPException(409, 'Explicit controlled execution approval required')
        return await worker.start(body.issue_number, body.test_suite, user.user_id)

    @router.post('/execution/{job_id}/publish', response_model=Response)
    async def publish(job_id: str, body: PublishInput, user=Depends(owner)):
        if not body.confirm_publication:
            raise HTTPException(409, 'Explicit draft PR publication confirmation required')
        async with orchestrator.lock:
            return await publisher.publish(job_id, user.user_id)

    @router.post('/execution/{job_id}/sync', response_model=Response)
    async def sync(job_id: str, user=Depends(owner)):
        return await publisher.sync(job_id, user.user_id)

    @router.post('/execution/{job_id}/publication/reconcile', response_model=Response)
    async def publication_reconcile(job_id: str, user=Depends(owner)):
        # Read-first recovery of an ambiguous publication. Never merges, never blind-retries.
        async with orchestrator.lock:
            return await publisher.reconcile(job_id, user.user_id)

    @router.get('/execution/{job_id}/report', response_model=Response)
    async def report(job_id: str, user=Depends(owner)):
        job = await store.one('jobs', {'id': job_id})
        if not job:
            raise HTTPException(404, 'Job not found')
        return {'job': {k: v for k, v in job.items() if k not in ['_id', 'patch_files']},
                'model_usage': await store.rows('model_usage', {'job_id': job_id}),
                'routing_decisions': await store.rows('routing_decisions', {'job_id': job_id}),
                'tests': await store.rows('test_runs', {'job_id': job_id}),
                'security_reviews': await store.rows('security_reviews', {'job_id': job_id}),
                'code_reviews': await store.rows('code_reviews', {'job_id': job_id}),
                'pull_request': await store.one('pull_requests', {'job_id': job_id}),
                'budget': await RunBudget(store).state()}

    @router.get('/flags', response_model=Response)
    async def get_feature_flags(user=Depends(owner)):
        return await autonomy.get_flags(store)

    @router.put('/flags', response_model=Response)
    async def put_feature_flag(body: FlagInput, user=Depends(owner)):
        return await autonomy.set_flag(store, body.name, body.value, user.user_id)

    @router.get('/opportunities', response_model=Response)
    async def opportunities(user=Depends(owner)):
        return {'opportunities': await store.rows('bounty_opportunities', {}, limit=200)}

    @router.post('/discovery/ingest', response_model=Response)
    async def discovery_ingest(body: DiscoveryIngestInput, user=Depends(owner)):
        return await autonomy.Discovery(store).ingest(body.source, [r.model_dump() for r in body.records], user.user_id)

    @router.post('/opportunities/{opportunity_id}/qualify', response_model=Response)
    async def qualify_opportunity(opportunity_id: str, user=Depends(owner)):
        return await autonomy.Qualification(store).qualify(opportunity_id, user.user_id)

    @router.post('/scheduler/run', response_model=Response)
    async def scheduler_run(user=Depends(owner)):
        return await autonomy.Scheduler(store).run_once(actor=user.user_id)

    @router.get('/sources/health', response_model=Response)
    async def sources_health(user=Depends(owner)):
        return {'sources': await store.rows('source_health', {}, limit=50)}

    @router.get('/discovery/config', response_model=Response)
    async def get_discovery_config(user=Depends(owner)):
        row = await store.one('system_settings', {'id': 'discovery-config'})
        return (row or {}).get('config') or {'query': 'is:issue is:open label:bounty', 'per_page': 20, 'enabled': False}

    @router.put('/discovery/config', response_model=Response)
    async def put_discovery_config(body: DiscoveryConfigInput, user=Depends(owner)):
        cfg = body.model_dump()
        await store.db.system_settings.update_one({'id': 'discovery-config'}, {'$set': {'config': cfg}}, upsert=True)
        await store.audit('master', 'Discovery config saved', f"query set; enabled={cfg['enabled']}; read-only search only", actor=user.user_id)
        return cfg

    @router.post('/discovery/github/run', response_model=Response)
    async def discovery_github_run(user=Depends(owner)):
        row = await store.one('system_settings', {'id': 'discovery-config'})
        cfg = (row or {}).get('config') or {'query': 'is:issue is:open label:bounty', 'per_page': 20}
        return await autonomy.Discovery(store).run_source(autonomy.GitHubIssueAdapter(store), cfg, user.user_id)

    @router.post('/opportunities/{opportunity_id}/recheck', response_model=Response)
    async def recheck_opportunity(opportunity_id: str, user=Depends(owner)):
        return await autonomy.LiveRecheck(store).recheck(opportunity_id, actor=user.user_id)

    @router.get('/briefing/latest', response_model=Response)
    async def briefing_latest(user=Depends(owner)):
        rows = await store.rows('briefings', {}, limit=1)
        return rows[0] if rows else {'status': 'VERIFIED DATA UNAVAILABLE'}

    @router.post('/briefing/run', response_model=Response)
    async def briefing_run(user=Depends(owner)):
        return await autonomy.Briefing(store).build(user.user_id)

    # ---- Per-repository authorization registry (owner-only). READ_ONLY by default; writes never auto-granted. ----
    @router.get('/repos', response_model=Response)
    async def repos_list(user=Depends(owner)):
        from .authorization import AutonomousBudget
        return {'repositories': await store.rows('repo_authorizations', {}, limit=200),
                'autonomous_budget': await AutonomousBudget(store).state()}

    @router.post('/repos/authorize', response_model=Response)
    async def repos_authorize(body: RepoAuthInput, user=Depends(owner)):
        from .repos import RepoRegistry
        return await RepoRegistry(store).authorize(body.repository, user.user_id, body.execution_enabled,
                                                   body.publication_enabled, body.expires_at, body.fork_strategy, body.account_identity)

    @router.post('/repos/status', response_model=Response)
    async def repos_status(body: RepoStatusInput, user=Depends(owner)):
        from .repos import RepoRegistry
        return await RepoRegistry(store).set_status(body.repository, body.status, user.user_id)

    @router.post('/repos/fork', response_model=Response)
    async def repos_fork(body: RepoForkInput, user=Depends(owner)):
        from .repos import RepoRegistry
        return await RepoRegistry(store).set_fork(body.repository, body.fork_repo, user.user_id)

    @router.post('/repos/capabilities/check', response_model=Response)
    async def repos_capabilities(body: RepoCapabilityInput, user=Depends(owner)):
        from .repos import RepoRegistry, check_capabilities
        registry = RepoRegistry(store)
        await registry.register_discovered(body.repository, actor=user.user_id)
        caps, meta = await check_capabilities(github, body.repository)
        await registry.record_capabilities(body.repository, caps)
        return {'repository': body.repository, 'capabilities': caps, 'meta': meta}

    @router.get('/execution/recommended-candidate', response_model=Response)
    async def recommended_candidate(refresh: bool = False, user=Depends(owner)):
        """READ-ONLY. Deterministically select ONE bounded, payable, open, executable candidate from live
        discovery. GitHub reads only — no execution, no spend. Cached (~10 min) so it returns fast; the
        owner must click RUN BOUNTY to execute."""
        from .candidate_selection import select_recommended, get_cached
        if not refresh:
            cached = await get_cached(store)
            if cached:
                return cached
        return await select_recommended(store, github)

    @router.post('/repos/fork/authorize', response_model=Response)
    async def repos_fork_authorize(body: ForkAuthorizeInput, user=Depends(owner)):
        """Pre-authorize a fork the owner controls as the write target for an upstream repo. Fail-closed:
        the fork must be a real GitHub fork that the connected account can push to (CREATE_PR). With
        create_fork=true, create the fork first via a narrow owner-approved GitHub write (forks only)."""
        from .repos import RepoRegistry, check_capabilities, resolve_targets
        if body.fork_repo.lower() == body.upstream.lower():
            raise HTTPException(422, 'Fork target must differ from the upstream repository')
        registry = RepoRegistry(store)
        await registry.register_discovered(body.upstream, actor=user.user_id)
        if body.create_fork:
            await _create_github_fork(store, github, body.upstream, body.fork_repo, user.user_id)
        caps, meta = await check_capabilities(github, body.fork_repo)
        if not meta.get('fork'):
            raise HTTPException(409, 'Provided fork_repo is not a GitHub fork')
        parent = (meta.get('parent') or {}).get('full_name') if isinstance(meta.get('parent'), dict) else None
        if parent and parent.lower() != body.upstream.lower():
            raise HTTPException(409, 'fork_repo is not a fork of the specified upstream repository')
        if not caps.get('CREATE_PR'):
            raise HTTPException(409, 'The connected GitHub account cannot push to this fork (no write/CREATE_PR permission)')
        account_identity = body.fork_repo.split('/')[0]
        await registry.register_discovered(body.fork_repo, account_identity=account_identity, actor=user.user_id)
        await registry.record_capabilities(body.fork_repo, caps)
        await registry.authorize(body.fork_repo, user.user_id, execution=True, publication=True,
                                 expires_at=body.expires_at, fork_strategy='FORK_AND_PR', account_identity=account_identity)
        await registry.set_fork(body.upstream, body.fork_repo, user.user_id)
        targets = await resolve_targets(registry, {'repository': body.upstream})
        return {'upstream': body.upstream, 'fork_repo': body.fork_repo,
                'write_ready': targets['write_ready'], 'strategy': targets['strategy'], 'capabilities': caps}
        if not meta.get('fork'):
            raise HTTPException(409, 'Provided fork_repo is not a GitHub fork')
        parent = (meta.get('parent') or {}).get('full_name') if isinstance(meta.get('parent'), dict) else None
        if parent and parent.lower() != body.upstream.lower():
            raise HTTPException(409, 'fork_repo is not a fork of the specified upstream repository')
        if not caps.get('CREATE_PR'):
            raise HTTPException(409, 'The connected GitHub account cannot push to this fork (no write/CREATE_PR permission)')
        account_identity = body.fork_repo.split('/')[0]
        await registry.register_discovered(body.fork_repo, account_identity=account_identity, actor=user.user_id)
        await registry.record_capabilities(body.fork_repo, caps)
        await registry.authorize(body.fork_repo, user.user_id, execution=True, publication=True,
                                 expires_at=body.expires_at, fork_strategy='FORK_AND_PR', account_identity=account_identity)
        await registry.set_fork(body.upstream, body.fork_repo, user.user_id)
        targets = await resolve_targets(registry, {'repository': body.upstream})
        return {'upstream': body.upstream, 'fork_repo': body.fork_repo,
                'write_ready': targets['write_ready'], 'strategy': targets['strategy'], 'capabilities': caps}

    @router.post('/opportunities/{opportunity_id}/authorization-preview', response_model=Response)
    async def authorization_preview(opportunity_id: str, user=Depends(owner)):
        from .authorization import AutonomousAuthorization
        o = await store.one('bounty_opportunities', {'id': opportunity_id})
        if not o:
            raise HTTPException(404, 'Opportunity not found')
        # Read-only policy evaluation. Never authorizes execution or spends budget.
        return await AutonomousAuthorization(store).evaluate(o)

    @router.get('/execution/review-queue', response_model=Response)
    async def review_queue(user=Depends(owner)):
        """Sort real runs into honest buckets so the queue means 'needs YOU'. Read-only."""
        from .runnable import review_bucket, NEEDS_YOUR_REVIEW, DECLINED_BY_ENGINE, BLOCKED_EXTERNALLY
        jobs = await store.rows('jobs', {'status': {'$in': ['WAITING_APPROVAL', 'APPROVED', 'NEEDS_REVIEW']},
                                         'archived': {'$ne': True}, 'parent_job': {'$exists': False},
                                         'simulated': {'$ne': True}}, limit=500)
        buckets = {NEEDS_YOUR_REVIEW: [], DECLINED_BY_ENGINE: [], BLOCKED_EXTERNALLY: []}
        for j in jobs:
            rb = review_bucket(j)
            if rb['bucket'] not in buckets:
                continue
            buckets[rb['bucket']].append({
                'id': j['id'], 'title': j.get('title'), 'repository': j.get('repository'),
                'issue_number': j.get('issue_number'), 'status': j.get('status'),
                'bounty_id': j.get('bounty_id'), 'pr_url': j.get('pr_url'), 'created_at': j.get('created_at'),
                'outcome': rb['outcome'], 'error': j.get('error')})
        return {'buckets': buckets,
                'counts': {k: len(v) for k, v in buckets.items()}}

    class ArchiveInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        job_ids: list[str] = Field(default_factory=list, max_length=500)
        bucket: Literal['DECLINED_BY_ENGINE', 'BLOCKED_EXTERNALLY'] | None = None

    @router.post('/jobs/{job_id}/archive', response_model=Response)
    async def archive_job(job_id: str, user=Depends(owner)):
        job = await store.one('jobs', {'id': job_id})
        if not job:
            raise HTTPException(404, 'Job not found')
        await store.db.jobs.update_one({'id': job_id}, {'$set': {'archived': True}})
        await store.audit('master', 'Review item dismissed', f'{job_id} archived from the review queue', actor=user.user_id)
        return {'archived': True, 'job_id': job_id}

    @router.post('/jobs/archive-bulk', response_model=Response)
    async def archive_bulk(body: ArchiveInput, user=Depends(owner)):
        from .runnable import review_bucket
        ids = set(body.job_ids)
        if body.bucket:
            for j in await store.rows('jobs', {'status': 'NEEDS_REVIEW', 'archived': {'$ne': True},
                                               'parent_job': {'$exists': False}, 'simulated': {'$ne': True}}, limit=500):
                if review_bucket(j)['bucket'] == body.bucket:
                    ids.add(j['id'])
        if not ids:
            return {'archived_count': 0}
        res = await store.db.jobs.update_many({'id': {'$in': list(ids)}}, {'$set': {'archived': True}})
        await store.audit('master', 'Review items dismissed (bulk)', f'{res.modified_count} archived; bucket={body.bucket}', actor=user.user_id)
        return {'archived_count': res.modified_count}

    return router