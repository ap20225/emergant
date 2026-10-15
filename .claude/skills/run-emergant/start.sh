#!/usr/bin/env bash
# Start mongod, backend (:8001) and frontend dev server (:3000) in the background,
# then block until both answer. Logs: $RUN_DIR/{mongo,backend,frontend}.log
set -euo pipefail
RUN_DIR=${RUN_DIR:-/tmp/emergant-run}
ROOT=$(git rev-parse --show-toplevel)
if ! pgrep -x mongod >/dev/null; then
  "$RUN_DIR/mongodb/bin/mongod" --dbpath "$RUN_DIR/mongo-data" --port 27017 --fork --logpath "$RUN_DIR/mongo.log" >/dev/null
fi
if ! curl -sf localhost:8001/api/ >/dev/null; then
  (cd "$ROOT/backend" && nohup python3 -m uvicorn server:app --host 0.0.0.0 --port 8001 > "$RUN_DIR/backend.log" 2>&1 &)
fi
if ! curl -sf localhost:3000 >/dev/null; then
  (cd "$ROOT/frontend" && BROWSER=none PORT=3000 nohup yarn start > "$RUN_DIR/frontend.log" 2>&1 &)
fi
for i in $(seq 1 120); do
  curl -sf localhost:8001/api/ >/dev/null && curl -sf localhost:3000 >/dev/null && { echo "up: backend :8001, frontend :3000"; exit 0; }
  sleep 2
done
echo "timed out; see $RUN_DIR/backend.log and frontend.log" >&2; exit 1
