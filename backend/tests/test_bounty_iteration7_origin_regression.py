"""Iteration 7 targeted regression for origin fix + owner safety boundaries."""

# Modules/features covered: CSRF origin boundary, owner auth behavior, secret-safe 422 path, and diagnostic no-spend invariant.
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


FRONTEND_ENV = dotenv_values("/app/frontend/.env")
BACKEND_ENV = dotenv_values("/app/backend/.env")
BOUNTY_ENV = dotenv_values("/app/backend/bounty.env")
BASE_URL = (FRONTEND_ENV.get("REACT_APP_BACKEND_URL") or "").rstrip("/")

EXPECTED_ALLOWED_ORIGINS = [
    "https://execution-control-3.preview.emergentagent.com",
    "https://model-router-lab.cluster-5.preview.emergentcf.cloud",
]


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


def bounty_api(session: requests.Session, method: str, path: str, json_body=None, origin: str | None = None):
    headers = {}
    if origin:
        headers["Origin"] = origin
    return session.request(
        method,
        f"{BASE_URL}/api/bounty{path}",
        json=json_body,
        headers=headers,
        timeout=40,
    )


def test_allowed_origins_env_exact_and_stale_alias_removed():
    raw = BOUNTY_ENV.get("BOUNTY_ALLOWED_ORIGINS", "")
    parsed = [value.strip().rstrip("/") for value in raw.split(",") if value.strip()]

    assert parsed == EXPECTED_ALLOWED_ORIGINS
    assert all("claw-init" not in value for value in parsed)


@pytest.mark.parametrize("trusted_origin", EXPECTED_ALLOWED_ORIGINS)
def test_trusted_owner_invalid_github_form_hits_422_not_403(owner_client, trusted_origin):
    fake_secret = "ghp_ITERATION7_SECRET_DO_NOT_ECHO_123456"
    payload = {
        "repository": "owner/repo",
        "issue_number": 0,
        "token": fake_secret,
    }
    response = bounty_api(owner_client, "POST", "/connections/github", payload, origin=trusted_origin)

    assert response.status_code == 422
    detail = str(response.json().get("detail", ""))
    assert detail == "Invalid configuration fields. Check repository, issue number and credential format."
    assert fake_secret not in detail
    assert "owner/repo" not in detail


@pytest.mark.parametrize(
    "untrusted_origin",
    [
        "https://model-router-lab.cluster-4.preview.emergentcf.cloud",  # stale alias candidate
        "https://evil.example.com",
        "https://execution-control-3.preview.emergentagent.com.evil.example",
    ],
)
def test_untrusted_origins_rejected_403_before_route_validation(owner_client, untrusted_origin):
    payload = {
        "repository": "owner/repo",
        "issue_number": 1,
        "token": "ghp_abcdefghijklmnopqrstuvwxyz",
    }
    response = bounty_api(owner_client, "POST", "/connections/github", payload, origin=untrusted_origin)

    assert response.status_code == 403
    assert response.json().get("detail") == "Untrusted request origin"


def test_anonymous_trusted_origin_valid_shape_is_401_not_403():
    anon = requests.Session()
    anon.headers.update({"Content-Type": "application/json"})

    payload = {
        "repository": "owner/repo",
        "issue_number": 1,
        "token": "ghp_abcdefghijklmnopqrstuvwxyz",
    }
    response = bounty_api(
        anon,
        "POST",
        "/connections/github",
        payload,
        origin="https://execution-control-3.preview.emergentagent.com",
    )

    assert response.status_code == 401
    assert "Not authenticated" in str(response.json().get("detail", ""))


def test_real_diagnostic_gate_is_409_and_model_usage_stays_zero(owner_client, mongo_db):
    before = mongo_db.model_usage.count_documents({})
    response = bounty_api(
        owner_client,
        "POST",
        "/connections/model/diagnostic",
        json_body=None,
        origin="https://execution-control-3.preview.emergentagent.com",
    )
    after = mongo_db.model_usage.count_documents({})

    assert response.status_code == 409
    assert "PROVIDER_CAP_UNVERIFIED" in str(response.json().get("detail", ""))
    assert before == 0
    assert after == 0
    assert before == after
