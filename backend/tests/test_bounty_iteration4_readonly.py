"""Iteration 4 read-only diagnostic tests for Penniless capability truthfulness."""

# Modules/features covered: auth session reuse, read-only API evidence, workspace test replay, env/secret wiring, static capability gates.
import asyncio
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.append("/app/backend")
from bounty.workspace import Workspace


FRONTEND_ENV = dotenv_values("/app/frontend/.env")
BACKEND_ENV = dotenv_values("/app/backend/.env")
BASE_URL = (FRONTEND_ENV.get("REACT_APP_BACKEND_URL") or "").rstrip("/")


def _require_env():
    if not BASE_URL:
        pytest.skip("Missing REACT_APP_BACKEND_URL in /app/frontend/.env")
    if not BACKEND_ENV.get("MONGO_URL") or not BACKEND_ENV.get("DB_NAME"):
        pytest.skip("Missing MONGO_URL/DB_NAME in /app/backend/.env")


def _process_has_env_key(key: str) -> bool:
    """Boolean-only presence check from running backend process environment."""
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        cmdline = entry / "cmdline"
        environ = entry / "environ"
        try:
            cmd = cmdline.read_text(errors="ignore")
            if "/app/backend/server.py" not in cmd and "backend/server.py" not in cmd:
                continue
            env_raw = environ.read_bytes().split(b"\x00")
            return any(item.startswith(f"{key}=".encode()) for item in env_raw if item)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return False


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
def auth_client(owner_token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"})
    return s


def api(session: requests.Session, path: str, params=None):
    return session.get(f"{BASE_URL}/api/bounty{path}", params=params, timeout=40)


def test_config_presence_github_and_secret_file_permissions():
    has_token = _process_has_env_key("GITHUB_TOKEN")
    has_webhook = _process_has_env_key("GITHUB_WEBHOOK_SECRET")
    assert has_token is False
    assert has_webhook is False

    bounty_env = dotenv_values("/app/backend/bounty.env")
    secret_path = Path(bounty_env["EXPLABS_SECRET_FILE"])
    secret_dir = secret_path.parent
    assert secret_dir.exists() and secret_path.exists()
    assert stat.S_IMODE(secret_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600


def test_bootstrap_preview_and_owner_reads(auth_client):
    public = requests.Session()
    public_resp = api(public, "/bootstrap")
    assert public_resp.status_code == 200
    pdata = public_resp.json()
    assert pdata["preview"] is True
    assert pdata["jobs"] == []

    owner_resp = api(auth_client, "/bootstrap")
    assert owner_resp.status_code == 200
    odata = owner_resp.json()
    assert odata["preview"] is False
    assert odata["integrations"]["github"] == "NOT CONNECTED"
    assert odata["integrations"]["webhook"] == "NOT CONNECTED"


def test_owner_finance_jobs_and_no_data_mutation_for_gets(auth_client, mongo_db):
    before = {
        "jobs": mongo_db.jobs.count_documents({}),
        "payments": mongo_db.payments.count_documents({}),
        "opportunities": mongo_db.opportunities.count_documents({}),
        "audit_logs": mongo_db.audit_logs.count_documents({}),
    }

    live_finance = api(auth_client, "/finance", params={"simulated": "false"})
    sim_finance = api(auth_client, "/finance", params={"simulated": "true"})
    assert live_finance.status_code == 200
    assert sim_finance.status_code == 200
    live = live_finance.json()
    sim = sim_finance.json()
    assert live["verified_revenue"] == 0
    assert sim["verified_revenue"] >= 0

    jobs = list(mongo_db.jobs.find({}, {"_id": 0, "id": 1, "simulated": 1}))
    assert len(jobs) >= 1
    for job in jobs[:2]:
        detail = api(auth_client, f"/jobs/{job['id']}")
        assert detail.status_code == 200
        j = detail.json()
        assert j["id"] == job["id"]
        assert j["simulated"] is True

    after = {
        "jobs": mongo_db.jobs.count_documents({}),
        "payments": mongo_db.payments.count_documents({}),
        "opportunities": mongo_db.opportunities.count_documents({}),
        "audit_logs": mongo_db.audit_logs.count_documents({}),
    }
    assert before == after


def test_workspace_tests_only_on_allowlisted_fixture_jobs(mongo_db):
    os.environ.update({k: v for k, v in dotenv_values("/app/backend/bounty.env").items() if v is not None})
    fixture_repo = mongo_db.repositories.find_one({"full_name": "penniless-lab/slugify"}, {"_id": 0})
    assert fixture_repo is not None
    assert fixture_repo["allowed"] is True
    assert fixture_repo["push_authorized"] is False

    fixture_jobs = list(mongo_db.jobs.find({"repository": "penniless-lab/slugify", "simulated": True}, {"_id": 0, "id": 1}))
    assert len(fixture_jobs) >= 1

    for row in fixture_jobs[:2]:
        result = asyncio.run(Workspace(row["id"]).tests())
        assert result["exit_code"] in [0, 1]
        assert "Ran 7 tests" in result["output"]


def test_static_sources_show_capability_blocks_and_unwired_transport():
    orchestrator = Path("/app/backend/bounty/orchestrator.py").read_text()
    workspace = Path("/app/backend/bounty/workspace.py").read_text()
    github = Path("/app/backend/bounty/github.py").read_text()
    routes = Path("/app/backend/bounty/routes.py").read_text()
    model_router = Path("/app/backend/bounty/model_router.py").read_text()
    server = Path("/app/backend/server.py").read_text()

    assert "Live execution BLOCKED" in orchestrator
    assert "fixture.ORIGINAL" in workspace and "test_slugify.py" in workspace
    assert "External submission NOT CONNECTED" in routes
    assert "raise HTTPException(409, reason)" in model_router
    assert "branch(self, repo, base, branch)" in github
    assert "draft_pr(self, repo, title, body, head, base)" in github
    assert "pr_details(self, repo, number)" in github
    assert "EXPLABS_SECRET_FILE" not in server
