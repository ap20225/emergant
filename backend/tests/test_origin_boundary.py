"""Deterministic tests for the shared /api/bounty origin allowlist (origin_policy)."""
import re

from bounty.origin_policy import origin_allowed, cors_origin_regex

CONFIGURED = [
    'https://execution-control-3.preview.emergentagent.com',
    'https://execution-control-3.cluster-5.preview.emergentcf.cloud',
]


def test_registered_agent_origin_pass():
    assert origin_allowed('https://execution-control-3.preview.emergentagent.com', CONFIGURED)


def test_current_preview_cluster_origin_pass():
    # The actual browser Origin from the Emergent preview (the reported failing case).
    assert origin_allowed('https://execution-control-3.cluster-5.preview.emergentcf.cloud', CONFIGURED)


def test_sibling_cluster_number_change_pass():
    # Only the agent host-form is pinned; a different cluster number of the SAME slug still passes.
    assert origin_allowed('https://execution-control-3.cluster-9.preview.emergentcf.cloud',
                          ['https://execution-control-3.preview.emergentagent.com'])


def test_unregistered_malicious_origin_block():
    assert origin_allowed('https://evil.example.com', CONFIGURED) is False


def test_different_app_slug_block():
    assert origin_allowed('https://some-other-app.cluster-5.preview.emergentcf.cloud', CONFIGURED) is False


def test_missing_origin_preserved():
    assert origin_allowed(None, CONFIGURED) is True
    assert origin_allowed('', CONFIGURED) is True


def test_non_https_origin_block():
    assert origin_allowed('http://execution-control-3.preview.emergentagent.com', CONFIGURED) is False


def test_cors_regex_matches_both_forms_only():
    rx = re.compile(cors_origin_regex(CONFIGURED))
    assert rx.match('https://execution-control-3.preview.emergentagent.com')
    assert rx.match('https://execution-control-3.cluster-5.preview.emergentcf.cloud')
    assert rx.match('https://execution-control-3.cluster-42.preview.emergentcf.cloud')
    assert not rx.match('https://evil.example.com')
    assert not rx.match('https://other-app.cluster-5.preview.emergentcf.cloud')
