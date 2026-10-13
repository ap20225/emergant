"""Deterministic reward extraction/verification tests. No network, no LLM."""
import sys

sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env')
from bounty.rewards import extract_reward
from bounty.store import now


def test_symbol_amounts_with_context():
    assert extract_reward('[BOUNTY $100] Fix bug')['reward_amount'] == 100.0
    assert extract_reward('Reward: $250 for this', 'pays $250')['reward_currency'] == 'USD'
    r = extract_reward('Bounty $1,000 available', 'body')
    assert r['reward_amount'] == 1000.0 and r['reward_status'] == 'PARSED_UNVERIFIED'


def test_amount_code_and_crypto():
    assert extract_reward('Add feature', 'Reward 100 USD')['reward_currency'] == 'USD'
    assert extract_reward('t', 'pays 100 USDC')['reward_currency'] == 'USDC'
    assert extract_reward('[EASY BOUNTY: 5 RTC] star repo')['reward_amount'] == 5.0
    assert extract_reward('t', 'bounty 500 DAI')['reward_currency'] == 'DAI'
    assert extract_reward('t', 'reward 250 USDT')['reward_amount'] == 250.0


def test_arbitrary_numbers_not_treated_as_reward():
    # a bare price with no bounty context must not be parsed as a reward
    assert extract_reward('Upgrade to plan for $5 per month', 'nothing relevant here')['reward_status'] == 'UNKNOWN'
    assert extract_reward('Issue about error code 500', 'the number 42 appears')['reward_amount'] is None


def test_conflicting_amounts():
    r = extract_reward('Bounty $100 or maybe $250', 'reward differs')
    assert r['reward_status'] == 'CONFLICTING' and r['reward_amount'] is None


def test_missing_reward_is_unknown():
    r = extract_reward('Add a helper', 'small fix, no money mentioned')
    assert r['reward_status'] == 'UNKNOWN' and r['reward_verified'] is False and r['reward_amount'] is None


def test_verified_via_authoritative_source():
    r = extract_reward('Fix bug', 'reward 100 USD', source_meta={'bounty_amount': 100, 'currency': 'USD'})
    assert r['reward_status'] == 'VERIFIED' and r['reward_verified'] is True and r['reward_confidence'] == 1.0
    assert r['reward_source'] == 'source_metadata' and r['reward_verified_at']


def test_source_text_conflict_marks_conflicting():
    r = extract_reward('Fix bug', 'the reward text says $999', source_meta={'bounty_amount': 100, 'currency': 'USD'})
    assert r['reward_status'] == 'CONFLICTING' and r['reward_verified'] is False


def test_expired_bounty():
    past = '2000-01-01T00:00:00+00:00'
    r = extract_reward('[BOUNTY $100]', 'body', source_meta={'bounty_amount': 100, 'currency': 'USD', 'expires_at': past})
    assert r['reward_status'] == 'EXPIRED' and r['reward_verified'] is False


def test_text_only_never_verified():
    r = extract_reward('[BOUNTY $500] real task', 'this pays $500')
    assert r['reward_status'] == 'PARSED_UNVERIFIED' and r['reward_verified'] is False
    assert now()  # sanity
