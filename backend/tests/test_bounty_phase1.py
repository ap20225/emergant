"""Phase 1 Penniless bounty API regression tests."""

# Modules/features covered: auth boundary, safe reference lifecycle, finance simulation, controls/guards.
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


FRONTEND_ENV = dotenv_values("/app/frontend/.env")
BASE_URL = (FRONTEND_ENV.get("REACT_APP_BACKEND_URL") or "").rstrip("/")


def _backend_env():
    values = dotenv_values("/app/backend/.env")
    if not values.get("MONGO_URL") or not values.get("DB_NAME"):
        pytest.skip("Missing backend MONGO_URL/DB_NAME in /app/backend/.env")
    return values


@pytest.fixture(scope="session")
def api_client():
    if not BASE_URL:
        pytest.skip("Missing REACT_APP_BACKEND_URL in /app/frontend/.env")
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


@pytest.fixture(scope="session")
def owner_token():
    env = _backend_env()
    mongo = MongoClient(env["MONGO_URL"])
    db = mongo[env["DB_NAME"]]
    owner = db.instance_config.find_one({"_id": "instance_owner"})
    if not owner:
        pytest.skip("No instance owner configured")
    session = db.user_sessions.find_one(
        {"user_id": owner["user_id"], "expires_at": {"$gt": datetime.now(timezone.utc)}},
        sort=[("expires_at", -1)],
    )
    if not session:
        pytest.skip("No non-expired owner session token in MongoDB")
    return session["session_token"]


@pytest.fixture(scope="session")
def auth_headers(owner_token):
    return {"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def auth_client(api_client, auth_headers):
    s = requests.Session()
    s.headers.update(auth_headers)
    return s


@pytest.fixture(scope="session")
def lifecycle_state():
    return {}


def api(session: requests.Session, method: str, path: str, json_body=None):
    return session.request(method, f"{BASE_URL}/api/bounty{path}", json=json_body, timeout=40)


def wait_for_status(session: requests.Session, job_id: str, expected: str, timeout_s: int = 140):
    start = time.time()
    last = None
    while time.time() - start < timeout_s:
        r = api(session, "GET", f"/jobs/{job_id}")
        assert r.status_code == 200
        last = r.json()
        if last.get("status") == expected:
            return last
        if last.get("status") in {"NEEDS_REVIEW", "FAILED", "CANCELLED", "REJECTED"}:
            pytest.fail(f"Job ended early in {last.get('status')}: {last.get('error')}")
        time.sleep(2)
    pytest.fail(f"Timeout waiting for {expected}. Last status={last.get('status') if last else 'unknown'}")


def ensure_job_id(session: requests.Session, state: dict):
    if state.get("job_id"):
        return state["job_id"]
    bs = api(session, "GET", "/bootstrap")
    assert bs.status_code == 200
    data = bs.json()
    opp_id = state.get("opportunity_id")
    if opp_id:
        for job in data.get("jobs", []):
            if job.get("opportunity_id") == opp_id:
                state["job_id"] = job["id"]
                return state["job_id"]
    pytest.skip("No lifecycle job available for current opportunity")


def test_bootstrap_public_preview(api_client):
    r = api(api_client, "GET", "/bootstrap")
    assert r.status_code == 200
    data = r.json()
    assert data["preview"] is True
    assert data["user"] is None
    assert data["opportunities"] == []
    assert data["jobs"] == []


def test_unauth_mutation_blocked(api_client):
    r = api(api_client, "POST", "/safe-test/discover")
    assert r.status_code == 401
    assert "Not authenticated" in r.json().get("detail", "")


def test_bootstrap_authenticated(auth_client):
    r = api(auth_client, "GET", "/bootstrap")
    assert r.status_code == 200
    data = r.json()
    assert data["preview"] is False
    assert data["user"]["email"]
    assert isinstance(data["settings"], dict)
    assert len(data["agents"]) >= 13


def test_private_job_detail_requires_auth(api_client):
    r = api(api_client, "GET", "/jobs/job_invalid123")
    assert r.status_code == 401


def test_safe_discover_score_and_dedup(auth_client, lifecycle_state):
    first = api(auth_client, "POST", "/safe-test/discover")
    assert first.status_code == 200
    item = first.json()
    lifecycle_state["opportunity_id"] = item["id"]
    evaln = item["evaluation"]
    assert evaln["expected_revenue"] == 28.8
    assert evaln["expected_profit"] == 28.8
    assert evaln["expected_value_per_hour"] == 57.6
    assert evaln["score"] > 0
    second = api(auth_client, "POST", "/safe-test/discover")
    assert second.status_code == 200
    assert second.json()["id"] == item["id"]


def test_pursue_duplicate_guard(auth_client, lifecycle_state):
    r = api(auth_client, "POST", f"/opportunities/{lifecycle_state['opportunity_id']}/pursue")
    if r.status_code == 200:
        job = r.json()
        lifecycle_state["job_id"] = job["id"]
        assert job["status"] == "SELECTED"
    else:
        assert r.status_code == 409
        ensure_job_id(auth_client, lifecycle_state)

    current = api(auth_client, "GET", f"/jobs/{lifecycle_state['job_id']}").json()
    if current["status"] == "NEEDS_REVIEW":
        retry = api(auth_client, "POST", f"/jobs/{lifecycle_state['job_id']}/retry")
        assert retry.status_code == 200

    dup = api(auth_client, "POST", f"/opportunities/{lifecycle_state['opportunity_id']}/pursue")
    assert dup.status_code == 409


def test_job_reaches_waiting_approval_and_has_real_evidence(auth_client, lifecycle_state):
    job_id = ensure_job_id(auth_client, lifecycle_state)
    job = wait_for_status(auth_client, job_id, "WAITING_APPROVAL")
    assert job["branch"].startswith("bounty/")
    assert job["test_status"] == "PASS"
    assert job["security_status"] == "PASS"
    assert job["review_status"] == "APPROVE"
    assert "slugify.py" in job.get("files_changed", [])
    assert '+    return "-".join(value.lower().split())' in (job.get("diff") or "")

    detail = api(auth_client, "GET", f"/jobs/{job_id}")
    assert detail.status_code == 200
    data = detail.json()
    lifecycle_state["pr_id"] = data.get("pr_id")
    phases = {t["phase"]: t for t in data.get("test_runs", [])}
    assert phases["baseline"]["status"] == "FAIL"
    assert phases["regression"]["status"] == "PASS"
    assert "Ran 7 tests" in phases["regression"]["output"]
    assert data["pull_requests"][0]["url"] is None
    assert data["pull_requests"][0]["status"] == "LOCAL_DRAFT"


def test_publish_is_blocked_for_reference_artifacts(auth_client, lifecycle_state):
    job_id = ensure_job_id(auth_client, lifecycle_state)
    r = api(auth_client, "POST", f"/jobs/{job_id}/publish")
    assert r.status_code == 409
    assert "NOT CONNECTED" in r.json().get("detail", "")


def test_github_and_webhook_not_connected_guards(auth_client):
    github = api(auth_client, "POST", "/github/check")
    assert github.status_code == 503
    assert "NOT CONNECTED" in github.json().get("detail", "")

    webhook = api(auth_client, "POST", "/webhooks/github")
    assert webhook.status_code == 503
    assert "NOT CONNECTED" in webhook.json().get("detail", "")


def test_github_import_url_validation(auth_client):
    bad = api(auth_client, "POST", "/github/import", {"url": "https://example.com/not-github"})
    assert bad.status_code == 422


def test_model_preflight_blocked_zero_budget(auth_client, lifecycle_state):
    job_id = ensure_job_id(auth_client, lifecycle_state)
    r = api(auth_client, "POST", f"/model/preflight/{job_id}")
    assert r.status_code == 409
    assert "Zero model budget" in r.json().get("detail", "") or "NOT CONNECTED" in r.json().get("detail", "")


def test_approve_then_reference_outcome(auth_client, lifecycle_state):
    job_id = ensure_job_id(auth_client, lifecycle_state)
    approve = api(auth_client, "POST", f"/jobs/{job_id}/approval", {"action": "APPROVE", "reason": "ok"})
    assert approve.status_code == 200
    assert approve.json()["status"] == "APPROVED"

    outcome = api(auth_client, "POST", f"/jobs/{job_id}/reference-outcome")
    assert outcome.status_code == 200
    assert outcome.json()["status"] == "MERGED"
    assert outcome.json()["payment_status"] == "PENDING"


def test_payments_finance_and_duplicate_guards(auth_client, lifecycle_state):
    job_id = ensure_job_id(auth_client, lifecycle_state)
    pay_ref = f"TEST_PAY_{uuid.uuid4().hex[:10]}"
    payment = api(auth_client, "POST", "/payments", {
        "job_id": job_id,
        "amount": 40,
        "reference": pay_ref,
        "method": "Local acceptance simulation",
        "verified": True,
        "attestation": True,
    })
    assert payment.status_code == 200
    assert payment.json()["payment_reference"] == pay_ref

    live_finance = api(auth_client, "GET", "/finance?simulated=false")
    sim_finance = api(auth_client, "GET", "/finance?simulated=true")
    assert live_finance.status_code == 200 and sim_finance.status_code == 200
    live = live_finance.json()
    sim = sim_finance.json()
    assert live["verified_revenue"] == 0
    assert sim["verified_revenue"] == 40
    assert sim["reserve"] == 28
    assert sim["reinvestment"] == 8
    assert sim["experimentation"] == 4

    exp_ref = f"TEST_EXP_{uuid.uuid4().hex[:10]}"
    expense = api(auth_client, "POST", "/expenses", {
        "amount": 1,
        "category": "compute",
        "description": "TEST compute expense",
        "reference": exp_ref,
        "simulated": True,
    })
    assert expense.status_code == 200

    sim_after = api(auth_client, "GET", "/finance?simulated=true")
    assert sim_after.status_code == 200
    assert sim_after.json()["net_profit"] == 39

    dup_payment = api(auth_client, "POST", "/payments", {
        "job_id": job_id,
        "amount": 40,
        "reference": pay_ref,
        "method": "Local acceptance simulation",
        "verified": True,
        "attestation": True,
    })
    assert dup_payment.status_code == 409

    dup_expense = api(auth_client, "POST", "/expenses", {
        "amount": 1,
        "category": "compute",
        "description": "TEST compute expense",
        "reference": exp_ref,
        "simulated": True,
    })
    assert dup_expense.status_code == 409


def test_pending_payment_not_recognized_as_revenue(auth_client, lifecycle_state):
    job_id = ensure_job_id(auth_client, lifecycle_state)
    pending_ref = f"TEST_PEND_{uuid.uuid4().hex[:10]}"
    pending = api(auth_client, "POST", "/payments", {
        "job_id": job_id,
        "amount": 12,
        "reference": pending_ref,
        "method": "Local acceptance simulation",
        "verified": False,
        "attestation": False,
    })
    assert pending.status_code == 200
    data = pending.json()
    assert data["payment_status"] == "PENDING"
    assert data["reward_received"] == 0


def test_controls_stop_spending_blocks_live_expense(auth_client):
    stop = api(auth_client, "POST", "/controls", {"action": "STOP_SPENDING"})
    assert stop.status_code == 200
    assert stop.json()["spending_stopped"] is True

    blocked = api(auth_client, "POST", "/expenses", {
        "amount": 1,
        "category": "compute",
        "description": "TEST live expense blocked",
        "reference": f"TEST_LIVE_{uuid.uuid4().hex[:8]}",
        "simulated": False,
    })
    assert blocked.status_code == 409

    resume = api(auth_client, "POST", "/controls", {"action": "RESUME"})
    assert resume.status_code == 200
    assert resume.json()["spending_stopped"] is False


def test_invalid_settings_validations(auth_client):
    bootstrap = api(auth_client, "GET", "/bootstrap")
    assert bootstrap.status_code == 200
    settings = bootstrap.json()["settings"]

    bad_weights = {**settings, "weights": {**settings["weights"], "reward": 26}}
    r1 = api(auth_client, "PUT", "/settings", bad_weights)
    assert r1.status_code == 422

    bad_alloc = {**settings, "reserve_percent": 71, "reinvestment_percent": 20, "experimentation_percent": 10}
    r2 = api(auth_client, "PUT", "/settings", bad_alloc)
    assert r2.status_code == 422


def test_restore_safe_controls_and_settings(auth_client):
    resume = api(auth_client, "POST", "/controls", {"action": "RESUME"})
    assert resume.status_code == 200

    bootstrap = api(auth_client, "GET", "/bootstrap")
    assert bootstrap.status_code == 200
    s = bootstrap.json()["settings"]
    target = {**s, "mode": "ASSISTED", "authorized_budget": 0}
    saved = api(auth_client, "PUT", "/settings", target)
    assert saved.status_code == 200
    assert saved.json()["mode"] == "ASSISTED"
    assert saved.json()["authorized_budget"] == 0
