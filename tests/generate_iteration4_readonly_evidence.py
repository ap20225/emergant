import json
from datetime import datetime, timezone

import requests
from dotenv import dotenv_values
from pymongo import MongoClient

from session_utils import get_owner_session_token


def counts(db):
    return {
        "opportunities": db.opportunities.count_documents({}),
        "jobs": db.jobs.count_documents({}),
        "pull_requests": db.pull_requests.count_documents({}),
        "payments": db.payments.count_documents({}),
        "expenses": db.expenses.count_documents({}),
        "audit_logs": db.audit_logs.count_documents({}),
        "webhook_events": db.webhook_events.count_documents({}),
        "simulated_jobs": db.jobs.count_documents({"simulated": True}),
        "external_prs": db.pull_requests.count_documents({"external": True}),
        "live_payments": db.payments.count_documents({"simulated": False}),
        "simulated_payments": db.payments.count_documents({"simulated": True}),
    }


def main():
    backend = dotenv_values("/app/backend/.env")
    frontend = dotenv_values("/app/frontend/.env")
    mongo = MongoClient(backend["MONGO_URL"])
    db = mongo[backend["DB_NAME"]]
    base = frontend["REACT_APP_BACKEND_URL"].rstrip("/")
    token = get_owner_session_token()

    owner = db.instance_config.find_one({"_id": "instance_owner"})
    owner_session_count = db.user_sessions.count_documents(
        {
            "user_id": owner["user_id"],
            "expires_at": {"$gt": datetime.now(timezone.utc)},
        }
    )

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    before = counts(db)

    checks = {}
    checks["public_bootstrap"] = requests.get(f"{base}/api/bounty/bootstrap", timeout=30)
    checks["owner_bootstrap"] = requests.get(f"{base}/api/bounty/bootstrap", headers=headers, timeout=30)
    checks["owner_finance_live"] = requests.get(f"{base}/api/bounty/finance?simulated=false", headers=headers, timeout=30)
    checks["owner_finance_simulated"] = requests.get(f"{base}/api/bounty/finance?simulated=true", headers=headers, timeout=30)

    jobs = list(db.jobs.find({}, {"_id": 0, "id": 1, "simulated": 1, "repository": 1}).sort("created_at", -1))
    job_checks = []
    for row in jobs:
        r = requests.get(f"{base}/api/bounty/jobs/{row['id']}", headers=headers, timeout=30)
        job_checks.append({
            "job_id": row["id"],
            "simulated": row.get("simulated"),
            "repository": row.get("repository"),
            "status": r.status_code,
            "response_status": r.json().get("status") if r.status_code == 200 else None,
        })

    after = counts(db)

    proc_env_presence = {"GITHUB_TOKEN": False, "GITHUB_WEBHOOK_SECRET": False}
    import pathlib
    proc = pathlib.Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_text(errors="ignore")
            if "/app/backend/server.py" not in cmd and "backend/server.py" not in cmd:
                continue
            environ = (entry / "environ").read_bytes().split(b"\x00")
            for key in proc_env_presence.keys():
                proc_env_presence[key] = any(item.startswith(f"{key}=".encode()) for item in environ if item)
            break
        except Exception:
            continue

    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "owner_session_count": owner_session_count,
        "process_env_presence": proc_env_presence,
        "db_counts_before": before,
        "db_counts_after": after,
        "db_counts_unchanged": before == after,
        "api_checks": {
            name: {
                "status": resp.status_code,
                "preview": (resp.json().get("preview") if resp.headers.get("content-type", "").startswith("application/json") else None),
            }
            for name, resp in checks.items()
        },
        "owner_integrations": checks["owner_bootstrap"].json().get("integrations", {}),
        "owner_metrics": checks["owner_bootstrap"].json().get("metrics", {}),
        "job_checks": job_checks,
    }
    with open("/app/test_reports/iteration4_readonly_evidence.json", "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
