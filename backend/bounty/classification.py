"""Deterministic opportunity classification. NO LLM, NO network.

Separates PAYABILITY, EXECUTABILITY and PRIORITY (never collapsed into one boolean),
adds a payout-certainty model and a three-lane router. HARD GATES protect money/safety;
SOFT SIGNALS only order the queue. Age, freshness, solver count and competition are
PRIORITY signals ONLY — they never block eligibility.
"""

PAYOUT_CERTAINTY = ['UNKNOWN', 'MANUAL_CLAIM', 'MAINTAINER_APPROVAL_REQUIRED',
                    'SPONSOR_APPROVAL_REQUIRED', 'PLATFORM_CONTROLLED_RELEASE',
                    'FUNDED_ESCROW', 'PLATFORM_VERIFIED']
_RANK = {c: i for i, c in enumerate(PAYOUT_CERTAINTY)}

# Execution policy: minimum payout certainty a bounty needs to be eligible for the AUTONOMOUS lane.
# Escrow is a valid mechanism but NOT the only proof of a real payable bounty: platform-controlled release
# and platform-verified payout also qualify. The rank order below means this accepts exactly
# PLATFORM_CONTROLLED_RELEASE, FUNDED_ESCROW and PLATFORM_VERIFIED.
AUTONOMOUS_MIN_CERTAINTY = 'PLATFORM_CONTROLLED_RELEASE'
TERMINAL_STATUSES = {'REJECTED', 'EXPIRED', 'CANCELLED', 'DUPLICATE'}

# Whether Bounty Hunter can drive the fix workflow itself. SEPARATE from payout certainty:
# certainty answers "is there credible payment authority?", authority answers "can we execute it?".
EXECUTION_AUTHORITY = ['UNKNOWN', 'HUMAN_CLAIM_REQUIRED', 'GITHUB_AUTHORIZED', 'DIRECT_PLATFORM_API']
AUTONOMOUS_EXEC_AUTHORITIES = {'DIRECT_PLATFORM_API', 'GITHUB_AUTHORIZED'}


def execution_authority(opp):
    ea = opp.get('execution_authority')
    return ea if ea in EXECUTION_AUTHORITY else 'UNKNOWN'


def rank(certainty):
    return _RANK.get(certainty or 'UNKNOWN', 0)


def payability(opp):
    rs = opp.get('reward_status')
    if rs == 'EXPIRED':
        return 'NO'
    if rs == 'VERIFIED':
        return 'YES'
    return 'UNKNOWN'  # PARSED_UNVERIFIED / CONFLICTING / UNKNOWN / None


def executability(opp):
    """HARD GATES ONLY. Never gated on age, freshness, solver count or competition."""
    if opp.get('status') in TERMINAL_STATUSES or opp.get('reward_status') == 'EXPIRED':
        return 'NO'
    if opp.get('attempted') or opp.get('claimed'):
        return 'NO'
    if opp.get('qualification_status') == 'REJECTED' or opp.get('risk_status') == 'FLAGGED':
        return 'NO'
    if not opp.get('repository') or opp.get('issue_number') in (None, ''):
        return 'UNKNOWN'
    if opp.get('qualification_status') != 'QUALIFIED' or opp.get('risk_status') != 'CLEARED':
        return 'UNKNOWN'
    return 'YES'


def priority(opp):
    """SOFT ordering only. Returns (category, score, signals). Missing data stays visible (LOW)."""
    score, signals = 0, []
    amt = opp.get('reward_amount') or 0
    try:
        amt = float(amt)
    except (TypeError, ValueError):
        amt = 0
    if amt >= 500:
        score += 3; signals.append('HIGH_VALUE')
    elif amt >= 100:
        score += 2
    elif amt > 0:
        score += 1
    cert = rank(opp.get('payout_certainty'))
    if cert >= rank('FUNDED_ESCROW'):
        score += 3; signals.append('HIGH_PAYOUT_CERTAINTY')
    elif cert >= rank('SPONSOR_APPROVAL_REQUIRED'):
        score += 1
    sc = opp.get('solver_count')
    if isinstance(sc, int):
        if sc == 0:
            score += 2; signals.append('LOW_COMPETITION')
        elif sc <= 2:
            score += 1
    cpr = opp.get('competing_pr_count')
    if isinstance(cpr, int) and cpr == 0:
        score += 1
    if opp.get('reward_created_at') and _is_recent(opp.get('reward_created_at')):
        score += 2; signals.append('NEWLY_FUNDED')
    cat = 'HOT' if score >= 7 else 'HIGH' if score >= 5 else 'MEDIUM' if score >= 2 else 'LOW'
    return cat, score, signals


def _is_recent(iso, days=3):
    try:
        from datetime import datetime, timezone, timedelta
        s = str(iso)
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) <= timedelta(days=days)
    except Exception:
        return False


def lane(opp, pay, exe, certainty, exec_authority):
    if opp.get('status') in TERMINAL_STATUSES or opp.get('reward_status') == 'EXPIRED':
        return 'WATCHLIST'
    # AUTONOMOUS: credible payment authority + executable + Bounty Hunter can drive the workflow itself.
    if (pay == 'YES' and exe == 'YES' and rank(certainty) >= rank(AUTONOMOUS_MIN_CERTAINTY)
            and exec_authority in AUTONOMOUS_EXEC_AUTHORITIES):
        return 'AUTONOMOUS'
    # A human claim / approval / payout action is required → the owner decides.
    if exec_authority == 'HUMAN_CLAIM_REQUIRED':
        return 'OWNER_APPROVAL'
    # No credible execution-authority evidence → keep visible but on the watchlist.
    if exec_authority == 'UNKNOWN':
        return 'WATCHLIST'
    has_signal = bool(opp.get('reward_amount')) or (certainty and certainty != 'UNKNOWN')
    if pay in ('YES', 'UNKNOWN') and exe in ('YES', 'UNKNOWN') and has_signal:
        return 'OWNER_APPROVAL'
    return 'WATCHLIST'


def blocking_reasons(opp, pay, exe, certainty, exec_authority):
    """WHY an opportunity is not autonomous-ready. NEVER includes age/freshness/solver/competition."""
    reasons = []
    if opp.get('status') in TERMINAL_STATUSES:
        reasons.append(f"STATUS_{opp.get('status')}")
    if opp.get('qualification_status') == 'REJECTED':
        reasons.append('REJECTED_QUALIFICATION')
    if opp.get('risk_status') == 'FLAGGED':
        reasons.append('SAFETY_FLAGGED')
    if opp.get('reward_status') == 'EXPIRED':
        reasons.append('REWARD_EXPIRED')
    elif opp.get('reward_status') != 'VERIFIED':
        reasons.append('REWARD_NOT_VERIFIED')
    if rank(certainty) < rank(AUTONOMOUS_MIN_CERTAINTY):
        reasons.append('PAYOUT_CERTAINTY_INSUFFICIENT_FOR_AUTONOMOUS')
    if exec_authority not in AUTONOMOUS_EXEC_AUTHORITIES:
        reasons.append(f'EXECUTION_AUTHORITY_{exec_authority}')
    if not opp.get('repository') or opp.get('issue_number') in (None, ''):
        reasons.append('NO_REPO_OR_ISSUE')
    if opp.get('attempted') or opp.get('claimed'):
        reasons.append('ALREADY_ATTEMPTED')
    return reasons


def canonical_identity(repository, issue_number):
    """Source-agnostic identity for cross-source dedup: the same repo#issue is ONE opportunity."""
    if repository and issue_number not in (None, ''):
        return f"{str(repository).lower()}#{issue_number}"
    return None


def classify(opp):
    pay = payability(opp)
    exe = executability(opp)
    certainty = opp.get('payout_certainty') or 'UNKNOWN'
    exec_auth = execution_authority(opp)
    cat, score, signals = priority(opp)
    return {
        'payability': pay,
        'executability': exe,
        'payout_certainty': certainty,
        'execution_authority': exec_auth,
        'priority': cat,
        'priority_score': score,
        'priority_signals': signals,
        'lane': lane(opp, pay, exe, certainty, exec_auth),
        'blocking_reasons': blocking_reasons(opp, pay, exe, certainty, exec_auth),
    }
