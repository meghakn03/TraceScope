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

api_port=${TRACESCOPE_API_PORT:-8000}
api_health() {
  .venv/bin/python -c "import json; from urllib.request import urlopen; data=json.loads(urlopen('http://127.0.0.1:${api_port}/api/health', timeout=0.5).read()); assert data.get('status') == 'ok' and data.get('storage') == 'sqlite + versioned JSON'" >/dev/null 2>&1
}
port_busy() {
  .venv/bin/python -c "import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0 else 1)" "$1"
}

if api_health; then
  echo "Found an existing TraceScope API on port $api_port; reusing it."
else
  while port_busy "$api_port"; do
    if [ "${TRACESCOPE_API_PORT+x}" = x ]; then
      echo "Port $api_port is already in use. Set TRACESCOPE_API_PORT to a free port and retry." >&2
      exit 1
    fi
    api_port=$((api_port + 1))
  done

  .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$api_port" &
  api_pid=$!
  attempt=0
  until api_health; do
    if ! kill -0 "$api_pid" 2>/dev/null; then
      echo "Could not start the API on port $api_port. Review the API error above." >&2
      exit 1
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "The API did not become ready within 30 seconds. Review the API output above." >&2
      exit 1
    fi
    sleep 1
  done
fi

TRACESCOPE_API_PORT=$api_port npm run dev &
web_pid=$!

echo "TraceScope is starting. Open http://localhost:5173 (API on port $api_port)"
echo "Press Ctrl+C to stop both services."
wait
