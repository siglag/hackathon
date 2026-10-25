#!/usr/bin/env bash
# Start DeepSeek (OpenRouter), local OCR, the evidence gateway, and the API/UI. Run from anywhere: /path/to/project/start.sh
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -gt 0 ]]; then
  echo 'Usage: ./start.sh'
  echo 'Options use environment variables: MEDASSIST_OPENROUTER_API_KEY, MEDASSIST_OPENROUTER_MODEL, HOST, PORT, TLS_CERT, TLS_KEY.'
  [[ $1 == --help || $1 == -h ]] && exit 0
  exit 2
fi
cd "$ROOT"
umask 077
export MEDASSIST_LLM_PROVIDER=openrouter MEDASSIST_ALLOW_CLOUD_MODEL=1
export MEDASSIST_OPENROUTER_MODEL="${MEDASSIST_OPENROUTER_MODEL:-deepseek/deepseek-v4.1-flash-1017}"
export MEDASSIST_GATEWAY_URL="${MEDASSIST_GATEWAY_URL:-http://127.0.0.1:8601}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8600}"
LOGDIR="${MEDASSIST_LOG_DIR:-$HOME/.local/state/medassist}"
PY="$ROOT/backend/.venv/bin/python"
mkdir -p "$LOGDIR"

pids=()
cleanup() {
  trap - EXIT INT TERM
  if (( ${#pids[@]} )); then
    echo 'Stopping services started by start.sh...'
    kill "${pids[@]}" 2>/dev/null || true
    for (( attempt=0; attempt<5; attempt++ )); do
      alive=0
      for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then alive=1; fi
      done
      (( alive )) || break
      sleep 1
    done
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid" 2>/dev/null || true; fi
      wait "$pid" 2>/dev/null || true
    done
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'echo "Startup failed. Logs: $LOGDIR" >&2' ERR
die() { echo "Error: $*" >&2; exit 1; }
command -v curl >/dev/null || die 'curl is required.'
command -v npm >/dev/null || die 'Node.js and npm are required.'

# Install backend dependencies only when missing. Prefer the documented Python 3.12.
if [[ ! -x "$PY" ]]; then
  echo 'Creating backend Python environment...'
  if command -v uv >/dev/null; then
    uv venv --python 3.12 "$ROOT/backend/.venv"
  else
    SYSTEM_PY=''
    for candidate in python3.12 python3.13 python3.11 python3; do
      if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] < (3, 14)))'; then
        SYSTEM_PY="$candidate"
        break
      fi
    done
    [[ -n "$SYSTEM_PY" ]] || die 'Install uv or Python 3.11–3.13 (3.12 recommended).'
    "$SYSTEM_PY" -m venv "$ROOT/backend/.venv"
  fi
fi
if ! (cd "$ROOT/backend" && "$PY" - <<'PY'
import importlib.util
import os
import app.main
if os.environ.get("MEDASSIST_OCR_ENGINE", "paddle") != "none":
    assert importlib.util.find_spec("paddleocr") and importlib.util.find_spec("paddle")
PY
) > /dev/null 2>&1; then
  echo 'Installing backend and OCR dependencies...'
  if command -v uv >/dev/null; then
    uv pip install --python "$PY" -e "$ROOT/backend[ocr]"
  else
    "$PY" -m pip install -e "$ROOT/backend[ocr]"
  fi
fi

# Check local service ports before preparing OCR models or starting services.
GATEWAY_PORT="$("$PY" - "$HOST" "$PORT" <<'PY'
import os
import socket
import sys
from urllib.parse import urlsplit
u = urlsplit(os.environ["MEDASSIST_GATEWAY_URL"])
if u.scheme != "http" or u.hostname != "127.0.0.1" or u.path not in ("", "/") or u.query or u.fragment or u.username:
    sys.exit("MEDASSIST_GATEWAY_URL must be http://127.0.0.1:<port>.")
port = u.port or 80
for host, p in [(sys.argv[1], int(sys.argv[2])), ("127.0.0.1", port)]:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        # Match Uvicorn: recently closed connections must not block a restart.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, p))
        except OSError as e:
            sys.exit(f"Cannot start service on {host}:{p}: {e}")
if port == int(sys.argv[2]):
    sys.exit("API and gateway need different ports.")
print(port)
PY
)"
TLS_ARGS=()
SCHEME=http
if [[ -n ${TLS_CERT:-} || -n ${TLS_KEY:-} ]]; then
  [[ -r ${TLS_CERT:-} && -r ${TLS_KEY:-} ]] || die 'Set both TLS_CERT and TLS_KEY to readable files.'
  TLS_ARGS=(--ssl-certfile "$TLS_CERT" --ssl-keyfile "$TLS_KEY")
  export MEDASSIST_SECURE_COOKIES=1
  SCHEME=https
fi

echo "Checking DeepSeek via OpenRouter..."
(cd "$ROOT/backend" && "$PY" - <<'PYTHON'
from app.agent import llm
from app.config import settings
if not settings.openrouter_api_key():
    raise SystemExit("OpenRouter API key missing. Set MEDASSIST_OPENROUTER_API_KEY or create openrouter.key in MEDASSIST_DATA_DIR.")
if not settings.openrouter_model.startswith("deepseek/"):
    raise SystemExit("MEDASSIST_OPENROUTER_MODEL must select a deepseek/ model.")
status = llm.status()
if not status["ok"]:
    raise SystemExit(f"OpenRouter check failed: {status.get('error', 'unavailable')}")
print(f"LLM ready: {llm.model_label()} via {llm.provider_name()}")
PYTHON
)

echo 'Building frontend...'
if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  (cd "$ROOT/frontend" && npm ci)
fi
(cd "$ROOT/frontend" && npm run build)

wait_http() {
  local service="$1" url="$2" service_pid="$3"
  shift 3
  for (( retry=0; retry<60; retry++ )); do
    kill -0 "$service_pid" 2>/dev/null || die "$service exited. Check $LOGDIR."
    if curl --noproxy '*' -fsS --max-time 2 "$@" "$url" >/dev/null 2>&1; then return; fi
    sleep 1
  done
  die "$service did not become ready. Check $LOGDIR."
}

if [[ ${MEDASSIST_OCR_ENGINE:-paddle} != none ]]; then
  echo 'Preparing local OCR models using a synthetic image...'
  "$PY" -I "$ROOT/scripts/setup_models.py"
fi

cd "$ROOT/backend"
"$PY" -c 'from app.config import settings; settings.gateway_secret()'
echo "Starting evidence gateway on 127.0.0.1:$GATEWAY_PORT..."
"$PY" -m uvicorn app.gateway_service:app --host 127.0.0.1 --port "$GATEWAY_PORT" --no-access-log >>"$LOGDIR/gateway.log" 2>&1 &
pids+=("$!")
# Verify gateway readiness using the same shared secret as the API.
"$PY" - "$!" <<'PY'
import os
import sys
import time
import httpx
from app.config import settings
with httpx.Client(timeout=2, trust_env=False) as c:
    for _ in range(60):
        os.kill(int(sys.argv[1]), 0)
        try:
            r = c.get(settings.gateway_url.rstrip("/") + "/v1/health", headers={"X-Gateway-Key": settings.gateway_secret()})
            r.raise_for_status()
            assert r.json()["ok"]
            break
        except (httpx.HTTPError, ValueError, AssertionError):
            time.sleep(1)
    else:
        sys.exit("Gateway startup timed out; check gateway.log.")
PY
echo "Starting API and frontend on $SCHEME://$HOST:$PORT..."
"$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --no-access-log "${TLS_ARGS[@]}" >>"$LOGDIR/api.log" 2>&1 &
pids+=("$!")
CHECK_HOST="$HOST"
[[ "$HOST" != 0.0.0.0 ]] || CHECK_HOST=127.0.0.1
[[ "$HOST" != :: ]] || CHECK_HOST=::1
[[ "$CHECK_HOST" != *:* ]] || CHECK_HOST="[$CHECK_HOST]"
# --insecure is only for the local readiness probe with a clinic/self-signed cert.
wait_http API "$SCHEME://$CHECK_HOST:$PORT/api/auth/state" "$!" --insecure
echo "Ready: $SCHEME://$CHECK_HOST:$PORT — model: $MEDASSIST_OPENROUTER_MODEL (OpenRouter)"
echo "Logs: $LOGDIR | Press Ctrl+C to stop."
while sleep 1; do
  for pid in "${pids[@]}"; do
    kill -0 "$pid" 2>/dev/null || die "A service stopped unexpectedly. Check $LOGDIR."
  done
done
