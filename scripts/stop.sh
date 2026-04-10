#!/bin/bash
# Force stop one project supervisor and its agent processes.
# Usage: ./scripts/stop.sh [repo_path_or_config]

set -euo pipefail

TARGET="${1:-/home/leanagent/math/extremal}"
BURST_USER="${BURST_USER:-lagentworker}"

readarray -t META < <(python3 - "$TARGET" <<'PY'
import json
import re
import sys
from pathlib import Path

target = Path(sys.argv[1]).resolve()
if target.is_file():
    config_path = target
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    repo = Path(raw.get("repo_path", config_path.parent)).resolve()
    session = str(((raw.get("tmux") or {}).get("session_name")) or re.sub(r"[^A-Za-z0-9_]+", "_", repo.name).strip("_") or "lagent_tablets")
else:
    repo = target
    config_path = repo / "lagent.config.json"
    session = re.sub(r"[^A-Za-z0-9_]+", "_", repo.name).strip("_") or "lagent_tablets"
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            session = str(((raw.get("tmux") or {}).get("session_name")) or session)
        except Exception:
            pass
print(repo)
print(config_path)
print(session)
PY
)

REPO="${META[0]}"
CONFIG="${META[1]}"
SESSION="${META[2]}"
LOG_DIR="$REPO/.agent-supervisor/logs"

echo "Stopping project:"
echo "  repo:    $REPO"
echo "  config:  $CONFIG"
echo "  session: $SESSION"

tmux kill-session -t "$SESSION" 2>/dev/null || true

for port in 3284 3285 3286 3288 3290 3310 3312 3314; do
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
done

pkill -f "$REPO/.agent-supervisor/logs/cycle-" >/dev/null 2>&1 || true
pkill -f "$REPO/.agent-supervisor/scripts/check.py" >/dev/null 2>&1 || true
sudo -n -u "$BURST_USER" pkill -f "claude --dangerously" >/dev/null 2>&1 || true
sudo -n -u "$BURST_USER" pkill -f "codex exec" >/dev/null 2>&1 || true
sudo -n -u "$BURST_USER" pkill -f "gemini.*yolo" >/dev/null 2>&1 || true

rm -f "$REPO/.agent-supervisor/pause" "$REPO/.agent-supervisor/restart" 2>/dev/null || true

sleep 2

remaining=$(ps ax -o pid= -o command= | grep -F "$REPO" | grep -v grep | wc -l | tr -d ' ')
if [ "$remaining" -gt 0 ]; then
    echo "WARNING: $remaining repo-scoped processes still running"
    ps ax -o pid= -o command= | grep -F "$REPO" | grep -v grep || true
else
    echo "All project processes stopped."
fi
