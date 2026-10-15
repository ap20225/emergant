---
name: run-emergant
description: Set up, start, stop, screenshot and drive the Bounty Hunter app (FastAPI backend + React frontend + MongoDB) locally, signed out or signed in as a local owner. Use when asked to run, launch, open, start, screenshot or test this app, or to check that a UI/API change works in the real running app.
---

Bounty Hunter (repo `emergant`) is a FastAPI backend (`backend/server.py`, :8001) plus a CRA/craco React frontend (`frontend/`, :3000), backed by MongoDB. **Agent path:** run `setup.sh` once, then `start.sh`, then drive the UI with `drive.cjs` (Playwright, headless Chromium) or the API with `curl`. All of these live in `.claude/skills/run-emergant/`. Paths below are relative to the repo root. Runtime state (Mongo data, logs, screenshots, session token) lives in `$RUN_DIR`, which defaults to `/tmp/emergant-run`.

## Setup (once per container, about 2 minutes)

```bash
.claude/skills/run-emergant/setup.sh
```

The script:
- downloads the MongoDB 7.0 binary into `$RUN_DIR/mongodb`;
- pip-installs `backend/requirements.txt` except `emergentintegrations`;
- runs `yarn install` in `frontend/`;
- writes `backend/.env` and `frontend/.env` if they're missing. Both files are gitignored and use safe local values: no keys, every spend cap at 0, and the model provider pointed at a dead port.

## Run (agent path)

```bash
.claude/skills/run-emergant/start.sh                 # mongod + backend + frontend; blocks until both answer (~20s)
python3 .claude/skills/run-emergant/seed_owner.py    # optional: local owner + session -> $RUN_DIR/session_token
```

Drive the UI. Each route prints one JSON line (`h1`, `signedIn`, `screenshot`, `pageErrors`, `apiFails`) and writes a full-page PNG to `$RUN_DIR/shots/`. **Open the PNG and look at it.**

```bash
node .claude/skills/run-emergant/drive.cjs /                                   # signed out (read-only view)
node .claude/skills/run-emergant/drive.cjs --owner / /opportunities /jobs /settings /command-center
node .claude/skills/run-emergant/drive.cjs --owner --click "Take the tour" /   # click a button/link by visible text, then screenshot
```

Options: `--out DIR` changes where screenshots go. A PNG is named after its route, so a later run overwrites an earlier one; use `--out` to keep both. `FRONTEND_URL` overrides `http://localhost:3000`. Routes are listed in `frontend/src/App.js`: `/`, `/command-center`, `/opportunities`, `/jobs`, `/jobs/:jobId`, `/payments`, `/finance`, `/settings`, `/login`.

Drive the API:

```bash
curl -s localhost:8001/api/                       # {"message":"Penniless Bounty Operations API","mode":"ASSISTED",...}
curl -s localhost:8001/api/bounty/bootstrap       # public snapshot (empty lists on a fresh DB)
curl -s -H "Authorization: Bearer $(cat /tmp/emergant-run/session_token)" localhost:8001/api/auth/me   # needs seed_owner.py
```

Stop everything with:

```bash
.claude/skills/run-emergant/stop.sh
```

## Test

```bash
cd backend && set -a && . ./.env && set +a && python3 -m pytest -q -p no:cacheprovider tests/ --continue-on-collection-errors
```

On a clean checkout this gives **131 passed, 38 failed, 51 skipped, 7 errors** in about 6s. The failures already exist in the repo; they have nothing to do with this setup:
- test fakes are missing methods (`'_Store' object has no attribute 'one'`);
- function signatures changed (`missing ... 'exec_authority'`);
- some tests assume the Emergent `/app` layout (`FileNotFoundError`);
- 2 modules fail at import (`test_execution_controller.py`).

Compare against this baseline rather than expecting green. Without `set -a; . ./.env`, many tests fail with `KeyError: 'MONGO_URL'`.

## Gotchas

- **The real login can't be used locally.** It runs Google OAuth through Emergent (`demobackend.emergentagent.com`) and redirects back to the Emergent preview host. `seed_owner.py` writes `users`, `instance_config.instance_owner` and `user_sessions` directly. It refuses a non-localhost `MONGO_URL`, and it refuses if a different owner already holds the instance (the first owner locks it).
- **The session cookie is `Secure; SameSite=None` and still works over plain http.** Chromium treats `localhost` as a secure context, and :3000 and :8001 are the same site. `drive.cjs` sets one cookie on domain `localhost` and it covers both ports.
- **A guided-tour modal covers the dashboard on first visit.** `drive.cjs` suppresses it by presetting `localStorage.bh_walkthrough_done_v1=1`. To test the tour itself, click "Take the tour".
- **`emergentintegrations==0.1.0` is on Emergent's private index**, so pip reports "No matching distribution". Only `backend/bounty/telegram_bot.py` imports it, lazily. Everything else runs without it.
- **pip fails with `Cannot uninstall PyYAML 6.0.1, RECORD file not found`** because Debian owns that copy. `setup.sh` passes `--ignore-installed PyYAML`.
- **The backend reads many env vars with `os.environ[...]`, not `.get`.** Every one of them must exist or the matching route raises `KeyError`. The `.env` written by `setup.sh` has the full list.
- **Two backend log errors at startup are expected:** `supervisorctl: No such file` (the legacy OpenClaw gateway, deactivated) and `Bounty credentials MISSING: e2b, explabs`.
- **Don't run `pkill -f "uvicorn server:app"` inline in an agent Bash call.** The pattern matches the calling shell's own command line and kills it (exit 144). Use `stop.sh`.
- Ubuntu 24.04 has no apt package for MongoDB. The ubuntu2204 tarball runs fine.
- `yarn install` warns about `package-lock.json`. That warning is harmless.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `start.sh` times out | `tail $RUN_DIR/backend.log $RUN_DIR/frontend.log`. A `KeyError` on boot means `backend/.env` is missing a var. Delete it and rerun `setup.sh`. |
| `drive.cjs --owner` shows `signedIn:false` | The token expired or the DB was wiped. Rerun `seed_owner.py`. |
| `seed_owner.py`: `refusing: instance already owned by ...` | Another owner is in the local DB. `stop.sh`, `rm -rf $RUN_DIR/mongo-data/*`, then `start.sh` again. |
| `Cannot find module 'playwright'` | `drive.cjs` falls back to `npm root -g`. On this image Playwright is global at `/opt/node22/lib/node_modules`. |
