"""Phase 5 end-to-end owner-session tests.

GitHub read-only; NO provider/E2B. Uses existing owner session from Mongo.
"""
import os, time, json, asyncio, pytest, requests
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")
BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


async def _fetch_session():
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    ic = await db.instance_config.find_one({"_id": "instance_owner"})
    if not ic:
        return None
    s = await db.user_sessions.find_one({"user_id": ic["user_id"]}, sort=[("created_at", -1)])
    client.close()
    return s


def _tok():
    s = asyncio.new_event_loop().run_until_complete(_fetch_session())
    return s.get("session_token") if s else None


TOKEN = _tok()
H = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def test_unauth_endpoints_reject_401():
    for m, p in [
        ("GET", "/api/bounty/sources/health"),
        ("GET", "/api/bounty/discovery/config"),
        ("PUT", "/api/bounty/discovery/config"),
        ("POST", "/api/bounty/discovery/github/run"),
        ("POST", "/api/bounty/opportunities/anything/recheck"),
    ]:
        r = requests.request(m, BASE + p, json={}, timeout=15)
        assert r.status_code in (401, 403), f"{m} {p} => {r.status_code}"


def test_capability_matrix():
    if not TOKEN:
        pytest.skip("no owner session")
    r = requests.get(BASE + "/api/bounty/execution/status", headers=H, timeout=20)
    assert r.status_code == 200
    cm = r.json().get("capability_matrix", {})
    assert cm.get("LIVE_RECHECK") == "READY"
    assert cm.get("PERSISTENT_SCHEDULER") == "PARTIAL"
    assert cm.get("AUTO_ESCALATION") == "DISABLED"
    assert cm.get("UNATTENDED_EXECUTION") == "DISABLED"


@pytest.fixture
def good_query():
    """Temporarily set discovery config to a valid GitHub search query, restore after."""
    if not TOKEN:
        pytest.skip("no owner session")
    prev = requests.get(BASE + "/api/bounty/discovery/config", headers=H, timeout=15).json()
    payload = {"query": "is:issue is:open label:bounty", "per_page": 10,
               "enabled": prev.get("enabled", False)}
    requests.put(BASE + "/api/bounty/discovery/config", headers=H, json=payload, timeout=15)
    yield
    requests.put(BASE + "/api/bounty/discovery/config", headers=H,
                 json={"query": prev.get("query", "is:issue is:open label:bounty"),
                       "per_page": prev.get("per_page", 20),
                       "enabled": prev.get("enabled", False)}, timeout=15)


def test_github_discovery_and_dedup(good_query):
    r1 = requests.post(BASE + "/api/bounty/discovery/github/run", headers=H, json={}, timeout=60)
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert d1.get("ok") is True and d1.get("source") == "github"
    r2 = requests.post(BASE + "/api/bounty/discovery/github/run", headers=H, json={}, timeout=60)
    d2 = r2.json()
    assert d2.get("ok") is True
    assert d2["created"] <= d1["created"], "second run must not create more"
    assert d2["duplicates"] >= d1["duplicates"]

    opps = requests.get(BASE + "/api/bounty/opportunities", headers=H, timeout=15).json().get("opportunities", [])
    gh = [o for o in opps if o.get("source") == "github"]
    assert gh, "expected github opportunities"
    for o in gh[:5]:
        assert o.get("reward_verification_status") == "UNKNOWN", o
    # at least some in DISCOVERED status (others may already have been actioned in prior tests)
    assert any(o.get("status") == "DISCOVERED" for o in gh)


def test_sources_health(good_query):
    # ensure at least one run to populate
    requests.post(BASE + "/api/bounty/discovery/github/run", headers=H, json={}, timeout=60)
    r = requests.get(BASE + "/api/bounty/sources/health", headers=H, timeout=15)
    assert r.status_code == 200
    sources = r.json().get("sources", [])
    gh = next((s for s in sources if s.get("id") == "github"), None)
    assert gh, sources
    assert gh.get("status") == "HEALTHY", gh
    assert gh.get("total_discovered", 0) > 0


def test_live_recheck(good_query):
    requests.post(BASE + "/api/bounty/discovery/github/run", headers=H, json={}, timeout=60)
    opps = requests.get(BASE + "/api/bounty/opportunities", headers=H, timeout=15).json().get("opportunities", [])
    gh = [o for o in opps if o.get("source") == "github"]
    assert gh
    oid = gh[0]["id"]
    r = requests.post(BASE + f"/api/bounty/opportunities/{oid}/recheck", headers=H, json={}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert isinstance(d.get("fresh"), bool)
    if d["fresh"] is False:
        assert d.get("status") == "STALE_REQUIRES_RECHECK"
        assert d.get("reason")


def test_no_execution_budget_change():
    if not TOKEN:
        pytest.skip("no owner session")
    st = requests.get(BASE + "/api/bounty/execution/status", headers=H, timeout=15).json()
    assert st["run_budget"]["authorized"] is False
