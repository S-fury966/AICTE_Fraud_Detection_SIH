#!/usr/bin/env bash
# One command for the demo: generate the synthetic dataset, load it through the
# real pipeline into a fresh database, measure it, then serve API + dashboard.
#
#   ./run_demo.sh            API on 127.0.0.1:8765, dashboard on localhost:5175
#   API_PORT=8800 ./run_demo.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
API_PORT="${API_PORT:-8765}"

cd "$ROOT/backend"
"$PY" -m demo.generate
"$PY" -m demo.load --fresh
"$PY" -m demo.evaluate > /dev/null && echo "evaluation written to data/demo/evaluation.json"

"$PY" -m app.api --port "$API_PORT" &
API_PID=$!
trap 'kill $API_PID 2>/dev/null' EXIT

cd "$ROOT/frontend"
[ -d node_modules ] || npm install
API_TARGET="http://127.0.0.1:$API_PORT" npx vite
