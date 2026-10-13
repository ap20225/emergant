"""Deterministic bounded-bounty candidate selection (READ-ONLY).

Ranks live discovery opportunities on PAYABLE + OPEN + AUTHORIZED + BOUNDED + TESTABLE using authoritative
structured reward data (never a reward parsed only from an issue title/body) and live GitHub issue state.
Selects ONE recommended candidate. Performs NO execution and NO spend — GitHub reads only.
"""
import asyncio
import base64
import re
from datetime import datetime, timezone

from fastapi import HTTPException

from .repos import RepoRegistry, resolve_targets
from .store import redact
from .patches import safe_path

CACHE_ID = 'recommended-candidate-cache'
CACHE_TTL_SECONDS = 600

# Bounded = targeted bug/regression/fix. Unbounded = feature/redesign/migration/architecture.
BOUNDED_POS = re.compile(
    r'\b(bug|bugs|fix|fixes|fixed|crash|crashes|error|errors|incorrect|wrong|broken|regression|'
    r'fail|fails|failing|failure|typo|null|npe|exception|leak|freeze|hang|hangs|deadlock|race|'
    r'off.?by.?one|not ?working|does.?n.?t ?work|missing|throws?|500|404|nullpointer|stacktrace)\b', re.I)
BOUNDED_NEG = re.compile(
    r'\b(support|feature|implement|implementation|redesign|migrat\w*|rewrite|refactor|epic|'
    r'architecture|architectural|integrat\w*|overhaul|revamp|proposal|rfc|roadmap|spec|'
    r'add .* support|multi-?subsystem|new module|framework)\b', re.I)
TESTABLE = re.compile(
    r'\b(test|tests|reproduc\w+|steps to|expected|actual|when i|stack ?trace|traceback|'
    r'given|assert|unit ?test|regression test)\b', re.I)

# Mirror the engine's bounded-inspection window (bounty/live_worker.py context()): it only reads files with
# these extensions (each <=20 KB), caps at 30 files / 100 KB total, and rejects a truncated tree. A repo whose
# real source is in OTHER extensions (e.g. Java/Kotlin/C++) is "inspectable" only via docs -> the agent can't
# fix real code (produces doc-only patches), so we also require at least one true CODE file to be inspectable.
INSPECT_EXT = re.compile(r'\.(py|js|ts|tsx|jsx|json|md|txt|toml|yaml|yml|go)$', re.I)
CODE_EXT = re.compile(r'\.(py|js|ts|tsx|jsx|go)$', re.I)
MAX_INSPECT_FILES = 30
MAX_INSPECT_BYTES = 100000


def _boundedness(text):
    pos = len(BOUNDED_POS.findall(text or ''))
    neg = len(BOUNDED_NEG.findall(text or ''))
    return pos, neg, (pos > 0 and neg == 0)


async def inspection_scope(github, repo, default_branch, signals):
    """Deterministic dry-run of the engine's NEW issue-relevant bounded context (bounty/relevance.py):
    ranks the tree by relevance to the issue, selects the bounded relevant subset (<=30 files/<=100KB),
    requires >=1 inspectable CODE file and that no SELECTED file trips the secret redactor. Large repos
    now pass when the requested change is localized. GitHub reads only — no model, no spend."""
    out = {'fits': False, 'reason': '', 'files': 0, 'bytes': 0, 'code_files': 0}
    try:
        ref = await github.request('GET', f'/repos/{repo}/git/ref/heads/{default_branch}')
        sha = (ref.get('object') or {}).get('sha')
    except HTTPException as e:
        out['reason'] = f'REF_{e.status_code}'
        return out
    if not sha:
        out['reason'] = 'NO_DEFAULT_REF'
        return out
    from .relevance import gather_context
    try:
        res = await gather_context(github, repo, sha, signals)
    except HTTPException as e:
        out['reason'] = str(e.detail)[:100]
        return out
    except Exception:
        out['reason'] = 'SCOPE_PROBE_ERROR'
        return out
    out['files'] = len(res['files'])
    out['code_files'] = res['code_files']
    out['bytes'] = sum(len(v.encode()) for v in res['files'].values())
    out['fits'] = bool(res['files']) and res['code_files'] >= 1
    out['reason'] = res['detail']
    return out


async def evaluate_candidate(github, registry, opp):
    """Deterministic per-candidate verification. Returns an evidence dict (no side effects)."""
    repo, issue = opp.get('repository'), opp.get('issue_number')
    amount = opp.get('reward_amount')
    if amount is None:
        amount = opp.get('authoritative_reward_amount')
    currency = opp.get('reward_currency') or opp.get('authoritative_reward_currency')
    ev = {
        'opportunity_id': opp.get('id'), 'source': opp.get('source'), 'repository': repo, 'issue_number': issue,
        'source_url': opp.get('source_url'),
        'reward_amount': amount, 'reward_currency': currency,
        'payout_authority': opp.get('payout_authority'), 'payout_certainty': opp.get('payout_certainty'),
        'reward_status': opp.get('reward_status'), 'execution_authority': opp.get('execution_authority'),
        'qualification_status': opp.get('qualification_status'), 'risk_status': opp.get('risk_status'),
        'solver_count': opp.get('solver_count'), 'attempted': bool(opp.get('attempted') or opp.get('claimed')),
        'checks': {}, 'blockers': [],
    }
    c = ev['checks']

    # 1. Authoritative reward source (structured platform data, NOT a title/body number).
    authoritative = bool(amount is not None and opp.get('payout_authority')
                         and opp.get('payout_source_type') == 'platform_api')
    c['authoritative_reward'] = authoritative
    ev['verification_source'] = f"{opp.get('payout_authority')} (structured platform API)" if authoritative else 'issue text only (untrusted)'
    if not authoritative:
        ev['blockers'].append('REWARD_NOT_AUTHORITATIVE')

    # 2. Live GitHub issue state (live GitHub wins for open/closed).
    if not repo or not issue:
        ev['blockers'].append('NO_REPO_OR_ISSUE')
        c['github_state'] = 'unknown'
        return _finalize(ev)
    try:
        gi = await github.request('GET', f'/repos/{repo}/issues/{issue}')
    except HTTPException as e:
        c['github_state'] = f'HTTP_{e.status_code}'
        ev['blockers'].append(f'GITHUB_{e.status_code}')
        return _finalize(ev)
    is_pr = 'pull_request' in gi
    state = gi.get('state')
    c['github_state'] = state
    c['is_pull_request'] = is_pr
    c['assigned'] = bool(gi.get('assignee'))
    c['comments'] = gi.get('comments')
    if state != 'open':
        ev['blockers'].append('ISSUE_NOT_OPEN')
    if is_pr:
        ev['blockers'].append('IS_PULL_REQUEST')

    # 3. Repository accessibility.
    rm = None
    try:
        rm = await github.request('GET', f'/repos/{repo}')
        c['repo_accessible'] = True
        c['default_branch'] = rm.get('default_branch')
        c['archived'] = bool(rm.get('archived'))
        c['language'] = rm.get('language')
        c['size_kb'] = rm.get('size')
        if rm.get('archived'):
            ev['blockers'].append('REPO_ARCHIVED')
    except HTTPException as e:
        c['repo_accessible'] = False
        ev['blockers'].append(f'REPO_{e.status_code}')

    # 3b. Stub/synthetic/farmed repository guard (multi-signal; small size alone is NOT a signal).
    try:
        from .repo_quality import assess_repo_quality
        rq = await assess_repo_quality(github, repo, rm, gi)
    except Exception:
        rq = {'confidence': 'NONE', 'synthetic': False, 'score': 0, 'signals': []}
    ev['repo_quality'] = rq
    if rq['confidence'] == 'HIGH':
        ev['blockers'].append('SYNTHETIC_REPO_HIGH_CONFIDENCE')

    # 4. Boundedness + testability from the LIVE issue (title + body + labels).
    title = gi.get('title') or opp.get('title') or ''
    body = (gi.get('body') or '')[:6000]
    labels = [l.get('name') for l in (gi.get('labels') or []) if isinstance(l, dict)]
    text = f'{title}\n{body}\n{" ".join(labels)}'
    ev['_issue_text'] = text[:6000]
    pos, neg, bounded = _boundedness(text)
    c['bounded_positive_hits'] = pos
    c['bounded_negative_hits'] = neg
    c['bounded'] = bounded
    c['labels'] = labels[:12]
    testable = bool(TESTABLE.search(text))
    c['testable'] = testable
    ev['title'] = title[:200]
    if neg > 0:
        ev['blockers'].append('SCOPE_LOOKS_UNBOUNDED')
    if not bounded:
        ev['blockers'].append('NO_BOUNDED_SIGNAL')

    # 5. Authorization / write target (fork-first).
    targets = await resolve_targets(registry, {'repository': repo})
    c['write_ready'] = targets.get('write_ready')
    c['write_strategy'] = targets.get('strategy')
    # A fork can be auto-created on RUN if the account has push scope, so unauthorized is not a hard blocker here.
    ev['authorization_status'] = ('AUTHORIZED' if targets.get('write_ready')
                                  else 'FORK_ON_RUN (auto fork+authorize when you click RUN BOUNTY)')

    # 6. Previous-attempt status.
    if ev['attempted']:
        ev['blockers'].append('ALREADY_ATTEMPTED')

    return _finalize(ev)


def _finalize(ev):
    c = ev['checks']
    open_ok = c.get('github_state') == 'open' and not c.get('is_pull_request')
    payable = c.get('authoritative_reward') and ev.get('reward_amount') is not None
    bounded = c.get('bounded')
    testable = c.get('testable')
    accessible = c.get('repo_accessible') and not c.get('archived')
    qualified = ev.get('qualification_status') == 'QUALIFIED' and ev.get('risk_status') == 'CLEARED'
    rq = ev.get('repo_quality') or {}
    synthetic_high = rq.get('confidence') == 'HIGH'
    # Preliminary (cheap) executability — the engine's inspection-scope gate is verified in phase 2.
    # A HIGH-confidence synthetic/farmed repo is excluded; MEDIUM/LOW are only ranked lower (below).
    ev['prelim_executable'] = bool(open_ok and payable and bounded and accessible and qualified
                                   and not ev['attempted'] and not synthetic_high)
    ev['executable'] = ev['prelim_executable']
    # Score favours strongest combined evidence — NOT the largest reward.
    ev['score'] = (
        (3 if bounded else 0) + (2 if testable else 0) + (2 if open_ok else 0)
        + (2 if payable else 0) + (1 if accessible else 0) + (1 if qualified else 0)
        + (1 if c.get('write_ready') else 0) - 2 * c.get('bounded_negative_hits', 0)
        - (2 if ev['attempted'] else 0) - (1 if c.get('assigned') else 0)
        - (3 if rq.get('confidence') == 'MEDIUM' else 0) - (1 if rq.get('confidence') == 'LOW' else 0)
    )
    ev['boundedness_evidence'] = (
        f"bounded-signal hits={c.get('bounded_positive_hits', 0)}, unbounded hits={c.get('bounded_negative_hits', 0)}, "
        f"testable={testable}, labels={c.get('labels')}"
    )
    return ev


async def select_recommended(store, github, max_live_checks=18):
    """Pick ONE recommended bounded candidate. Pre-filters on structured reward + qualification, pre-ranks by
    stored-title boundedness to bound the number of live GitHub calls, then verifies live and returns the best."""
    registry = RepoRegistry(store)
    opps = await store.rows('bounty_opportunities', {
        'qualification_status': 'QUALIFIED', 'risk_status': 'CLEARED',
        'repository': {'$ne': None}, 'issue_number': {'$ne': None},
        'payout_source_type': 'platform_api',
    }, limit=2000)
    pool = [o for o in opps if (o.get('reward_amount') is not None or o.get('authoritative_reward_amount') is not None)
            and not (o.get('attempted') or o.get('claimed'))]

    def pre_rank(o):
        pos, neg, _ = _boundedness(f"{o.get('title', '')} {' '.join(o.get('labels') or [])}")
        return (pos - 2 * neg, -(o.get('solver_count') or 0))

    pool.sort(key=pre_rank, reverse=True)
    subset = pool[:max_live_checks]
    sem = asyncio.Semaphore(10)

    async def _eval(o):
        async with sem:
            try:
                return await evaluate_candidate(github, registry, o)
            except Exception:
                return None

    evaluated = [e for e in await asyncio.gather(*[_eval(o) for o in subset]) if e]

    # Phase 2: dry-run the engine's bounded-inspection + secret gate on the strongest preliminary candidates
    # in score order, stopping at the first genuinely runnable one. executable REQUIRES this to pass.
    prelim = sorted([e for e in evaluated if e.get('prelim_executable')], key=lambda e: e['score'], reverse=True)
    checked = 0
    found = False
    for e in prelim:
        if checked >= 6 or found:
            e['executable'] = False
            e['blockers'].append('INSPECTION_NOT_VERIFIED')
            continue
        checked += 1
        from .relevance import extract_signals
        signals = extract_signals(e.get('title') or '', e.get('_issue_text') or '', e['checks'].get('labels') or [])
        scope = await inspection_scope(github, e['repository'], e['checks'].get('default_branch') or 'main', signals)
        e['checks']['inspection_fits'] = scope['fits']
        e['checks']['inspection_detail'] = scope['reason']
        e['checks']['inspectable_files'] = scope['files']
        e['checks']['inspectable_code_files'] = scope['code_files']
        e['executable'] = bool(e['prelim_executable'] and scope['fits'])
        if scope['fits']:
            e['score'] += 3  # a repo the engine can truly inspect+fix is the strongest signal
            found = True
        else:
            e['blockers'].append(f"REPO_{scope['reason']}")

    executable = [e for e in evaluated if e['executable']]
    executable.sort(key=lambda e: e['score'], reverse=True)
    ranked = sorted(evaluated, key=lambda e: e['score'], reverse=True)
    rec = executable[0] if executable else None
    conn = await store.one('integration_connections', {'id': 'github'})
    login = (conn or {}).get('login')
    if rec and login and rec.get('repository'):
        rec['fork_owner'] = login
        rec['suggested_fork_repo'] = f"{login}/{rec['repository'].split('/')[1]}"
    for e in evaluated:
        e.pop('_issue_text', None)  # keep the cached payload compact + JSON-clean
    result = {
        'recommended': rec,
        'considered': len(pool),
        'live_checked': len(evaluated),
        'executable_count': len(executable),
        'computed_at': datetime.now(timezone.utc).isoformat(),
        'shortlist': [{'repository': e['repository'], 'issue_number': e['issue_number'],
                       'reward_amount': e['reward_amount'], 'score': e['score'],
                       'executable': e['executable'],
                       'repo_quality': (e.get('repo_quality') or {}).get('confidence', 'NONE'),
                       'blockers': e['blockers']} for e in ranked[:8]],
    }
    await store.db.system_settings.update_one({'id': CACHE_ID}, {'$set': {'id': CACHE_ID, 'result': result, 'computed_at': result['computed_at']}}, upsert=True)
    return result


async def get_cached(store):
    row = await store.one('system_settings', {'id': CACHE_ID})
    if not row or not row.get('result'):
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['computed_at'])).total_seconds()
    except Exception:
        return None
    result = row['result']
    result['cached'] = True
    result['cache_age_seconds'] = int(age)
    return result if age < CACHE_TTL_SECONDS else None
