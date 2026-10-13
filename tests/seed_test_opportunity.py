"""Seed a TEST-only safe opportunity clone for UI approval/outcome flow."""

from datetime import datetime, timezone
from uuid import uuid4

from dotenv import dotenv_values
from pymongo import MongoClient


def main():
    env = dotenv_values("/app/backend/.env")
    mongo = MongoClient(env["MONGO_URL"])
    db = mongo[env["DB_NAME"]]

    primary = db.opportunities.find_one(
        {"repository": "penniless-lab/slugify", "source": "safe-test", "simulated": True},
        sort=[("created_at", -1)],
    )
    if not primary:
        raise RuntimeError("Primary safe-test opportunity not found")

    suffix = uuid4().hex[:8]
    clone = {k: v for k, v in primary.items() if k != "_id"}
    clone.update(
        {
            "id": f"opportunity_testui_{suffix}",
            "title": f"TEST_UI clone {suffix}: {primary.get('title', 'Safe opportunity')}",
            "issue_number": int(primary.get("issue_number", 1)) + int(datetime.now(timezone.utc).strftime("%M")) + 100,
            "dedup_key": f"penniless-lab/slugify#test-{suffix}",
            "status": "EVALUATED",
            "source": "safe-test",
            "simulated": True,
            "repository": "penniless-lab/slugify",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    existing = db.opportunities.find_one({"id": clone["id"]}, {"_id": 0})
    if not existing:
        db.opportunities.insert_one(clone)

    print(clone["id"])


if __name__ == "__main__":
    main()
