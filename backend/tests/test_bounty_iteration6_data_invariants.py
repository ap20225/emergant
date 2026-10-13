"""Iteration 6 data invariant checks on current preview state."""

# Modules/features covered: current simulated/live counters and non-revenue baseline invariants.
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


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


def test_preview_data_invariants(mongo_db, owner_token):
    simulated_jobs = mongo_db.jobs.count_documents({"simulated": True})
    simulated_payments = mongo_db.payments.count_documents({"simulated": True})
    external_prs = mongo_db.pull_requests.count_documents({"external": True})
    model_usage = mongo_db.model_usage.count_documents({})

    assert simulated_jobs == 2
    assert simulated_payments == 3
    assert external_prs == 0
    assert model_usage == 0

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"})
    live_finance = session.get(f"{BASE_URL}/api/bounty/finance", params={"simulated": "false"}, timeout=40)
    assert live_finance.status_code == 200
    payload = live_finance.json()
    assert payload.get("verified_revenue") == 0
