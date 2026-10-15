#!/usr/bin/env bash
RUN_DIR=${RUN_DIR:-/tmp/emergant-run}
pkill -f "uvicorn server:app" || true
pkill -f "craco start" || true; pkill -f "yarn start" || true
"$RUN_DIR/mongodb/bin/mongod" --dbpath "$RUN_DIR/mongo-data" --shutdown >/dev/null 2>&1 || pkill -x mongod || true
echo stopped
