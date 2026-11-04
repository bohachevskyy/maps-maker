#!/bin/bash
# Stop Codex workers when the 5-hour window reaches the cap (default 72%).
# Run in the background while workers are active; exits when none remain.
CAP=${1:-72}
DIR="$(cd "$(dirname "$0")" && pwd)"
while pgrep -f "codex exec --skip-git-repo-check -s workspace-write" >/dev/null; do
  out=$(python3 "$DIR/codex_usage.py" "$CAP"); rc=$?
  if [ $rc -eq 1 ]; then
    pkill -f "codex exec --skip-git-repo-check -s workspace-write"
    echo "STOPPED at cap: $out"; exit 0
  fi
  sleep 120
done
echo "workers finished: $(python3 "$DIR/codex_usage.py" "$CAP")"
