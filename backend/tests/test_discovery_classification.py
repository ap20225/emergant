"""Deterministic tests for discovery classification + adapters. NO network, NO DB, NO LLM."""
import sys
sys.path.insert(0, '/app/backend')

from bounty.classification import (classify, payability, executability, priority, lane,
                                   blocking_reasons, canonical_identity, rank)
from bounty.autonomy import OpireAdapter, AlgoraAdapter, canonical_duplicate_key
from bounty.rewards import extract_reward


def _opp(**over):
    base = {'status': 'AWAITING_AUTHORIZATION', 'qualification_status': 'QUALIFIED', 'risk_status': 'CLEARED',
            'reward_status': 'VERIFIED', 'repository': 'org/repo', 'issue_number': 7, 'reward_amount': 200,
            'payout_certainty': 'FUNDED_ESCROW', 'attempted': False}
    base.update(over)
    return base


def test_structured_reward_verified_vs_text_only():
    verified = extract_reward('Fix bug', source_meta={'bounty_amount': 250, 'currency': 'USD'})
    assert verified['reward_status'] == 'VERIFIED'
    text = extract_reward('Bounty $250 for this fix')
    assert text['reward_status'] == 'PARSED_UNVERIFIED'  # issue text never VERIFIED


def test_payability_states():
    assert payability(_opp(reward_status='VERIFIED')) == 'YES'
    assert payability(_opp(reward_status='PARSED_UNVERIFIED')) == 'UNKNOWN'
    assert payability(_opp(reward_status='CONFLICTING')) == 'UNKNOWN'
    assert payability(_opp(reward_status='EXPIRED')) == 'NO'


def test_executability_hard_gates():
    assert executability(_opp()) == 'YES'
    assert executability(_opp(qualification_status='REJECTED')) == 'NO'
    assert executability(_opp(risk_status='FLAGGED')) == 'NO'
    assert executability(_opp(attempted=True)) == 'NO'
    assert executability(_opp(repository=None)) == 'UNKNOWN'
    assert executability(_opp(qualification_status='PENDING')) == 'UNKNOWN'


def test_no_age_freshness_hard_gate():
    old = _opp(issue_created_at='2019-01-01T00:00:00+00:00', reward_created_at='2019-01-02T00:00:00+00:00')
    assert executability(old) == 'YES'  # old bounty still executable
    assert 'REWARD_NOT_VERIFIED' not in blocking_reasons(old, 'YES', 'YES', 'FUNDED_ESCROW')
    for reason in blocking_reasons(old, 'YES', 'YES', 'FUNDED_ESCROW'):
        assert 'AGE' not in reason and 'FRESH' not in reason and 'OLD' not in reason


def test_no_solver_count_hard_gate():
    crowded = _opp(solver_count=25)
    assert executability(crowded) == 'YES'  # high competition still executable/visible
    assert lane(crowded, 'YES', 'YES', 'FUNDED_ESCROW') == 'AUTONOMOUS'
    cat, _, _ = priority(crowded)
    assert cat in ('HOT', 'HIGH', 'MEDIUM', 'LOW')  # competition affects priority only


def test_newly_funded_old_issue_is_hot():
    from datetime import datetime, timezone
    recent = datetime.now(timezone.utc).isoformat()
    opp = _opp(issue_created_at='2020-01-01T00:00:00+00:00', reward_created_at=recent,
               reward_amount=800, solver_count=0, payout_certainty='FUNDED_ESCROW')
    cat, score, signals = priority(opp)
    assert 'NEWLY_FUNDED' in signals and 'LOW_COMPETITION' in signals and 'HIGH_VALUE' in signals
    assert cat == 'HOT'
    assert executability(opp) == 'YES'  # still eligible regardless of old issue date


def test_low_competition_priority_signal():
    _, _, signals = priority(_opp(solver_count=0))
    assert 'LOW_COMPETITION' in signals


def test_payout_certainty_gates_autonomous_lane_only():
    # low certainty -> not autonomous, but still visible (owner approval), never a hard reject
    opp = _opp(payout_certainty='MANUAL_CLAIM', reward_status='PARSED_UNVERIFIED')
    c = classify(opp)
    assert c['lane'] in ('OWNER_APPROVAL', 'WATCHLIST')
    assert 'PAYOUT_CERTAINTY_INSUFFICIENT_FOR_AUTONOMOUS' in c['blocking_reasons']
    assert rank('FUNDED_ESCROW') > rank('MANUAL_CLAIM') > rank('UNKNOWN')


def test_three_lanes():
    assert classify(_opp())['lane'] == 'AUTONOMOUS'
    owner = _opp(reward_status='PARSED_UNVERIFIED', payout_certainty='MANUAL_CLAIM', reward_amount=100)
    assert classify(owner)['lane'] == 'OWNER_APPROVAL'
    watch = _opp(reward_status='UNKNOWN', payout_certainty='UNKNOWN', reward_amount=None, qualification_status='PENDING')
    assert classify(watch)['lane'] == 'WATCHLIST'


def test_cross_source_dedup_identity():
    # Same underlying issue across GitHub + Opire resolves to ONE canonical identity.
    assert canonical_identity('Org/Repo', 42) == 'org/repo#42'
    assert canonical_identity('org/repo', 42) == canonical_identity('ORG/REPO', 42)
    # per-source duplicate keys differ by source, but canonical identity unifies them
    assert canonical_duplicate_key('github', 'org/repo', 42) != canonical_duplicate_key('opire', 'org/repo', 42)


def test_opire_adapter_normalize_manual_claim():
    a = OpireAdapter(store=None)
    n = a.normalize({'id': 'x1', 'title': '[BOUNTY] Fix [500$]', 'url': 'https://github.com/microg/GmsCore/issues/2994',
                     'createdAt': 1776963564265, 'claimerUsers': [{'id': 'a'}, {'id': 'b'}], 'programmingLanguages': ['Java']})
    assert n['repository'] == 'microg/GmsCore' and n['issue_number'] == 2994
    assert n['payout_authority'] == 'opire' and n['payout_certainty'] == 'MANUAL_CLAIM'
    assert n['authoritative_reward'] is False  # Opire text is never auto-verified
    assert n['solver_count'] == 2 and n['reward_created_at'].startswith('20')


def test_algora_adapter_normalize_escrow_cents():
    a = AlgoraAdapter(store=None)
    n = a.normalize({'id': 'b1', 'amount': 10000, 'currency': 'USD',
                     'issue': {'html_url': 'https://github.com/foo/bar/issues/9', 'title': 'T', 'state': 'open'}})
    assert n['repository'] == 'foo/bar' and n['issue_number'] == 9
    assert n['reward'] == 100.0 and n['authoritative_reward'] is True  # 10000 cents -> $100
    assert n['payout_certainty'] == 'FUNDED_ESCROW' and n['payout_authority'] == 'algora'


def test_missing_data_still_visible_low_priority():
    cat, score, _ = priority({'reward_amount': None})
    assert cat == 'LOW'  # never hidden; just low priority
