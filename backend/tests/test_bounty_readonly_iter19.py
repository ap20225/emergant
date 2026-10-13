"""Iteration 19 - Read-only verification of runner attestation, discovery board, controls, and fork negative safety."""
import os
import pymongo
import pytest
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


def _session_token():
    c = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = c[os.environ["DB_NAME"]]
    now = datetime.utcnow()
    s = db.user_sessions.find_one({"expires_at": {"$gt": now}}, sort=[("created_at", -1)])
    if not s:
        s = db.user_sessions.find_one(sort=[("created_at", -1)])
    assert s, "No owner session"
    return s["session_token"]


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    tok = _session_token()
    s.headers.update({"Authorization": f"Bearer {tok}"})
    s.cookies.set("session_token", tok)
    return s


def test_execution_status_runtime_verified(client):
    r = client.get(f"{BASE_URL}/api/bounty/execution/status", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    caps = data.get("capabilities", {})
    runner = data.get("runner", {})
    print("workspace=", caps.get("workspace"))
    print("runner=", runner)
    assert caps.get("workspace") == "RUNTIME_VERIFIED", f"workspace={caps.get('workspace')}"
    assert runner.get("runtime_verified") is True
    assert runner.get("status") == "RUNTIME_VERIFIED"

    atts = data.get("execution_attestations") or data.get("attestations") or {}
    rv = None
    if isinstance(atts, list):
        rv = next((a for a in atts if a.get("id") == "runner-verification" or a.get("kind") == "runner-verification" or a.get("name") == "runner-verification"), None)
    elif isinstance(atts, dict):
        rv = atts.get("runner-verification") or atts.get("runner_verification")
    if not rv:
        # Fallback: attestation attached under runner object
        rv = runner.get("attestation")
    print("runner-verification=", rv)
    assert rv, "Missing runner-verification attestation"
    assert rv.get("verified") is True
    assert rv.get("file_write") is True
    assert rv.get("non_root") is True
    assert rv.get("git") is True
    assert rv.get("python") is True
    assert "verified_at" in rv


def test_discovery_board_lanes(client):
    r = client.get(f"{BASE_URL}/api/bounty/discovery/board", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    items = data.get("items") or data.get("board") or []
    if isinstance(data, list):
        items = data
    print("total items:", len(items))
    lanes = {}
    for it in items:
        assert "execution_authority" in it, f"missing execution_authority: {it}"
        assert "lane" in it, f"missing lane: {it}"
        lanes[it["lane"]] = lanes.get(it["lane"], 0) + 1
    print("lane counts:", lanes)
    assert lanes.get("AUTONOMOUS", 0) == 0
    # Ensure other lanes populated
    assert (lanes.get("OWNER_APPROVAL", 0) + lanes.get("WATCHLIST", 0)) > 0


def test_execution_controls_background_discovery(client):
    r = client.get(f"{BASE_URL}/api/bounty/execution/controls", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    print("controls=", data)
    assert "background_discovery" in data
    assert data["background_discovery"] is True


def test_fork_authorize_negative(client):
    r = client.post(
        f"{BASE_URL}/api/bounty/repos/fork/authorize",
        json={"upstream": "facebook/react", "fork_repo": "ap20225/bounty-hunter"},
        timeout=30,
    )
    print("status", r.status_code, "body", r.text[:400])
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    detail = (r.json().get("detail") or "").lower()
    assert "fork" in detail
