#!/usr/bin/env bash
# One-time setup: MongoDB binary, backend/frontend deps, local .env files.
# Idempotent. Run from the repo root. State lives in $RUN_DIR (default /tmp/emergant-run).
set -euo pipefail
RUN_DIR=${RUN_DIR:-/tmp/emergant-run}
ROOT=$(git rev-parse --show-toplevel)
mkdir -p "$RUN_DIR"/{mongo-data,secrets,jobs,work,home}
chmod 700 "$RUN_DIR/secrets"

# 1. MongoDB (not installed in the container; apt has no package on 24.04)
if [ ! -x "$RUN_DIR/mongodb/bin/mongod" ]; then
  curl -sSfL -o "$RUN_DIR/mongo.tgz" https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-ubuntu2204-7.0.14.tgz
  mkdir -p "$RUN_DIR/mongodb" && tar xzf "$RUN_DIR/mongo.tgz" -C "$RUN_DIR/mongodb" --strip-components=1
  rm "$RUN_DIR/mongo.tgz"
fi

# 2. Backend deps. emergentintegrations lives on Emergent's private index -> skip it
#    (only bounty/telegram_bot.py imports it, lazily). Debian-owned PyYAML can't be
#    uninstalled, hence --ignore-installed PyYAML.
grep -v '^emergentintegrations' "$ROOT/backend/requirements.txt" > "$RUN_DIR/requirements.txt"
pip install -q --ignore-installed PyYAML -r "$RUN_DIR/requirements.txt" 2>&1 | grep -v -e WARNING -e 'requires.*incompatible' -e "dependency conflicts" -e "pip's dependency resolver" || true

# 3. Frontend deps
(cd "$ROOT/frontend" && yarn install --frozen-lockfile --silent)

# 4. Local env files (both gitignored). Safe values: no keys, model provider
#    pointed at a dead port so nothing can be billed. The real spend caps live in
#    the DB (Execution controls: daily $25, per-job $0.25), not in env. The
#    MAX_AUTONOMOUS_* / RUN_BUDGET_CAP / RUNNER_SPEND_AUTHORIZED vars below are
#    read only by old tests, never by production code; kept so those tests load.
#    E2B_API_URL is optional (SDK default endpoint when unset).
if [ ! -f "$ROOT/backend/.env" ]; then cat > "$ROOT/backend/.env" <<ENV
MONGO_URL=mongodb://localhost:27017
DB_NAME=bounty_local
CORS_ORIGINS=http://localhost:3000
BOUNTY_ALLOWED_ORIGINS=http://localhost:3000
BOUNTY_LEGACY_AUTOMATION_ENABLED=false
BOUNTY_EXECUTION_PATH=/usr/bin:/bin
BOUNTY_GIT=/usr/bin/git
BOUNTY_PYTHON=/usr/bin/python3
BOUNTY_GIT_AUTHOR_NAME=local
BOUNTY_GIT_AUTHOR_EMAIL=local@example.invalid
BOUNTY_JOBS_DIR=$RUN_DIR/jobs
BOUNTY_REMOTE_HOME=$RUN_DIR/home
BOUNTY_REMOTE_WORKDIR=$RUN_DIR/work
PENNILESS_SECRETS_DIR=$RUN_DIR/secrets
GITHUB_API_URL=https://api.github.com
GITHUB_API_VERSION=2022-11-28
GITHUB_WEB_URL=https://github.com
EXPLABS_BASE_URL=http://127.0.0.1:9/v1
EXPLABS_MANAGEMENT_URL=http://127.0.0.1:9
EXPLABS_REQUEST_TIMEOUT_SECONDS=10
FREE_ROUTE_MAX_EVIDENCE_AGE_SECONDS=3600
MAX_AUTONOMOUS_DAILY_SPEND_NANO_USD=0
MAX_AUTONOMOUS_SPEND_PER_JOB_NANO_USD=0
RUN_BUDGET_CAP_NANO_USD=0
RUN_BUDGET_RESERVE_RATE_NANO_PER_MILLION=0
RUNNER_SPEND_AUTHORIZED=false
RUNNER_COMMAND_TIMEOUT_SECONDS=60
RUNNER_HARD_TIMEOUT_SECONDS=600
RUNNER_VERIFY_TIMEOUT_SECONDS=60
REACT_APP_BACKEND_URL=http://localhost:8001
ENV
fi
[ -f "$ROOT/frontend/.env" ] || echo REACT_APP_BACKEND_URL=http://localhost:8001 > "$ROOT/frontend/.env"
echo "setup ok ($RUN_DIR)"
