#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT_DIR"

command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required." >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Node.js is required." >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "npm is required." >&2; exit 1; }

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import fastapi, uvicorn, pydantic' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r backend/requirements.txt
fi
if [ ! -d node_modules ]; then
  npm install
fi

api_pid=
web_pid=
cleanup() {
  [ -z "$api_pid" ] || kill "$api_pid" 2>/dev/null || true
  [ -z "$web_pid" ] || kill "$web_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 &
api_pid=$!
npm run dev -- --host 127.0.0.1 &
web_pid=$!

echo "TraceScope is starting. Open http://localhost:5173"
echo "Press Ctrl+C to stop both services."
wait
