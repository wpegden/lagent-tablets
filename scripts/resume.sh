#!/bin/bash
# Resume the supervisor from its current state.
# Usage: ./scripts/resume.sh <repo_path_or_config> [extra_args...]

set -euo pipefail

TARGET="${1:?Usage: resume.sh <repo_path_or_config> [extra_args...]}"
shift
EXTRA_ARGS=("$@")

readarray -t META < <(python3 - "$TARGET" <<'PY'
import json
import re
import sys
from pathlib import Path

target = Path(sys.argv[1]).resolve()
if target.is_file():
    config_path = target
else:
    config_path = target / "lagent.config.json"
if not config_path.exists():
    raise SystemExit(f"Missing config: {config_path}")
raw = json.loads(config_path.read_text(encoding="utf-8"))
repo = Path(raw.get("repo_path", config_path.parent)).resolve()
session = str(((raw.get("tmux") or {}).get("session_name")) or re.sub(r"[^A-Za-z0-9_]+", "_", repo.name).strip("_") or "lagent_tablets")
print(repo)
print(config_path)
print(session)
PY
)

REPO="${META[0]}"
CONFIG="${META[1]}"
SESSION="${META[2]}"
LOG_DIR="$REPO/.agent-supervisor/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/supervisor_$(date +%Y%m%d_%H%M%S).log"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "ERROR: tmux session $SESSION already exists. Stop it first with ./scripts/stop.sh $REPO"
    exit 1
fi

echo "Resuming project:"
echo "  repo:    $REPO"
echo "  config:  $CONFIG"
echo "  session: $SESSION"
if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
    echo "  extra args: ${EXTRA_ARGS[*]}"
fi

tmux new-session -d -s "$SESSION" "cd /home/leanagent/src/lagent-tablets && python3 -u -m lagent_tablets.cli --config '$CONFIG' ${EXTRA_ARGS[*]} 2>&1 | tee '$LOG'"

sleep 3
echo "Log: $LOG"
tail -5 "$LOG" 2>/dev/null || true
