import json

import requests
from dotenv import dotenv_values
from session_utils import get_owner_session_token


def main():
    base = dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"].rstrip("/")
    token = get_owner_session_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    bootstrap = requests.get(f"{base}/api/bounty/bootstrap", headers=headers, timeout=30)
    sim = requests.get(f"{base}/api/bounty/finance?simulated=true", headers=headers, timeout=30)
    live = requests.get(f"{base}/api/bounty/finance?simulated=false", headers=headers, timeout=30)

    result = {
        "bootstrap_status": bootstrap.status_code,
        "sim_status": sim.status_code,
        "live_status": live.status_code,
        "settings": bootstrap.json().get("settings", {}),
        "controls": bootstrap.json().get("controls", {}),
        "sim_finance": sim.json() if sim.status_code == 200 else {},
        "live_finance": live.json() if live.status_code == 200 else {},
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
