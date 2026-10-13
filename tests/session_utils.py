from datetime import datetime, timezone

from dotenv import dotenv_values
from pymongo import MongoClient


def _env():
    values = dotenv_values("/app/backend/.env")
    if not values.get("MONGO_URL") or not values.get("DB_NAME"):
        raise RuntimeError("Missing MONGO_URL/DB_NAME in /app/backend/.env")
    return values


def get_owner_session_token() -> str:
    values = _env()
    mongo = MongoClient(values["MONGO_URL"])
    db = mongo[values["DB_NAME"]]

    owner = db.instance_config.find_one({"_id": "instance_owner"})
    if not owner:
        raise RuntimeError("instance_owner not found")

    session = db.user_sessions.find_one(
        {"user_id": owner["user_id"], "expires_at": {"$gt": datetime.now(timezone.utc)}},
        sort=[("expires_at", -1)],
    )
    if not session:
        raise RuntimeError("No active owner session available")
    return session["session_token"]
