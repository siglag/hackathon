#!/usr/bin/env bash
# Compatibility entry point: use the same DeepSeek stack as ./start.sh.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/start.sh" "$@"
