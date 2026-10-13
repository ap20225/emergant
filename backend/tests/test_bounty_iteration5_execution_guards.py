"""Iteration 5 targeted execution-pipeline guard regression tests."""

# Modules/features covered: execution owner/auth guards, secret-safe validation, readiness blockers, diagnostic gate.
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from dotenv import dotenv_values
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pymongo import MongoClient
import sys

sys.path.append("/app/backend")
from bounty.execution_routes import execution_router


FRONTEND_ENV = dotenv_values("/app/frontend/.env")
BACKEND_ENV = dotenv_values("/app/backend/.env")
BASE_URL = (FRONTEND_ENV.get("REACT_APP_BACKEND_URL") or "").rstrip("/")


def _require_env():
    if not BASE_URL:
        pytest.skip("Missing REACT_APP_BACKEND_URL in /app/frontend/.env")
    if not BACKEND_ENV.get("MONGO_URL") or not BACKEND_ENV.get("DB_NAME"):
        pytest.skip("Missing MONGO_URL/DB_NAME in /app/backend/.env")


@pytest.fixture(scope="session")
def mongo_db():
    _require_env()
    client = MongoClient(BACKEND_ENV["MONGO_URL"])
    return client[BACKEND_ENV["DB_NAME"]]


@pytest.fixture(scope="session")
def owner_token(mongo_db):
    owner = mongo_db.instance_config.find_one({"_id": "instance_owner"})
    if not owner:
        pytest.skip("No instance owner configured")
    session = mongo_db.user_sessions.find_one(
        {"user_id": owner["user_id"], "expires_at": {"$gt": datetime.now(timezone.utc)}},
        sort=[("expires_at", -1)],
    )
    if not session:
        pytest.skip("No non-expired owner session token in MongoDB")
    return session["session_token"]


@pytest.fixture(scope="session")
def owner_client(owner_token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def anon_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def api(session: requests.Session, method: str, path: str, json_body=None):
    return session.request(method, f"{BASE_URL}/api/bounty{path}", json=json_body, timeout=40)


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/execution/status", None),
        ("POST", "/connections/github", {"repository": "owner/repo", "issue_number": 1, "token": "x" * 24}),
        ("POST", "/connections/github/verify", None),
        ("POST", "/connections/runner", {"api_key": "x" * 20, "template_id": "tmpl_1"}),
        ("POST", "/execution/start", {"issue_number": 1, "test_suite": "pytest", "confirm_execution": True}),
        ("POST", "/execution/job_fake/publish", {"confirm_publication": True}),
        ("POST", "/execution/job_fake/sync", None),
    ],
)
def test_execution_and_connection_endpoints_require_auth(anon_client, method, path, body):
    response = api(anon_client, method, path, body)
    assert response.status_code == 401


def test_execution_status_reports_not_ready_and_expected_blockers(owner_client):
    response = api(owner_client, "GET", "/execution/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "NOT_READY"
    blocker_codes = {b["code"] for b in data.get("blockers", [])}
    assert "PROVIDER_CAP_UNVERIFIED" in blocker_codes
    assert "CONTROLLED_RUN_REQUIRED" in blocker_codes
    assert "ZERO_SPEND_POLICY" in blocker_codes


def test_execution_status_runner_required_or_unverified(owner_client):
    response = api(owner_client, "GET", "/execution/status")
    assert response.status_code == 200
    blocker_codes = {b["code"] for b in response.json().get("blockers", [])}
    assert "RUNNER_REQUIRED" in blocker_codes or "RUNNER_UNVERIFIED" in blocker_codes


def test_secret_safe_validation_422_does_not_echo_token(owner_client):
    secret = "ghp_SUPER_SECRET_NEVER_ECHO_123456789"
    payload = {"repository": "owner/repo", "issue_number": 0, "token": secret}
    response = api(owner_client, "POST", "/connections/github", payload)
    assert response.status_code == 422
    body = response.json()
    detail = str(body.get("detail", ""))
    assert "Invalid configuration fields" in detail
    assert secret not in detail
    assert "owner/repo" not in detail


def test_model_diagnostic_is_fail_closed_and_no_reservation_consumed(owner_client, mongo_db):
    before = mongo_db.model_usage.count_documents({"id": "experiential-diagnostic-once"})
    response = api(owner_client, "POST", "/connections/model/diagnostic")
    assert response.status_code == 409
    assert "PROVIDER_CAP_UNVERIFIED" in response.json().get("detail", "")
    after = mongo_db.model_usage.count_documents({"id": "experiential-diagnostic-once"})
    assert before == after


def test_model_preflight_is_fail_closed_for_existing_job(owner_client, mongo_db):
    job = mongo_db.jobs.find_one({}, {"_id": 0, "id": 1})
    if not job:
        pytest.skip("No existing job available for preflight check")
    response = api(owner_client, "POST", f"/model/preflight/{job['id']}")
    assert response.status_code == 409
    assert any(
        marker in response.json().get("detail", "")
        for marker in ["PROVIDER_CAP_UNVERIFIED", "ZERO_SPEND_POLICY", "MODEL_ROUTE_REQUIRED"]
    )


def test_execution_start_requires_explicit_confirmation(owner_client):
    response = api(
        owner_client,
        "POST",
        "/execution/start",
        {"issue_number": 1, "test_suite": "pytest", "confirm_execution": False},
    )
    assert response.status_code == 409
    assert "Explicit controlled execution approval required" in response.json().get("detail", "")


def test_publish_requires_explicit_confirmation(owner_client):
    response = api(owner_client, "POST", "/execution/job_fake/publish", {"confirm_publication": False})
    assert response.status_code == 409
    assert "Explicit draft PR publication confirmation required" in response.json().get("detail", "")


def test_non_owner_forbidden_isolated_mock_router():
    class DummyStore:
        pass

    class DummyOrchestrator:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.tasks = {}
            self.live_worker = None

        async def stop(self):
            return None

    class DummyGitHub:
        pass

    async def owner_guard(x_user: str | None = Header(default=None)):
        if x_user != "owner":
            raise HTTPException(403, "Forbidden")
        return SimpleNamespace(user_id="owner")

    import tempfile
    import os

    temp_dir = tempfile.mkdtemp(prefix="penniless-secrets-")
    os.environ.setdefault("PENNILESS_SECRETS_DIR", temp_dir)

    app = FastAPI()
    app.include_router(execution_router(DummyStore(), DummyOrchestrator(), DummyGitHub(), owner_guard), prefix="/api/bounty")
    client = TestClient(app)

    response = client.get("/api/bounty/execution/status", headers={"x-user": "non-owner"})
    assert response.status_code == 403
