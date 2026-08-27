#!/usr/bin/env bash
# Start the map service. Ctrl-C stops it.
#
#   ./run.sh              foreground on :8000
#   ./run.sh 8080         a different port
#   ./run.sh --reload     restart automatically when the code changes
#
set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
ARGS=()
for arg in "$@"; do
  case "$arg" in
    [0-9]*) PORT="$arg" ;;
    *)      ARGS+=("$arg") ;;
  esac
done

# The key lives in .env, which is gitignored. /map works without it;
# /describe and prompt-driven /export need it.
if [[ -f .env ]]; then
  set -a; source .env; set +a
else
  echo "note: no .env found — /map will work, /describe will return 503" >&2
fi

# Clear out anything already holding the port, so a restart is just a re-run.
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  echo "stopping what is already on :$PORT"
  lsof -ti ":$PORT" | xargs kill 2>/dev/null || true
  sleep 1
fi

echo "mapsvc on http://127.0.0.1:$PORT  (scale $(sed -n 's/^SCALE = "\(.*\)"/\1/p' mapsvc/registry.py))"
# ${ARGS[@]+...} guards the empty case: macOS ships bash 3.2, where an
# empty array under `set -u` is an "unbound variable" error.
exec uv run uvicorn mapsvc.api:app --host 127.0.0.1 --port "$PORT" ${ARGS[@]+"${ARGS[@]}"}
