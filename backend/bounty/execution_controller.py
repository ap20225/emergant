"""Dashboard-callable execution of an authorized bounty through the EXISTING fork-first pipeline.

MANUAL RUN (owner clicks RUN BOUNTY) reuses the existing authorization checks, model router,
LiveWorker, E2B runner, security/review gates and reconciliation-aware Publisher. It does NOT
implement its own pipeline and does NOT require the global AUTONOMOUS_EXECUTION_ENABLED flag
(an owner click is explicit human authorization). Budget stays hard-capped by RunBudget.
"""
import asyncio
import os
import httpx
from fastapi import HTTPException

from .authorization import AutonomousBudget
from .budget import RunBudget
from .live_worker import LiveWorker
from .publication import Publisher
from .repos import RepoRegistry, resolve_targets, verify_execution_targets
from .secrets import SecretVault
from .store import now, uid, redact

LIVE_STATES = ['SELECTED', 'TRIAGE', 'RESEARCHING', 'CODING', 'TESTING', 'REVIEW', 'READY', 'WAITING_APPROVAL', 'APPROVED']


async def recover_orphaned_executions(store):
    """Startup recovery: a QUEUED job whose in-memory task was lost to a process restart can never
    resume on its own and permanently blocks the single-slot autonomous driver. Move every orphaned
    QUEUED job to NEEDS_REVIEW (never blindly resume partially executed code) and safely release its
    unspent per-job budget reservation back to the daily master. Returns the recovered count."""
    from .authorization import AutonomousBudget
    budget = AutonomousBudget(store)
    orphans = await store.rows('jobs', {'status': 'QUEUED'}, limit=500)
    for job in orphans:
        res = job.get('budget_reservation_nano_usd')
        res_date = job.get('budget_reservation_date') or (job.get('created_at') or now())[:10]
        if res:
            try:
                await budget.release(job['id'], res, date=res_date)
            except Exception:
                pass
        await store.db.jobs.update_one({'id': job['id']}, {'$set': {
            'status': 'NEEDS_REVIEW', 'next_action': 'Needs Review',
            'error': 'Process restarted while this run was queued; it was not resumed (no blind restart of partially executed code). Budget reservation released. Owner review required.'}})
        await store.audit('master', 'Recovered orphaned QUEUED job',
                          'No unsafe automatic restart; budget reservation reconciled; owner review required', 'BLOCKED', job['id'])
    return len(orphans)


async def execution_policy(store):
    row = await store.one('system_settings', {'id': 'execution-policy'})
    cfg = (row or {}).get('config') or {}
    return {'max_total_model_attempts': cfg.get('max_total_model_attempts', 6),
            'max_attempts_per_model': cfg.get('max_attempts_per_model', 2),
            'max_corrections_per_model': cfg.get('max_corrections_per_model', 2),
            'max_total_corrections': cfg.get('max_total_corrections', 3),
            'max_timeout_retries_per_model': cfg.get('max_timeout_retries_per_model', 2),
            'max_model_escalations': cfg.get('max_model_escalations', 2),
            'per_job_budget_nano_usd': cfg.get('per_job_budget_nano_usd', await RunBudget(store).cap()),
            'timeouts': {'routine': int(os.environ.get('EXPLABS_TIMEOUT_ROUTINE_SECONDS', 45)),
                         'coding': int(os.environ.get('EXPLABS_TIMEOUT_CODING_SECONDS', 90)),
                         'flagship_coding': int(os.environ.get('EXPLABS_TIMEOUT_FLAGSHIP_SECONDS', 120))}}


def classify_failure(reason):
    r = (reason or '').lower()
    if any(k in r for k in ['budget', 'cap_would_exceed', 'run_not_authorized']):
        return 'BUDGET'
    if any(k in r for k in ['blocked_write', 'not authorized', 'credential', 'permission', 'repository read blocked', 'security']):
        return 'HARD_STOP'
    if any(k in r for k in ['failed or ambiguous', 'timeout', 'http 5', '502', '503', '429']):
        return 'TRANSIENT'
    if 'regression tests did not pass' in r or 'test' in r:
        return 'TEST_FAILURE'
    if 'review' in r:
        return 'REVIEW'
    return 'OTHER'


def next_action(failure_class):
    return {'TRANSIENT': 'Retry', 'TEST_FAILURE': 'Retry', 'REVIEW': 'Needs Review',
            'BUDGET': 'Blocked', 'HARD_STOP': 'Blocked'}.get(failure_class, 'Needs Review')


class ExecutionController:
    def __init__(self, store, orchestrator, github):
        self.store = store
        self.orchestrator = orchestrator
        self.github = github
        from .connections import Connections
        self.worker = LiveWorker(store, orchestrator, github, Connections(store))
        self.publisher = Publisher(store, github)

    async def _claim_if_required(self, upstream, issue_number, ev):
        """DB-guarded, single-post Opire claim. Never posts twice; never re-keys prior claims."""
        key = f'{upstream}#{issue_number}'
        if await self.store.one('bounty_claims', {'id': key}):
            return 'ALREADY_CLAIMED'
        issue = await self.github.request('GET', f'/repos/{upstream}/issues/{issue_number}')
        if '/opire try' not in (issue.get('body') or '').lower():
            return 'NOT_REQUIRED'
        token = SecretVault().get('github')
        if not token:
            raise HTTPException(409, 'CLAIM_BLOCKED: no GitHub credential')
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            res = await c.post(os.environ['GITHUB_API_URL'] + f'/repos/{upstream}/issues/{issue_number}/comments',
                               headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                        'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']}, json={'body': '/opire try'})
        if res.status_code in (200, 201):
            from pymongo.errors import DuplicateKeyError
            body = res.json()
            try:
                await self.store.insert('bounty_claims', {'id': key, 'comment_url': body.get('html_url'), 'body': '/opire try'})
            except DuplicateKeyError:
                pass
            return 'CLAIMED_NOW'
        raise HTTPException(res.status_code if res.status_code in (401, 403) else 502,
                            f'CLAIM_BLOCKED: HTTP {res.status_code}; claim not made')

    async def run_bounty(self, opportunity_id, actor, owner_approved=False, acknowledge_unverified_payout=False):
        """Queue ONE manual run of an authorized bounty. Returns job_id immediately; pipeline runs in background."""
        async with self.orchestrator.lock:
            await self.orchestrator.gate(new=True)  # mode/global-stop gate (existing)
            opp = await self.store.one('bounty_opportunities', {'id': opportunity_id}) or await self.store.one('opportunities', {'id': opportunity_id})
            if not opp:
                raise HTTPException(404, 'Opportunity not found')
            upstream, issue_number = opp['repository'], opp.get('issue_number')
            if not issue_number:
                raise HTTPException(409, 'Opportunity has no issue number')
            # ---- existing authorization checks (owner-initiated == human authorization) ----
            reward_status = opp.get('reward_status') or ('VERIFIED' if opp.get('reward_verified') else 'UNKNOWN')
            # Platform API-confirmed rewards from trusted bounty sources are treated as verified
            TRUSTED_PLATFORM_SOURCES = {'opire', 'algora', 'taskbounty'}
            if reward_status != 'VERIFIED' and opp.get('payout_source_type') == 'platform_api' and opp.get('source') in TRUSTED_PLATFORM_SOURCES:
                auth_amount = opp.get('authoritative_reward_amount') or opp.get('reward_amount')
                if auth_amount and float(auth_amount) > 0:
                    reward_status = 'VERIFIED'
            payout_mode = 'VERIFIED'
            if reward_status != 'VERIFIED':
                # OWNER_APPROVAL path: an explicit owner approval authorizes the CODING WORK for a credible,
                # non-escrow opportunity. Payout REMAINS a human claim — never auto-verified, never auto-claimed,
                # the reward is never treated as guaranteed. This does NOT change payout-certainty rules.
                from .classification import classify
                if not (owner_approved and acknowledge_unverified_payout and classify(opp)['lane'] == 'OWNER_APPROVAL'):
                    raise HTTPException(409, 'BLOCKED: reward not VERIFIED (OWNER_APPROVAL-lane execution requires owner_approved + acknowledge_unverified_payout)')
                payout_mode = 'OWNER_APPROVAL_HUMAN_CLAIM'
            if opp.get('qualification_status') != 'QUALIFIED':
                raise HTTPException(409, "This bounty hasn't been qualified yet — run Qualify on it first, then try again.")
            if opp.get('risk_status') != 'CLEARED':
                raise HTTPException(409, "The safety / prompt-injection check hasn't cleared this bounty yet.")
            reg = RepoRegistry(self.store)
            # Live issue is the identity source of truth (stored discovery title can be stale/mis-populated).
            issue = await self.github.request('GET', f'/repos/{upstream}/issues/{issue_number}')
            if issue.get('state') != 'open' or 'pull_request' in issue:
                raise HTTPException(409, 'The GitHub issue is closed or is now a pull request — there is nothing to work on.')
            # Ensure a write target: auto-create + authorize the owner's fork if none is ready yet.
            pre = await resolve_targets(reg, {'repository': upstream})
            if not pre.get('write_ready'):
                from .repos import create_and_authorize_fork
                await create_and_authorize_fork(self.store, self.github, upstream, actor)
            targets = await verify_execution_targets(self.github, reg, {**opp, 'issue_number': issue_number, 'title': issue.get('title')})
            if opp.get('title') != issue.get('title'):
                await self.store.db.bounty_opportunities.update_one({'id': opportunity_id}, {'$set': {'title': issue.get('title')}})
            per_job_cap = await AutonomousBudget(self.store).per_job_cap()
            if not await AutonomousBudget(self.store).available(per_job_cap):
                raise HTTPException(409, 'The daily budget is used up. Raise it in Execution controls or wait for the next day.')
            # duplicate protection: only a LIVE/published attempt blocks; historical failures are preserved
            blocking = await self.store.db.jobs.count_documents({'repository': upstream, 'issue_number': issue_number, 'status': {'$in': LIVE_STATES + ['SUBMITTED']}})
            if blocking:
                raise HTTPException(409, 'A run already exists for this bounty (live or already published).')
            # claim (idempotent) BEFORE spend
            claim_status = await self._claim_if_required(upstream, issue_number, {})
            # authorize run resources (existing router/budget/runner)
            from .gateway import ExperientialGateway
            from .runner import RemoteRunner
            from .model_router import ModelRouter
            await ExperientialGateway(self.store).catalog('system')
            await RemoteRunner(self.store).preflight()
            await RunBudget(self.store).authorize(actor, f'manual:{upstream}#{issue_number}')
            await ModelRouter(self.store).authorize('coding', None)
            issue = await self.github.request('GET', f'/repos/{upstream}/issues/{issue_number}')
            repo_meta = await self.github.request('GET', f'/repos/{upstream}')
            default_branch = repo_meta.get('default_branch') or 'main'
            ref = await self.github.request('GET', f"/repos/{upstream}/git/ref/heads/{default_branch}")
            base_sha = ref['object']['sha']
            jid = uid('job')
            # Atomic daily-master reservation before any spend (never lets one job eat the whole budget).
            await AutonomousBudget(self.store).reserve(jid, per_job_cap)
            reservation_date = now()[:10]
            job = {'id': jid, 'opportunity_id': f'{opportunity_id}#live-{jid}', 'bounty_id': opportunity_id,
                   'repository': upstream, 'upstream_repo': upstream, 'execution_repo': targets['execution_repo'],
                   'publication_repo': targets['publication_repo'], 'execution_default_branch': default_branch,
                   'issue_number': issue_number, 'title': issue.get('title'), 'issue_body': redact((issue.get('body') or '')[:10000]),
                   'status': 'SELECTED', 'simulated': False, 'controlled_test': False, 'run_mode': 'MANUAL',
                   'reward': float(opp.get('reward_amount') or 0), 'reward_currency': opp.get('reward_currency'),
                   'reward_verified': reward_status == 'VERIFIED', 'payout_mode': payout_mode,
                   'payout_status': 'HUMAN_CLAIM_REQUIRED' if payout_mode != 'VERIFIED' else 'VERIFIED',
                   'expected_profit': 0, 'cost': None, 'elapsed_seconds': 0, 'claim_status': claim_status,
                   'test_status': 'BLOCKED', 'security_status': 'BLOCKED', 'review_status': 'BLOCKED',
                   'approval_status': 'WAITING_FOR_APPROVAL', 'base_sha': base_sha, 'base_branch': default_branch,
                   'branch': f'penniless/{jid}', 'test_suite': 'unittest', 'created_at': now(), 'files_changed': [], 'iteration': 0,
                   'attempt': 1, 'correction': 0,
                   'budget_reservation_nano_usd': per_job_cap, 'budget_reservation_date': reservation_date}
            await self.store.insert('jobs', job)
            await self.store.audit('master', 'Manual bounty run queued', f'{upstream}#{issue_number} via {targets["execution_repo"]}; draft PR only; no merge', job_id=jid, actor=actor, authorization='HUMAN_APPROVED')
            conn = await self.store.one('integration_connections', {'id': 'github'})
            await self.store.db.jobs.update_one({'id': job['id']}, {'$set': {'status': 'QUEUED'}})
            self.orchestrator.tasks[job['id']] = asyncio.create_task(self._execute_loop(job, conn, actor))
            return {'job_id': job['id'], 'status': 'QUEUED', 'claim_status': claim_status, 'stage': 'QUEUED'}

    async def _coding_ladder(self):
        from .routing_policy import get_policy, ordered_models
        cat = await self.store.one('integration_connections', {'id': 'experiential'}) or {}
        ids = cat.get('model_ids', [])
        pol = (await get_policy(self.store))['policy']
        ladder = list(ordered_models(pol['coding'], ids))
        for fb in ['qwen3-coder-next', 'gpt-5.1-codex-mini', 'claude-sonnet-4.6']:
            if fb in ids and fb not in ladder:
                ladder.append(fb)
        return ladder[:6] or ['qwen3-coder-flash']

    async def _force_coding_model(self, model):
        row = await self.store.one('system_settings', {'id': 'model-routing'})
        original = ((row or {}).get('config') or {}).get('coding')
        await self.store.db.system_settings.update_one({'id': 'model-routing'}, {'$set': {'config.coding': [model]}}, upsert=True)
        return original

    async def _restore_coding_model(self, original):
        if original is not None:
            await self.store.db.system_settings.update_one({'id': 'model-routing'}, {'$set': {'config.coding': original}})

    async def _make_child(self, parent, model):
        cid = uid('job')
        keys = ['repository', 'upstream_repo', 'execution_repo', 'publication_repo', 'execution_default_branch',
                'issue_number', 'title', 'issue_body', 'reward', 'reward_currency', 'base_sha', 'base_branch', 'test_suite']
        child = {k: parent[k] for k in keys}
        child.update({'id': cid, 'opportunity_id': f'{parent["bounty_id"]}#child-{cid}', 'bounty_id': parent['bounty_id'],
                      'parent_job': parent['id'], 'status': 'SELECTED', 'simulated': False, 'controlled_test': False,
                      'run_mode': 'MANUAL', 'expected_profit': 0, 'cost': None, 'elapsed_seconds': 0, 'coding_model': model,
                      'test_status': 'BLOCKED', 'security_status': 'BLOCKED', 'review_status': 'BLOCKED',
                      'approval_status': 'WAITING_FOR_APPROVAL', 'branch': f'penniless/{cid}', 'created_at': now(),
                      'files_changed': [], 'iteration': 0})
        await self.store.insert('jobs', child)
        return child

    async def _mirror(self, parent_id, child, attempt, correction, model):
        await self.store.db.jobs.update_one({'id': parent_id}, {'$set': {
            'status': child.get('status'), 'active_child': child['id'], 'attempt': attempt, 'correction': correction,
            'model': model, 'test_status': child.get('test_status'), 'security_status': child.get('security_status'),
            'review_status': child.get('review_status'), 'sandbox_id': child.get('sandbox_id')}})

    async def _budget_fits(self):
        b = await RunBudget(self.store).state()
        rb = RunBudget(self.store)
        need = rb.worst_case(9000, 12000) + 2 * rb.worst_case(3000, 1500)
        return (b.get('cap_nano_usd', 0) - b.get('spent_nano_usd', 0)) >= need

    async def _correction_text(self, child, failure_class):
        if failure_class == 'TEST_FAILURE':
            tr = await self.store.one('test_runs', {'job_id': child['id'], 'phase': 'regression'})
            out = (tr or {}).get('output', '')[:2500]
            return ('Your PREVIOUS attempt FAILED the repository tests. Exact test output:\n' + out +
                    '\nFix the implementation and/or tests so they AGREE and PASS with the same command. '
                    'Keep changes additive and strictly scoped; create every temp dir/file the test needs; do not modify unrelated files.')
        rv = await self.store.one('code_reviews', {'job_id': child['id']}) or await self.store.one('security_reviews', {'job_id': child['id']})
        findings = (rv or {}).get('findings') or []
        return ('Your PREVIOUS attempt was rejected by review for: ' + '; '.join(str(f) for f in findings)[:1500] +
                '. Remove all out-of-scope changes; keep the change minimal, additive and strictly limited to the bounty.')

    async def _execute_loop(self, parent, conn, actor):
        pid = parent['id']
        policy = await execution_policy(self.store)
        # Escalate forward only, capped at (initial model + max_model_escalations). Never cycle back.
        ladder = (await self._coding_ladder())[:policy['max_model_escalations'] + 1]
        total_attempts = total_corrections = 0
        winner = None
        try:
            for model in ladder:
                if total_attempts >= policy['max_total_model_attempts']:
                    break
                timeout_retries = corrections_here = attempts_this_model = 0
                correction = None
                while total_attempts < policy['max_total_model_attempts'] and attempts_this_model < policy['max_attempts_per_model']:
                    if not await self._budget_fits():
                        await self._finalize_failure(pid, 'BUDGET: remaining cap cannot safely fit another bounded attempt')
                        return
                    total_attempts += 1
                    attempts_this_model += 1
                    child = await self._make_child(parent, model)
                    await self._mirror(pid, {**child, 'status': 'CODING'}, total_attempts, total_corrections, model)
                    original = await self._force_coding_model(model)
                    try:
                        await self.worker.run(child, conn, correction=correction)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    finally:
                        await self._restore_coding_model(original)
                    child = await self.store.one('jobs', {'id': child['id']})
                    await self._mirror(pid, child, total_attempts, total_corrections, model)
                    if child.get('status') == 'WAITING_APPROVAL':
                        winner = child
                        break
                    fc = classify_failure(child.get('error'))
                    await self.store.audit('master', 'Bounded attempt outcome',
                        f'model={model} attempt={total_attempts} class={fc}: {str(child.get("error"))[:160]}', 'BLOCKED', pid)
                    if fc in ('HARD_STOP', 'BUDGET'):
                        await self._finalize_failure(pid, child.get('error'))
                        return
                    if fc == 'TRANSIENT' and timeout_retries < policy['max_timeout_retries_per_model']:
                        timeout_retries += 1
                        correction = None
                        continue
                    if fc in ('TEST_FAILURE', 'REVIEW') and corrections_here < policy['max_corrections_per_model'] and total_corrections < policy['max_total_corrections']:
                        corrections_here += 1
                        total_corrections += 1
                        correction = await self._correction_text(child, fc)
                        continue
                    break  # exhausted this model -> escalate to next
                if winner:
                    break
            if not winner:
                last = await self.store.one('jobs', {'id': pid})
                await self._finalize_failure(pid, last.get('error') or 'All bounded attempts exhausted without a passing, review-approved patch')
                return
            # ---- publish the winning attempt (Draft PR only; reconcile if ambiguous) ----
            await self.store.db.jobs.update_one({'id': winner['id']}, {'$set': {'status': 'APPROVED', 'approval_status': 'APPROVED', 'approved_artifact_hash': winner['artifact_hash']}})
            await self.store.db.approval_requests.update_many({'job_id': winner['id']}, {'$set': {'status': 'APPROVED', 'actor': actor}})
            try:
                await self.publisher.publish(winner['id'], actor)
            except HTTPException:
                await self.publisher.reconcile(winner['id'], actor)
            wpr = await self.store.one('pull_requests', {'job_id': winner['id']})
            wjob = await self.store.one('jobs', {'id': winner['id']})
            await self.store.db.jobs.update_one({'id': pid}, {'$set': {
                'status': wjob.get('status'), 'winning_child': winner['id'], 'branch': winner['branch'],
                'commit': wjob.get('commit'), 'model': winner.get('coding_model'), 'next_action': None,
                'test_status': 'PASS', 'security_status': 'PASS', 'review_status': 'APPROVE'}})
        except asyncio.CancelledError:
            raise
        except HTTPException as e:
            await self._finalize_failure(pid, e.detail)
        except Exception as e:
            await self._finalize_failure(pid, repr(e))
        finally:
            try:
                rb = await RunBudget(self.store).state()
                await AutonomousBudget(self.store).reconcile(pid, rb.get('spent_nano_usd', 0))
            except Exception:
                pass
            try:
                await RunBudget(self.store).close()
            except Exception:
                pass
            self.orchestrator.tasks.pop(pid, None)

    async def _finalize_failure(self, job_id, reason):
        fc = classify_failure(reason)
        await self.store.db.jobs.update_one({'id': job_id}, {'$set': {
            'status': 'NEEDS_REVIEW', 'failure_class': fc, 'next_action': next_action(fc), 'error': redact(str(reason or ''))[:400]}})
        await self.store.audit('master', 'Manual run stopped', f'{fc}; next={next_action(fc)}', 'BLOCKED', job_id)

    async def job_state(self, job_id):
        job = await self.store.one('jobs', {'id': job_id})
        if not job:
            raise HTTPException(404, 'Job not found')
        pr_job = job.get('winning_child') or job.get('active_child') or job_id
        pr = await self.store.one('pull_requests', {'job_id': pr_job})
        budget = await RunBudget(self.store).state()
        return {'job_id': job_id, 'bounty_id': job.get('bounty_id'), 'reward': job.get('reward'), 'reward_currency': job.get('reward_currency'),
                'payout_mode': job.get('payout_mode'), 'payout_status': job.get('payout_status'), 'reward_verified': job.get('reward_verified'),
                'repository': job.get('repository'), 'issue_number': job.get('issue_number'), 'stage': job.get('status'),
                'run_mode': job.get('run_mode'), 'model': job.get('model'),
                'attempt': job.get('attempt'), 'max_attempts': (await execution_policy(self.store))['max_total_model_attempts'],
                'correction': job.get('correction'), 'active_child': job.get('active_child'), 'winning_child': job.get('winning_child'),
                'test_status': job.get('test_status'), 'security_status': job.get('security_status'), 'review_status': job.get('review_status'),
                'sandbox': job.get('sandbox_id'), 'branch': job.get('branch'), 'commit': job.get('commit'),
                'pr': {'url': (pr or {}).get('url'), 'number': (pr or {}).get('number'), 'status': (pr or {}).get('status')} if pr else None,
                'failure_class': job.get('failure_class'), 'next_action': job.get('next_action'), 'error': job.get('error'),
                'cost': {'spent_nano_usd': budget.get('spent_nano_usd'), 'cap_nano_usd': budget.get('cap_nano_usd'),
                         'remaining_nano_usd': (budget.get('cap_nano_usd', 0) - budget.get('spent_nano_usd', 0))},
                'active': job.get('status') in (LIVE_STATES + ['QUEUED', 'CODING', 'TESTING', 'REVIEW'])}

    async def stop(self, job_id, actor):
        job = await self.store.one('jobs', {'id': job_id})
        if not job:
            raise HTTPException(404, 'Job not found')
        if job.get('status') not in LIVE_STATES + ['QUEUED']:
            raise HTTPException(409, 'Job is not running')
        await self.orchestrator.stop(job_id)  # cancels task -> worker kills E2B, sets CANCELLED
        try:
            await RunBudget(self.store).close()
        except Exception:
            pass
        await self.store.db.jobs.update_one({'id': job_id}, {'$set': {'status': 'STOPPED', 'next_action': 'Needs Review'}})
        await self.store.audit('master', 'Job stopped by owner', 'No new model calls/retries/publication; E2B terminated; budget reconciled; audit preserved', 'BLOCKED', job_id, actor=actor, authorization='HUMAN_APPROVED')
        return {'job_id': job_id, 'status': 'STOPPED'}
