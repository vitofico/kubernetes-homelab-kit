#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [[ -n "${PYTHON:-}" ]]; then
  exec "$PYTHON" "$ROOT/tools/test-catalog.py" "$@"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" "$ROOT/tools/test-catalog.py" "$@"
else
  exec python3 "$ROOT/tools/test-catalog.py" "$@"
fi
