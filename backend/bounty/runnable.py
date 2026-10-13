"""Product-visibility helpers (NO gate is defined or weakened here — this only explains, in human
language, what the EXISTING gates already decided).

- `runnable_state(opp)`  : from STORED discovery/qualification data (fast, no live GitHub), tell the
  operator whether a bounty can be run now, and if not, why + the one-tap remedy. Deeper live checks
  (issue still open, upstream accepts PRs, scope fit) happen when the owner actually runs it.
- `review_bucket(job)`   : sort a finished job into an HONEST bucket so the review queue means
  "needs YOU", not "everything that didn't publish".
"""
from .classification import classify, TERMINAL_STATUSES

RUNNABLE = 'RUNNABLE'
NEEDS_QUALIFICATION = 'NEEDS_QUALIFICATION'
NEEDS_RECHECK = 'NEEDS_RECHECK'
NOT_RUNNABLE = 'NOT_RUNNABLE'


def runnable_state(opp):
    """Return {state, reason, remedy}. remedy in {'qualify','recheck',None}. Stored data only."""
    c = classify(opp)
    if opp.get('status') in TERMINAL_STATUSES:
        return {'state': NOT_RUNNABLE, 'reason': f"Closed as {str(opp.get('status')).lower()}.", 'remedy': None}
    if opp.get('reward_status') == 'EXPIRED':
        return {'state': NOT_RUNNABLE, 'reason': 'The reward has expired.', 'remedy': None}
    if opp.get('attempted') or opp.get('claimed'):
        return {'state': NOT_RUNNABLE, 'reason': 'Already attempted or claimed — a run exists for this bounty.', 'remedy': None}
    if not opp.get('repository') or opp.get('issue_number') in (None, ''):
        return {'state': NOT_RUNNABLE, 'reason': 'No linked GitHub issue to work on.', 'remedy': None}
    if opp.get('qualification_status') == 'REJECTED':
        return {'state': NOT_RUNNABLE, 'reason': 'Did not pass economic/scope qualification.', 'remedy': None}
    if opp.get('risk_status') == 'FLAGGED':
        return {'state': NOT_RUNNABLE, 'reason': 'Flagged by the safety / prompt-injection check.', 'remedy': None}
    if c['execution_authority'] == 'UNKNOWN':
        return {'state': NOT_RUNNABLE,
                'reason': 'No execution authority — not a workable GitHub-issue bounty (e.g. a wallet/claim-only listing).',
                'remedy': None}
    if opp.get('reward_status') == 'CONFLICTING':
        return {'state': NEEDS_RECHECK, 'reason': 'Reward data conflicts across sources — recheck before running.', 'remedy': 'recheck'}
    if opp.get('qualification_status') != 'QUALIFIED' or opp.get('risk_status') != 'CLEARED':
        return {'state': NEEDS_QUALIFICATION, 'reason': 'Not qualified yet — qualify it to make it runnable.', 'remedy': 'qualify'}
    # Passes every stored gate. Live checks (issue open, upstream accepts PRs, bounded scope) run on click.
    if opp.get('reward_status') == 'VERIFIED':
        note = 'Runnable now — verified reward.'
    else:
        note = 'Runnable now — payout stays a human claim (you approve on run).'
    return {'state': RUNNABLE, 'reason': note, 'remedy': None,
            'owner_approval_required': opp.get('reward_status') != 'VERIFIED'}


# --- review queue buckets ---
NEEDS_YOUR_REVIEW = 'NEEDS_YOUR_REVIEW'
DECLINED_BY_ENGINE = 'DECLINED_BY_ENGINE'
BLOCKED_EXTERNALLY = 'BLOCKED_EXTERNALLY'
DONE = 'DONE'

_EXTERNAL_MARKERS = (
    'interactions on this repository', 'restricted to prior contributors', 'issue is not an open',
    'issue no longer open', 'not an open issue', 'blocked_write', 'no authorized write target',
    'fork', 'publication http', 'publication', 'http 4', 'http 5', '422', '429', '502', '503',
    'credential', 'permission', 'rate limit', 'not found', 'base branch changed', 'identity changed',
    'draft response not confirmed', 'archived',
)
_DECLINED_MARKERS = (
    'not actionable', 'exceeds bounded', 'no supported inspectable', 'no inspectable',
    'out of scope', 'not valid bounded', 'empty or oversized', 'triage determined',
    'no relevant files', 'not decodable',
)


def review_bucket(job):
    """Sort a job into an honest bucket + a human outcome line. Non-review states -> DONE."""
    status = job.get('status')
    if status in ('WAITING_APPROVAL', 'APPROVED'):
        return {'bucket': NEEDS_YOUR_REVIEW,
                'outcome': 'A tested, review-approved patch is ready for your decision.'}
    if status in ('SUBMITTED', 'PAID', 'MERGED', 'REJECTED', 'CANCELLED', 'STOPPED', 'UNDER_REVIEW', 'DRAFT'):
        return {'bucket': DONE, 'outcome': f'Completed as {str(status).replace("_", " ").lower()}.'}
    if status == 'NEEDS_REVIEW':
        e = (job.get('error') or '').lower()
        fc = job.get('failure_class')
        if any(k in e for k in _EXTERNAL_MARKERS):
            return {'bucket': BLOCKED_EXTERNALLY, 'outcome': _external_outcome(e)}
        if any(k in e for k in _DECLINED_MARKERS):
            return {'bucket': DECLINED_BY_ENGINE,
                    'outcome': 'The engine correctly declined: the task was too broad or out of the bounded scope.'}
        if fc == 'REVIEW':
            return {'bucket': NEEDS_YOUR_REVIEW, 'outcome': 'A review raised concerns — worth your look.'}
        return {'bucket': NEEDS_YOUR_REVIEW, 'outcome': 'An ambiguous failure worth a look.'}
    return {'bucket': DONE, 'outcome': f'Status {status}.'}


def _external_outcome(e):
    if 'prior contributors' in e or 'interactions on this repository' in e:
        return 'Blocked externally: the upstream project restricts pull requests to prior contributors.'
    if 'open issue' in e or 'no longer open' in e or 'not an open' in e:
        return 'Blocked externally: the GitHub issue was closed or is no longer open.'
    if 'fork' in e:
        return 'Blocked externally: a write fork could not be created/authorized for this repository.'
    if 'rate limit' in e or '429' in e:
        return 'Blocked externally: GitHub rate limit / permission reached.'
    return 'Blocked externally (outside our control): the upstream repository or GitHub rejected publication.'
