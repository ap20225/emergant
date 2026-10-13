"""Deterministic bounty-reward extraction + verification. No LLM, no network. Never fabricates a reward.

States: UNKNOWN, PARSED_UNVERIFIED, VERIFIED, CONFLICTING, EXPIRED.
Only VERIFIED (backed by an authoritative structured source) may satisfy the autonomous paid-work policy.
Free-text parsing yields at most PARSED_UNVERIFIED.
"""
import re

from .store import now

CURRENCY_TOKENS = ['USDC', 'USDT', 'USDS', 'USD', 'DAI', 'ETH', 'WETH', 'BTC', 'EUR', 'GBP',
                   'RTC', 'SOL', 'MATIC', 'OP', 'ARB']
_CODES = '|'.join(sorted(CURRENCY_TOKENS, key=len, reverse=True))
_AMT = r'(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?'
SYMBOL = re.compile(r'(?<![A-Za-z0-9])(?:US)?\$\s?' + _AMT)
AMOUNT_CODE = re.compile(r'(?<![A-Za-z0-9])' + _AMT + r'\s?(' + _CODES + r')(?![A-Za-z])', re.I)
CODE_AMOUNT = re.compile(r'(?<![A-Za-z0-9])(' + _CODES + r')\s?' + _AMT + r'(?![A-Za-z0-9])', re.I)
BOUNTY_CTX = re.compile(r'(?i)bount|reward|prize|payout|\bpays?\b|\bpaid\b|earn')


def _num(int_part, dec_part):
    v = float(int_part.replace(',', ''))
    if dec_part:
        v += float('0.' + dec_part)
    return v


def _candidates(text):
    """Returns list of (amount, currency, from_symbol) unique tuples. Requires a currency symbol/code."""
    out = []
    for m in SYMBOL.finditer(text):
        out.append((_num(m.group(1), m.group(2)), 'USD', True))
    for m in AMOUNT_CODE.finditer(text):
        out.append((_num(m.group(1), m.group(2)), m.group(3).upper(), False))
    for m in CODE_AMOUNT.finditer(text):
        out.append((_num(m.group(2), m.group(3)), m.group(1).upper(), False))
    seen, uniq = set(), []
    for amt, cur, sym in out:
        key = (amt, cur)
        if key not in seen:
            seen.add(key)
            uniq.append({'amount': amt, 'currency': cur, 'from_symbol': sym})
    return uniq


def _blank(status='UNKNOWN'):
    return {'reward_amount': None, 'reward_currency': None, 'reward_source': None,
            'reward_confidence': 0.0, 'reward_verified': False, 'reward_status': status,
            'reward_verified_at': None, 'candidates': []}


def extract_reward(title, body='', labels=None, comments=None, source_meta=None, deadline=None):
    """Deterministic reward extraction. `source_meta` may carry an authoritative amount/currency and expiry."""
    parts = [title or '', body or '']
    if labels:
        parts.append(' '.join(str(x) for x in labels))
    if comments:
        parts.append(' '.join(str(x) for x in comments))
    text = '  '.join(parts)
    has_ctx = bool(BOUNTY_CTX.search(text))

    cands = _candidates(text)
    # Symbol-only ($N) matches require bounty context so arbitrary "$5" prices aren't treated as rewards.
    cands = [c for c in cands if (not c['from_symbol']) or has_ctx]
    distinct = {(c['amount'], c['currency']) for c in cands}

    # Expiry check first (authoritative or provided deadline in the past).
    expires = (source_meta or {}).get('expires_at') or deadline
    if expires and isinstance(expires, str) and len(expires) >= 10 and expires < now():
        r = _blank('EXPIRED')
        r['candidates'] = cands
        return r

    # Authoritative structured source → VERIFIED (or CONFLICTING if text disagrees).
    meta_amt = (source_meta or {}).get('bounty_amount')
    if meta_amt not in (None, ''):
        try:
            amt = float(str(meta_amt).replace(',', ''))
        except (TypeError, ValueError):
            amt = None
        if amt is not None:
            cur = ((source_meta or {}).get('currency') or 'USD').upper()
            if distinct and (amt, cur) not in distinct and all(a != amt for a, _ in distinct):
                r = _blank('CONFLICTING')
                r.update({'reward_amount': amt, 'reward_currency': cur, 'reward_source': 'source_metadata',
                          'reward_confidence': 0.5, 'candidates': cands})
                return r
            return {'reward_amount': amt, 'reward_currency': cur, 'reward_source': 'source_metadata',
                    'reward_confidence': 1.0, 'reward_verified': True, 'reward_status': 'VERIFIED',
                    'reward_verified_at': now(), 'candidates': cands}

    # Text-only parsing: never VERIFIED.
    if len(distinct) > 1:
        r = _blank('CONFLICTING')
        r['candidates'] = cands
        return r
    if len(distinct) == 1:
        c = cands[0]
        return {'reward_amount': c['amount'], 'reward_currency': c['currency'], 'reward_source': 'issue_text',
                'reward_confidence': 0.6, 'reward_verified': False, 'reward_status': 'PARSED_UNVERIFIED',
                'reward_verified_at': None, 'candidates': cands}
    return _blank('UNKNOWN')
