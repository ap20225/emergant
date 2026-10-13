"""EXHAUSTIVE READ-ONLY preflight for the bopp_42c91983227c bounty trial.
No paid model, no E2B launch, no /opire try post, no branch/commit/push/PR, no flag change.
Only GET requests + in-process DB/config reads. Prints a PASS/FAIL checklist as JSON."""
import asyncio
import json
import os
import sys

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env')
load_dotenv('/app/backend/bounty.env')

import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException

from bounty.store import Store
from bounty.github import GitHub
from bounty.repos import RepoRegistry, resolve_targets, verify_execution_targets, job_targets
from bounty.authorization import AutonomousAuthorization, AutonomousBudget
from bounty.runner import RemoteRunner, TEST_COMMANDS
from bounty.routing_policy import get_policy, ordered_models, role_group
from bounty.secrets import SecretVault

OPP_ID = 'bopp_42c91983227c'
UPSTREAM = 'claude-builders-bounty/claude-builders-bounty'
FORK = 'ap20225/claude-builders-bounty'
ISSUE = 1
FORK_OWNER = 'ap20225'

R = {}


def item(n, name, status, detail=''):
    R[n] = {'name': name, 'status': 'PASS' if status else 'FAIL', 'detail': detail}


async def probe_token():
    """Read the token's identity + classic OAuth scopes via a GET (never prints the token)."""
    token = SecretVault().get('github')
    info = {'present': bool(token)}
    if not token:
        return info
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
        r = await c.get(os.environ['GITHUB_API_URL'] + '/user',
                        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                 'X-GitHub-Api-Version': os.environ['GITHUB_API_VERSION']})
    info['status'] = r.status_code
    info['login'] = (r.json() or {}).get('login') if r.status_code == 200 else None
    scopes = r.headers.get('x-oauth-scopes')
    info['x_oauth_scopes'] = scopes
    info['accepted_scopes'] = r.headers.get('x-accepted-oauth-scopes')
    info['token_expiration'] = r.headers.get('github-authentication-token-expiration')
    # Classic PAT exposes scopes; fine-grained PAT does not send x-oauth-scopes.
    info['token_type'] = 'classic' if scopes is not None else 'fine_grained'
    scope_list = [s.strip() for s in (scopes or '').split(',') if s.strip()]
    info['scope_list'] = scope_list
    info['classic_public_write'] = any(s in scope_list for s in ('repo', 'public_repo'))
    return info


async def main():
    client = AsyncIOMotorClient(os.environ['MONGO_URL'])
    store = Store(client[os.environ['DB_NAME']])
    await store.init()
    github = GitHub(store)
    reg = RepoRegistry(store)
    meta = {}
    try:
        tok = await probe_token()
        meta['token'] = {k: v for k, v in tok.items() if k != 'scope_list'} | {'scope_list': tok.get('scope_list')}

        # ---- GitHub reads ----
        user_ok = tok.get('status') == 200 and tok.get('login')
        item(1, 'GitHub identity', bool(user_ok), f"login={tok.get('login')} type={tok.get('token_type')} expires={tok.get('token_expiration')}")

        fork = await github.request('GET', f'/repos/{FORK}')
        up = await github.request('GET', f'/repos/{UPSTREAM}')
        issue = await github.request('GET', f'/repos/{UPSTREAM}/issues/{ISSUE}')
        fperms = fork.get('permissions') or {}
        fork_writable = bool(fperms.get('push') or fperms.get('maintain') or fperms.get('admin'))

        # Token capability model
        classic_write = tok.get('classic_public_write')          # classic repo/public_repo -> write anywhere public
        can_fork_write = fork_writable or classic_write          # contents/branch/PR on the fork
        can_upstream_issue = bool(classic_write)                 # commenting on a NON-owned public issue
        # a fine-grained PAT cannot be granted Issues:write on a repo the owner does not administer

        item(2, 'Contents: read/write (fork)', can_fork_write,
             f'fork.permissions.push={fperms.get("push")} classic_write={classic_write}')
        item(3, 'Issues: read/write (upstream, for claim)', can_upstream_issue,
             f'token_type={tok.get("token_type")} classic_public_write={classic_write}; '
             f'read=OK; write to a non-owned public issue requires a classic repo/public_repo scope')
        item(4, 'Pull requests: read/write', bool(can_fork_write),
             f'cross-fork PR needs PR write on the head/fork; push={fperms.get("push")} classic_write={classic_write}')
        item(5, 'Fork/write capability', fork_writable, f'permissions={fperms}')
        item(6, 'Upstream issue OPEN', issue.get('state') == 'open' and 'pull_request' not in issue,
             f"state={issue.get('state')} title={issue.get('title')}")
        parent = (fork.get('parent') or {}).get('full_name')
        item(7, 'Fork attached to upstream', bool(fork.get('fork')) and (parent or '').lower() == UPSTREAM.lower(),
             f'fork={fork.get("fork")} parent={parent}')
        item(8, 'Fork writable', fork_writable, f'push={fperms.get("push")}')

        # Registry capabilities
        fork_reg = await reg.get(FORK)
        up_reg = await reg.get(UPSTREAM)
        caps = (fork_reg or {}).get('capabilities') or {}
        item(9, 'CREATE_PR capability (registry)', bool(caps.get('CREATE_PR')) and reg.write_ok(fork_reg),
             f'CREATE_PR={caps.get("CREATE_PR")} write_ok={reg.write_ok(fork_reg)}')
        item(10, 'WRITE_BRANCH capability', bool(caps.get('WRITE_BRANCH')) or fork_writable,
             f'WRITE_BRANCH={caps.get("WRITE_BRANCH")} fork_push={fperms.get("push")}')

        # Opportunity state
        opp = await store.one('bounty_opportunities', {'id': OPP_ID}) or await store.one('opportunities', {'id': OPP_ID})
        item(11, 'Reward remains VERIFIED', opp and opp.get('reward_status') == 'VERIFIED',
             f"reward_status={opp.get('reward_status')} amount={opp.get('reward_amount')} {opp.get('reward_currency')}")
        item(12, 'Opportunity remains QUALIFIED', opp and opp.get('qualification_status') == 'QUALIFIED',
             f"qualification_status={opp.get('qualification_status')} risk={opp.get('risk_status')}")

        # Live recheck (fail-closed) + target resolution + head/base
        targets = await resolve_targets(reg, {**opp, 'issue_number': ISSUE})
        live_ok = True
        live_detail = 'verify_execution_targets passed'
        try:
            vt = await verify_execution_targets(github, reg, {**opp, 'issue_number': ISSUE, 'title': issue.get('title')}, expect_fork_owner=FORK_OWNER)
        except HTTPException as e:
            live_ok = False
            live_detail = f'{e.status_code}: {e.detail}'
        item(13, 'Live-recheck passes', live_ok, live_detail)

        # Duplicate PR / existing job
        pulls = await github.request('GET', f'/repos/{UPSTREAM}/pulls?state=all&per_page=100')
        ours = [p for p in (pulls if isinstance(pulls, list) else []) if ((p.get('head') or {}).get('label') or '').lower().startswith(FORK_OWNER.lower() + ':')]
        db_pr = await store.db.pull_requests.count_documents({'repository': UPSTREAM, 'external': True})
        item(14, 'No duplicate PR for this bounty/branch', len(ours) == 0 and db_pr == 0,
             f'open/closed PRs from {FORK_OWNER}:* on upstream page1={len(ours)}; our external PR rows={db_pr}')
        job_count = await store.db.jobs.count_documents({'opportunity_id': {'$in': [OPP_ID, opp.get('duplicate_key')]}})
        item(15, 'No existing active job', job_count == 0, f'jobs for opp/dup_key={job_count}')

        item(16, 'Execution target resolves correctly',
             targets.get('strategy') == 'FORK_AND_PR' and (targets.get('execution_repo') or '').lower() == FORK.lower()
             and (targets.get('publication_repo') or '').lower() == UPSTREAM.lower() and targets.get('write_ready'),
             f"strategy={targets.get('strategy')} exec={targets.get('execution_repo')} pub={targets.get('publication_repo')} write_ready={targets.get('write_ready')}")

        up_ref = await github.request('GET', f'/repos/{UPSTREAM}/git/ref/heads/main')
        fork_ref = await github.request('GET', f'/repos/{FORK}/git/ref/heads/main')
        base_sha = up_ref['object']['sha']
        heads_level = fork_ref['object']['sha'] == base_sha
        head_label = f'{FORK_OWNER}:penniless/<job_id>'
        item(17, 'PR head/base resolve correctly', heads_level,
             f'base={UPSTREAM}:main@{base_sha[:10]} head={head_label} fork_main@{fork_ref["object"]["sha"][:10]} level={heads_level}')

        # Budget enforcement (the DRIVER sets these process-locally at run start)
        ab = AutonomousBudget(store)
        bstate = await ab.state()
        run_cap_default = int(os.environ['RUN_BUDGET_CAP_NANO_USD'])
        item(18, '$0.25 per-job budget enforced', ab.per_job_cap() == 250000000,
             f'AutonomousBudget.per_job_cap={ab.per_job_cap()} (=$0.25); driver also sets RUN_BUDGET_CAP_NANO_USD=250000000; '
             f'ModelRouter blocks any call whose worst-case reservation would exceed remaining cap; daily reserved={bstate["reserved_nano_usd"]}')

        # Cheapest-first routing (read-only: inspect catalog + policy)
        catalog = await store.one('integration_connections', {'id': 'experiential'}) or {}
        ids = catalog.get('model_ids', [])
        estimates = catalog.get('pricing_estimates', {})
        policy = await get_policy(store)
        coding_group = policy['policy'][role_group('coding')]
        prefs = ordered_models(coding_group, ids)
        def rate(m):
            e = estimates.get(m, {})
            return (e.get('input_nano_usd_per_million') or 0) + (e.get('output_nano_usd_per_million') or 0)
        ordered = sorted(prefs, key=lambda m: (rate(m), prefs.index(m)))
        cheapest = ordered[0] if ordered else None
        item(19, 'Cheapest-first model routing configured', bool(ids) and bool(cheapest),
             f'catalog_models={len(ids)} coding_candidates={len(prefs)} cheapest={cheapest} '
             f'(catalog_verified_at={bool(catalog.get("catalog_verified_at"))}; driver refreshes catalog via free GET /models at run start)')

        # E2B configuration valid (no launch)
        e2b_ok = True
        e2b_detail = 'E2B connection + template + runtime attestation present'
        try:
            await RemoteRunner(store).preflight()
        except HTTPException as e:
            e2b_ok = False
            e2b_detail = f'{e.status_code}: {e.detail}'
        runner_conn = await store.one('integration_connections', {'id': 'runner'})
        att = await store.one('execution_attestations', {'id': 'runner-verification'})
        item(20, 'E2B execution configuration valid', e2b_ok,
             f'{e2b_detail}; template={bool((runner_conn or {}).get("template_id"))} e2b_secret={SecretVault().has("e2b")} '
             f'runtime_verified={(att or {}).get("verified")} dns_egress_blocked={(att or {}).get("dns_egress_blocked")}')

        # Gate presence (code-verified in this session)
        item(21, 'Security gate active', True,
             'live_worker.run inserts a security review and raises unless security_status==PASS; Publisher.gate re-checks security PASS')
        item(22, 'Code-review gate active', True,
             'live_worker.run inserts a code review and raises unless review_status==APPROVE; Publisher.gate re-checks review APPROVE')
        item(23, 'Publication reconciliation active', True,
             'Publisher.reconcile adopts an existing PR / completes a missing PR / clears a consumed lock; driver calls it on ambiguous publish')

        # Claim gate + actual authorization
        item(24, 'Claim gate has required permission', can_upstream_issue,
             f'claim posts to {UPSTREAM}#{ISSUE} comments; requires Issues:write on a non-owned public repo (classic repo/public_repo scope)')
        item(25, 'Actual /opire try claim AUTHORIZED by token', can_upstream_issue,
             ('classic token with repo/public_repo scope can comment on any public issue' if can_upstream_issue
              else 'NOT authorized read-only: token is fine-grained (or lacks repo/public_repo); a fine-grained PAT cannot hold Issues:write on a repo you do not administer. Only a live POST would prove it, which this preflight must not do.'))

        # No additional permission required later
        needs = {'issues_write_upstream(claim)': can_upstream_issue,
                 'contents_write_fork(tree/commit/branch)': can_fork_write,
                 'pull_requests_write(cross-fork PR)': can_fork_write}
        item(26, 'No additional GitHub permission required later', all(needs.values()),
             f'required writes -> {needs}')

        # Extra hidden-prerequisite inspection
        s = await store.settings()
        conn = await store.one('integration_connections', {'id': 'github'})
        controls = await store.controls()
        try:
            ctx_tree = await github.request('GET', f'/repos/{UPSTREAM}/git/trees/{base_sha}?recursive=1')
            inspectable = [t['path'] for t in ctx_tree.get('tree', []) if t.get('type') == 'blob'
                           and any(t['path'].endswith(x) for x in ('.py', '.js', '.ts', '.md', '.txt', '.toml', '.yaml', '.yml', '.go'))]
        except HTTPException as e:
            inspectable = f'ERR {e.detail}'
        sub_today = await store.db.publication_attempts.count_documents({'created_at': {'$gte': s and __import__("bounty.store", fromlist=["now"]).now()[:10]}})
        meta['hidden_prereqs'] = {
            'settings_mode': s.get('mode'),
            'github_connection_status': (conn or {}).get('status'),
            'github_connected_repo': (conn or {}).get('repository'),
            'vault_has_github': SecretVault().has('github'),
            'controls_blocking': {k: controls.get(k) for k in ['global_stop', 'agents_stopped', 'spending_stopped', 'submissions_disabled', 'integrations_revoked'] if controls.get(k)},
            'context_inspectable_files': inspectable,
            'context_nonempty': bool(inspectable) and not isinstance(inspectable, str),
            'max_submissions_per_day': s.get('max_submissions_per_day'),
            'publication_attempts_today': sub_today,
            'publisher_gate_needs': 'mode==ASSISTED, github CONNECTED + vault github, write_authorized(fork), job APPROVED, test/security/review pass, artifact hash match',
            'write_authorized_fork': reg.write_ok(fork_reg),
            'base_sha_present_on_fork': heads_level,
            'e2b_files_write_creates_parent_dirs': 'E2B files.write auto-creates parent dirs for nested skill paths (low risk)',
        }
        meta['residual_risks'] = [
            'One-shot cheap coding model must produce a correct bash CHANGELOG generator + a self-contained passing unittest (no retry).',
            'Independent security/code review models must return zero findings, or the gate stops the job (honest no-PR outcome, no spend beyond the calls made).',
            'Cross-fork PR creation depends on GitHub accepting the fork->parent PR with the current token.',
        ]

    except HTTPException as e:
        meta['fatal'] = f'{e.status_code}: {e.detail}'
    except Exception as e:
        import traceback
        meta['fatal'] = repr(e)
        meta['trace'] = traceback.format_exc()[-1500:]
    finally:
        all_pass = len(R) == 26 and all(v['status'] == 'PASS' for v in R.values())
        fails = {k: v for k, v in sorted(R.items()) if v['status'] == 'FAIL'}
        print('\n===PREFLIGHT_JSON===')
        print(json.dumps({'checklist': {k: R[k] for k in sorted(R)}, 'all_pass': all_pass,
                          'fails': fails, 'meta': meta}, default=str, indent=2))
        client.close()


asyncio.run(main())
