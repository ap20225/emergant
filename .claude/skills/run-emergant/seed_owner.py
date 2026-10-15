"""Create a LOCAL-ONLY instance owner + session in the local Mongo, bypassing Google login.

Writes the session token to $RUN_DIR/session_token (never commit it). Refuses to run
unless MONGO_URL points at localhost, so it can't touch a real deployment.
"""
import os, secrets, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient

root = Path(__file__).resolve().parents[3]
load_dotenv(root / "backend" / ".env")
url = os.environ["MONGO_URL"]
if "localhost" not in url and "127.0.0.1" not in url:
    sys.exit(f"refusing: MONGO_URL is not local ({url})")
db = MongoClient(url)[os.environ["DB_NAME"]]
now = datetime.now(timezone.utc)
user = {"user_id": "user_localowner", "email": "owner@local.invalid", "name": "Local Owner", "picture": None, "created_at": now}
owner = db.instance_config.find_one({"_id": "instance_owner"})
if owner and owner["user_id"] != user["user_id"]:
    sys.exit(f"refusing: instance already owned by {owner.get('email')}")
db.users.update_one({"user_id": user["user_id"]}, {"$setOnInsert": user}, upsert=True)
db.instance_config.update_one({"_id": "instance_owner"}, {"$setOnInsert": {"user_id": user["user_id"], "email": user["email"], "name": user["name"], "locked_at": now}}, upsert=True)
token = secrets.token_hex(32)
db.user_sessions.insert_one({"user_id": user["user_id"], "session_token": token, "expires_at": now + timedelta(days=7), "created_at": now})
out = Path(os.environ.get("RUN_DIR", "/tmp/emergant-run")) / "session_token"
out.write_text(token); out.chmod(0o600)
print(f"owner seeded; token -> {out}")
