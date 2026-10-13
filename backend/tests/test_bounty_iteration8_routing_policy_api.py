"""Iteration 8 focused live API contracts for free-first routing policy."""

# Modules/features covered: owner-only GET/PUT routing policy, strict validation, persistence/reload,
# and no unintended mutation of operating settings or model-usage invariants.
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


FRONTEND_ENV = dotenv_values("/app/frontend/.env")
BACKEND_ENV = dotenv_values("/app/backend/.env")
BASE_URL = (FRONTEND_ENV.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
ORIGIN = f"{urlparse(BASE_URL).scheme}://{urlparse(BASE_URL).netloc}" if BASE_URL else ""


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


@pytest.fixture
def owner_client(owner_token):
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {owner_token}",
            "Content-Type": "application/json",
            "Origin": ORIGIN,
        }
    )
    return s


@pytest.fixture
def anonymous_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Origin": ORIGIN})
    return s


@pytest.fixture
def original_policy(owner_client):
    response = owner_client.get(f"{BASE_URL}/api/bounty/routing-policy", timeout=40)
    assert response.status_code == 200
    policy = deepcopy(response.json()["policy"])
    yield policy
    owner_client.put(f"{BASE_URL}/api/bounty/routing-policy", json=policy, timeout=40)


def test_routing_policy_requires_owner_authentication(anonymous_client):
    response = anonymous_client.get(f"{BASE_URL}/api/bounty/routing-policy", timeout=40)
    assert response.status_code == 401
    assert "Not authenticated" in str(response.json().get("detail", ""))


def test_get_routing_policy_has_expected_shape_and_no_legacy_primary(owner_client):
    response = owner_client.get(f"{BASE_URL}/api/bounty/routing-policy", timeout=40)

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy"] == "FREE_FIRST"
    assert payload["paid_execution"] == "DISABLED"
    assert "qwen3-coder-30b-a3b-instruct" in payload["policy"]["coding"]
    assert "primary_model" not in payload["policy"]
    assert "fallback_model" not in payload["policy"]


def test_put_routing_policy_persists_reload_and_does_not_change_settings_or_usage(owner_client, mongo_db, original_policy):
    before_settings = mongo_db.system_settings.find_one({"id": "settings"}, {"_id": 0}) or {}
    before_usage = mongo_db.model_usage.count_documents({})
    before_diag = mongo_db.model_usage.count_documents({"id": "experiential-diagnostic-once"})

    updated = deepcopy(original_policy)
    updated["max_free_attempts"] = 1
    updated["routine"] = ["gemini-3.5-flash-lite", "gpt-oss-20b", "qwen3-coder-flash"]
    updated["coding"] = ["qwen3-coder-next", "qwen3-coder-flash", "qwen3-coder-30b-a3b-instruct"]

    put_response = owner_client.put(
        f"{BASE_URL}/api/bounty/routing-policy",
        json=updated,
        timeout=40,
    )
    assert put_response.status_code == 200
    assert put_response.json()["policy"]["max_free_attempts"] == 1

    get_response = owner_client.get(f"{BASE_URL}/api/bounty/routing-policy", timeout=40)
    assert get_response.status_code == 200
    reloaded = get_response.json()["policy"]
    assert reloaded["max_free_attempts"] == 1
    assert reloaded["routine"] == updated["routine"]
    assert reloaded["coding"] == updated["coding"]

    after_settings = mongo_db.system_settings.find_one({"id": "settings"}, {"_id": 0}) or {}
    after_usage = mongo_db.model_usage.count_documents({})
    after_diag = mongo_db.model_usage.count_documents({"id": "experiential-diagnostic-once"})
    assert after_settings == before_settings
    assert after_usage == before_usage
    assert after_diag == before_diag


@pytest.mark.parametrize(
    "bad_value",
    [0, 3, 1.5],
)
def test_put_routing_policy_rejects_invalid_max_attempts(owner_client, original_policy, bad_value):
    invalid_payload = deepcopy(original_policy)
    invalid_payload["max_free_attempts"] = bad_value

    response = owner_client.put(f"{BASE_URL}/api/bounty/routing-policy", json=invalid_payload, timeout=40)
    assert response.status_code == 422
    assert response.json().get("detail") == "Invalid configuration fields. Check repository, issue number and credential format."


def test_put_routing_policy_rejects_duplicate_or_bad_model_ids(owner_client, original_policy):
    duplicate_payload = deepcopy(original_policy)
    duplicate_payload["coding"] = ["qwen3-coder-next", "qwen3-coder-next"]
    duplicate_response = owner_client.put(f"{BASE_URL}/api/bounty/routing-policy", json=duplicate_payload, timeout=40)

    bad_id_payload = deepcopy(original_policy)
    bad_id_payload["routine"] = ["gemini-3.5-flash-lite", "bad model id"]
    bad_id_response = owner_client.put(f"{BASE_URL}/api/bounty/routing-policy", json=bad_id_payload, timeout=40)

    assert duplicate_response.status_code == 422
    assert bad_id_response.status_code == 422
    assert duplicate_response.json().get("detail") == "Invalid configuration fields. Check repository, issue number and credential format."
    assert bad_id_response.json().get("detail") == "Invalid configuration fields. Check repository, issue number and credential format."


def test_put_routing_policy_rejects_forbidden_extra_paid_gate_fields(owner_client, original_policy):
    payload = deepcopy(original_policy)
    payload["enable_paid_execution"] = True
    payload["budget_override"] = 1
    payload["billing_proof"] = "sk_TEST_SECRET_SHOULD_NOT_ECHO"

    response = owner_client.put(f"{BASE_URL}/api/bounty/routing-policy", json=payload, timeout=40)
    detail = str(response.json().get("detail", ""))

    assert response.status_code == 422
    assert detail == "Invalid configuration fields. Check repository, issue number and credential format."
    assert "sk_TEST_SECRET_SHOULD_NOT_ECHO" not in detail
