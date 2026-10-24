#!/usr/bin/env bash
# Start the Local Medical Assistant: [Ollama relay (WSL only)] + evidence gateway + API.
#
#   scripts/run.sh            # HTTP on 127.0.0.1:8600 (workstation only)
#   HOST=0.0.0.0 TLS_CERT=... TLS_KEY=... scripts/run.sh   # HTTPS on the clinic LAN (needed for phone cameras)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8600}"
LOGDIR="${MEDASSIST_LOG_DIR:-$HOME/.local/state/medassist}"
mkdir -p "$LOGDIR"
chmod 700 "$LOGDIR"

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

# WSL with Ollama installed on Windows: Windows-localhost is not reachable from a
# NAT-mode distro, so start the loopback-only relay through curl.exe.
if grep -qi microsoft /proc/version 2>/dev/null && ! curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null; then
  if command -v curl.exe >/dev/null || [ -x /mnt/c/Windows/System32/curl.exe ]; then
    echo "Starting WSL -> Windows Ollama relay on 127.0.0.1:11434"
    python3 "$ROOT/scripts/ollama_wsl_relay.py" >>"$LOGDIR/relay.log" 2>&1 &
    pids+=($!)
    sleep 1
  fi
fi

if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  echo "Building frontend…"
  (cd "$ROOT/frontend" && npm run build >/dev/null)
fi

# Branch deepseek-openrouter: DeepSeek V4.1 Flash 1017 via OpenRouter replaces local Qwen.
# This sends case content to a cloud provider. Use synthetic data. Set LLM=ollama to go back.
if [ "${LLM:-openrouter}" = "openrouter" ]; then
  export MEDASSIST_LLM_PROVIDER=openrouter MEDASSIST_ALLOW_CLOUD_MODEL=1
  echo "WARNING: cloud model ${MEDASSIST_OPENROUTER_MODEL:-deepseek/deepseek-v4.1-flash-1017} — case content is sent to OpenRouter." >&2
fi
# OCR via an OpenRouter vision model replaces local PaddleOCR. Set OCR=paddle to go back.
if [ "${OCR:-openrouter}" = "openrouter" ]; then
  export MEDASSIST_OCR_ENGINE=openrouter MEDASSIST_ALLOW_CLOUD_MODEL=1
  echo "WARNING: cloud OCR ${MEDASSIST_OCR_MODEL:-qwen/qwen3-vl-235b-a22b-instruct} — page images are sent to OpenRouter." >&2
fi

cd "$ROOT/backend"
# Create the API<->gateway shared secret once, before either process starts.
"$PY" -c "from app.config import settings; settings.gateway_secret()"
echo "Starting evidence gateway on 127.0.0.1:8601"
"$PY" -m uvicorn app.gateway_service:app --host 127.0.0.1 --port 8601 --no-access-log >>"$LOGDIR/gateway.log" 2>&1 &
pids+=($!)

TLS_ARGS=()
if [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ]; then
  TLS_ARGS=(--ssl-certfile "$TLS_CERT" --ssl-keyfile "$TLS_KEY")
  export MEDASSIST_SECURE_COOKIES=1
  SCHEME=https
else
  SCHEME=http
  if [ "$HOST" != "127.0.0.1" ]; then
    echo "WARNING: serving on $HOST without TLS. Use TLS_CERT/TLS_KEY (see scripts/make_local_ca.sh) for LAN access." >&2
  fi
fi
echo "Starting API on $SCHEME://$HOST:$PORT"
"$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --no-access-log "${TLS_ARGS[@]}" 2>&1 | tee -a "$LOGDIR/api.log" &
pids+=($!)
wait
