"""Phase 2 targeted regression: retry guardrails and pending payment verification."""

# Modules/features covered: /jobs/{id}/retry boundaries, /payments/{id}/verify flow, finance invariants.
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
def auth_client(owner_token):
    if not BASE_URL:
        pytest.skip("Missing REACT_APP_BACKEND_URL in /app/frontend/.env")
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"})
    return s


def api(session: requests.Session, method: str, path: str, json_body=None):
    return session.request(method, f"{BASE_URL}/api/bounty{path}", json=json_body, timeout=40)


def _pending_test_payment(auth_client: requests.Session):
    finance = api(auth_client, "GET", "/finance?simulated=true")
    assert finance.status_code == 200
    payload = finance.json()
    target = next(
        (p for p in payload.get("payments", []) if p.get("payment_status") == "PENDING" and str(p.get("reference", "")).startswith("TEST_PEND")),
        None,
    )
    if not target:
        pytest.skip("No pending TEST_PEND payment available for verification flow")
    return payload, target


def test_retry_guard_rejects_non_needs_review_job(auth_client):
    bootstrap = api(auth_client, "GET", "/bootstrap")
    assert bootstrap.status_code == 200
    job = next((j for j in bootstrap.json().get("jobs", []) if j.get("status") == "PAID"), None)
    if not job:
        pytest.skip("No PAID job available to validate retry guard")

    retry = api(auth_client, "POST", f"/jobs/{job['id']}/retry")
    assert retry.status_code == 409
    assert "Only an interrupted/blocked reference job" in retry.json().get("detail", "")


def test_pending_payment_verify_attestation_required(auth_client):
    _, payment = _pending_test_payment(auth_client)
    verify = api(auth_client, "POST", f"/payments/{payment['id']}/verify", {"attestation": False})
    assert verify.status_code == 422
    assert "attestation" in verify.json().get("detail", "").lower() or "verification" in verify.json().get("detail", "").lower()


def test_pending_payment_verify_then_duplicate_blocked_and_finance_updates(auth_client):
    before, payment = _pending_test_payment(auth_client)

    assert before["verified_revenue"] == 40
    assert before["gross_revenue"] == 40
    assert before["pending_receipts"] == 12
    assert before["net_profit"] == 39

    verify = api(auth_client, "POST", f"/payments/{payment['id']}/verify", {"attestation": True})
    assert verify.status_code == 200
    verified = verify.json()
    assert verified["id"] == payment["id"]
    assert verified["payment_status"] == "VERIFIED"
    assert verified["reference"] == payment["reference"]

    after_sim = api(auth_client, "GET", "/finance?simulated=true")
    assert after_sim.status_code == 200
    sim = after_sim.json()
    assert sim["verified_revenue"] == 52
    assert sim["gross_revenue"] == 52
    assert sim["pending_receipts"] == 0
    assert sim["net_profit"] == 51

    after_live = api(auth_client, "GET", "/finance?simulated=false")
    assert after_live.status_code == 200
    live = after_live.json()
    assert live["verified_revenue"] == 0
    assert live["gross_revenue"] == 0
    assert live["available_budget"] == 0

    duplicate = api(auth_client, "POST", f"/payments/{payment['id']}/verify", {"attestation": True})
    assert duplicate.status_code == 409
    assert "Only pending payments can be verified" in duplicate.json().get("detail", "")


def test_sub_cent_amounts_rejected(auth_client):
    invalid_payment = api(
        auth_client,
        "POST",
        "/payments",
        {
            "job_id": "job_2a5bc6a3deb2",
            "amount": 0.001,
            "reference": "TEST_SUBCENT_PAY",
            "method": "Local acceptance simulation",
            "verified": False,
            "attestation": False,
        },
    )
    assert invalid_payment.status_code == 422

    invalid_expense = api(
        auth_client,
        "POST",
        "/expenses",
        {
            "amount": 0.001,
            "category": "compute",
            "description": "TEST sub-cent expense",
            "reference": "TEST_SUBCENT_EXP",
            "simulated": True,
        },
    )
    assert invalid_expense.status_code == 422
