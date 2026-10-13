"""Iteration 9: read-only owner metadata invariants for saved GitHub/E2B state."""

import sys
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values


sys.path.insert(0, "/app/tests")
from session_utils import get_owner_session_token  # noqa: E402


FRONTEND_ENV = dotenv_values("/app/frontend/.env")
BASE_URL = FRONTEND_ENV["REACT_APP_BACKEND_URL"].rstrip("/")


@pytest.fixture(scope="session")
def owner_client() -> requests.Session:
    """Owner-authenticated session via in-memory existing session helper."""
    token = get_owner_session_token()
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    session.cookies.set("session_token", token)
    return session


def test_owner_auth_me(owner_client: requests.Session):
    """Auth module: owner session must still be valid."""
    response = owner_client.get(f"{BASE_URL}/api/auth/me", timeout=20)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data.get("user_id"), str) and data["user_id"]
    assert isinstance(data.get("email"), str) and "@" in data["email"]


def test_bootstrap_owner_not_preview(owner_client: requests.Session):
    """Bounty bootstrap module: owner should not be in preview mode."""
    response = owner_client.get(f"{BASE_URL}/api/bounty/bootstrap", timeout=20)
    assert response.status_code == 200
    data = response.json()
    assert data["preview"] is False


def test_execution_status_saved_github_metadata(owner_client: requests.Session):
    """Execution metadata module: saved GitHub connection remains connected/read-verified."""
    response = owner_client.get(f"{BASE_URL}/api/bounty/execution/status", timeout=20)
    assert response.status_code == 200
    data = response.json()

    github = data["github"]
    assert github["status"] == "CONNECTED"
    assert github["repository"] == "ap20225/bounty-hunter"
    assert github["issue_number"] == 1
    assert github["repository_read"] is True
    assert github["issue_read"] is True
    assert github["write_permission"] == "NOT_VERIFIED"


def test_execution_status_saved_runner_metadata(owner_client: requests.Session):
    """Execution metadata module: saved runner credential/template metadata is present."""
    response = owner_client.get(f"{BASE_URL}/api/bounty/execution/status", timeout=20)
    assert response.status_code == 200
    data = response.json()

    runner = data["runner"]
    assert runner["credential_configured"] is True
    assert runner["template_id"] == "openclaw"
    assert runner["status"] == "CONFIGURED_UNVERIFIED"
    assert runner["verified_at"] is None


def test_execution_status_blockers_do_not_require_missing_configs(owner_client: requests.Session):
    """Readiness module: saved GitHub/runner must not show *_REQUIRED blockers."""
    response = owner_client.get(f"{BASE_URL}/api/bounty/execution/status", timeout=20)
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "NOT_READY"
    blocker_codes = {item["code"] for item in data.get("blockers", [])}
    assert "GITHUB_REQUIRED" not in blocker_codes
    assert "RUNNER_REQUIRED" not in blocker_codes


def test_model_metadata_and_usage_invariants(owner_client: requests.Session):
    """Model gateway module: catalog/inference/usage invariants remain unchanged."""
    response = owner_client.get(f"{BASE_URL}/api/bounty/execution/status", timeout=20)
    assert response.status_code == 200
    data = response.json()

    model = data["model"]
    assert model["model_count"] == 302
    assert model["inference_verified_at"] is None
    assert isinstance(data.get("usage"), list)
    assert len(data["usage"]) == 0
