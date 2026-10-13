"""Reusable authenticated Playwright regression blueprint for origin-fix mutation flows.

Execute this logic inside an async Playwright context with `page` object.
Session token is fetched dynamically from MongoDB via /app/tests/session_utils.py.
"""

from urllib.parse import urlparse

from dotenv import dotenv_values

from session_utils import get_owner_session_token


def bootstrap_auth_cookie():
    base_url = dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"].rstrip("/")
    host = urlparse(base_url).hostname
    token = get_owner_session_token()
    return {
        "base_url": base_url,
        "cookie": {
            "name": "session_token",
            "value": token,
            "domain": host,
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
    }
