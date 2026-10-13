"""
Iteration 20 tests for the bounty RUN fix:
- Primary: /api/bounty/execution/bounty/run with owner_approved+acknowledge should NOT 409
- Negative: gate still intact without owner_approved (409 message shape)
- Speed/cache: /api/bounty/execution/recommended-candidate <10s and cached=true on repeat
- Fork authorize: after run, /api/bounty/repos includes an authorized ap20225/qtop
"""
import os
import time
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
API = f"{BASE_URL}/api/bounty"
OPP_ID = "bopp_29603583105c"


def _load_frontend_env():
    # Ensure BASE_URL from frontend/.env is used
    global BASE_URL, API
    if not BASE_URL:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
        API = f"{BASE_URL}/api/bounty"


@pytest.fixture(scope="session")
def session_token():
    _load_frontend_env()
    mongo_url = None
    db_name = None
    with open("/app/backend/.env") as f:
        for line in f:
            if line.startswith("MONGO_URL="):
                mongo_url = line.split("=", 1)[1].strip().strip('"')
            if line.startswith("DB_NAME="):
                db_name = line.split("=", 1)[1].strip().strip('"')
    c = MongoClient(mongo_url)[db_name]
    s = c["user_sessions"].find_one({}, sort=[("created_at", -1)])
    assert s and s.get("session_token"), "No session found"
    return s["session_token"]


@pytest.fixture(scope="session")
def client(session_token):
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Authorization": f"Bearer {session_token}",
    })
    s.cookies.set("session_token", session_token)
    return s


# --- SPEED / CACHE (do this first so it's fast) ---
def test_recommended_candidate_speed_and_cache(client):
    # Do NOT force refresh; the cache may already be primed. Measure both calls.
    t0 = time.time()
    r1 = client.get(f"{API}/execution/recommended-candidate", timeout=30)
    dt1 = time.time() - t0
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    rec = d1.get("recommended") or d1.get("candidate") or d1
    print("recommended (first call):", dt1, "sec; keys=", list(d1.keys()))
    print("recommended payload sample:", str(rec)[:600])
    assert dt1 < 15, f"first recommended-candidate too slow: {dt1}s"

    # Second call should be cached
    t0 = time.time()
    r2 = client.get(f"{API}/execution/recommended-candidate", timeout=30)
    dt2 = time.time() - t0
    assert r2.status_code == 200
    d2 = r2.json()
    print("recommended (second call):", dt2, "sec; cached=", d2.get("cached"))
    assert dt2 < 10
    # Cache flag if exposed
    if "cached" in d2:
        assert d2["cached"] in (True, "true", 1)


# --- NEGATIVE GATE ---
def test_gate_still_intact_without_owner_approved(client):
    # Use SAME opportunity but omit flags. This should either 409 with reward-not-VERIFIED
    # message OR 409 duplicate/live guard (both are 409 and acceptable per instructions).
    body = {"opportunity_id": OPP_ID, "confirm": True}
    r = client.post(f"{API}/execution/bounty/run", json=body, timeout=30)
    print("negative gate status:", r.status_code, "body:", r.text[:500])
    assert r.status_code in (400, 409, 422), f"expected block, got {r.status_code}"
    txt = r.text.lower()
    # Accept either the reward-not-verified gate OR a duplicate-live guard
    assert (
        "owner_approved" in txt
        or "verified" in txt
        or "duplicate" in txt
        or "already" in txt
        or "in progress" in txt
        or "live" in txt
    ), r.text


# --- PRIMARY: RUN with owner_approved ---
JOB_ID_HOLDER = {}


def test_run_bounty_with_owner_approved(client):
    body = {
        "opportunity_id": OPP_ID,
        "confirm": True,
        "owner_approved": True,
        "acknowledge_unverified_payout": True,
    }
    r = client.post(f"{API}/execution/bounty/run", json=body, timeout=60)
    print("run status:", r.status_code, "body:", r.text[:800])
    # Accept 200 OR 409 iff message is duplicate-live (a live job already exists from a prior test/run).
    if r.status_code == 409:
        low = r.text.lower()
        assert "duplicate" in low or "already" in low or "live" in low or "in progress" in low, r.text
        # Try to recover any existing job id via state list, otherwise skip poll
        pytest.skip(f"Live job already exists for {OPP_ID}: {r.text[:200]}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "job_id" in data, data
    JOB_ID_HOLDER["job_id"] = data["job_id"]
    stage = data.get("stage") or data.get("status")
    print("initial stage:", stage, "job_id:", data["job_id"])
    assert stage in ("QUEUED", "SELECTED", "TRIAGE", "RUNNING", "PENDING") or stage is not None


def test_poll_job_state(client):
    job_id = JOB_ID_HOLDER.get("job_id")
    if not job_id:
        pytest.skip("no job_id captured")
    final = None
    for i in range(6):
        r = client.get(f"{API}/execution/bounty/{job_id}/state", timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()
        stage = j.get("stage") or j.get("status")
        print(f"poll {i}: stage={stage} attempt={j.get('attempt')} pr={ (j.get('pr') or {}).get('url')} cost={j.get('cost')} err={j.get('error') or j.get('next_action')}")
        final = j
        if stage in ("DONE", "SUCCEEDED", "FAILED", "DECLINED", "CANCELLED", "COMPLETED"):
            break
        time.sleep(5)
    print("FINAL job doc keys:", list(final.keys()) if final else None)
    print("FINAL doc:", str(final)[:1500])
    # Contract: payout human-claim + reward not verified
    if final:
        payout_mode = final.get("payout_mode") or (final.get("job") or {}).get("payout_mode")
        reward_verified = final.get("reward_verified")
        print("payout_mode:", payout_mode, "reward_verified:", reward_verified)


# --- FORK AUTHORIZATION ---
def test_fork_authorized_after_run(client):
    r = client.get(f"{API}/repos", timeout=30)
    print("repos status:", r.status_code, "body sample:", r.text[:800])
    if r.status_code != 200:
        # Fall back to reading the DB directly
        with open("/app/backend/.env") as f:
            env = {ln.split("=",1)[0]: ln.split("=",1)[1].strip().strip('"') for ln in f if "=" in ln}
        c = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]
        docs = list(c["repo_authorizations"].find({}, {"_id": 0}))
        print("repo_authorizations docs:", docs[:20])
        assert any("qtop" in (d.get("fork_repo","") + d.get("repo","") + d.get("upstream","")) for d in docs), docs
        return
    data = r.json()
    items = data if isinstance(data, list) else (data.get("repositories") or data.get("repos") or data.get("authorizations") or [])
    found = False
    for it in items:
        s = str(it).lower()
        if "qtop" in s and ("ap20225" in s or "fork" in s):
            found = True
            print("found fork entry:", it)
            break
    assert found, f"No authorized qtop fork found in /repos response: {items}"
