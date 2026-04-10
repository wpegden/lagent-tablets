#!/bin/bash
# Check status of one project supervisor and agents.
# Usage: ./scripts/status.sh [repo_path_or_config]

set -euo pipefail

TARGET="${1:-/home/leanagent/math/extremal}"

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
repo = target if target.is_dir() else Path(".")
session = re.sub(r"[^A-Za-z0-9_]+", "_", repo.name).strip("_") or "lagent_tablets"
if config_path.exists():
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        repo = Path(raw.get("repo_path", repo)).resolve()
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

echo "=== Project ==="
echo "  repo:    $REPO"
echo "  config:  $CONFIG"
echo "  session: $SESSION"

echo ""
echo "=== Supervisor ==="
SUPERVISOR_RUNNING=0
if tmux has-session -t "$SESSION" 2>/dev/null; then
    SUPERVISOR_RUNNING=1
    echo "  tmux: running"
    LOG=$(ls -t "$LOG_DIR"/supervisor_*.log 2>/dev/null | head -1 || true)
    [ -n "$LOG" ] && echo "  latest log: $LOG"
else
    echo "  tmux: stopped"
fi

echo ""
echo "=== State ==="
STATE_INFO=$(python3 -c "
import json
from pathlib import Path
state = Path('$REPO/.agent-supervisor/state.json')
tablet = Path('$REPO/.agent-supervisor/tablet.json')
if state.exists() and tablet.exists():
    s = json.load(open(state))
    t = json.load(open(tablet))
    nodes = [n for n in t['nodes'] if n != 'Preamble']
    closed = [n for n in nodes if t['nodes'][n].get('status') == 'closed']
    print(f'  cycle={s[\"cycle\"]} phase={s[\"phase\"]} resume={s.get(\"resume_from\",\"\")}')
    print(f'  tablet: {len(closed)}/{len(nodes)} closed')
" 2>/dev/null)
echo "$STATE_INFO"

LATEST_DIR=$(find "$REPO/.agent-supervisor/logs" -maxdepth 1 -type d -name 'cycle-*' 2>/dev/null | sed 's#.*/cycle-##' | sort -n | tail -1 || true)
STATE_CYCLE=$(python3 -c "import json; print(json.load(open('$REPO/.agent-supervisor/state.json')).get('cycle', 0))" 2>/dev/null || true)
if [ "$SUPERVISOR_RUNNING" -eq 1 ] && [ -n "$LATEST_DIR" ] && [ -n "$STATE_CYCLE" ] && [ "$LATEST_DIR" -gt "$STATE_CYCLE" ] 2>/dev/null; then
    echo "  in-flight artifacts: cycle=$((10#$LATEST_DIR))"
fi

echo ""
echo "=== Agents ==="
for port in 3284 3285 3286 3288 3290; do
    status=$(curl -s http://localhost:$port/status 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d.get(\"agent_type\",\"?\")}: {d.get(\"status\",\"?\")}')" 2>/dev/null || true)
    [ -n "$status" ] && echo "  port $port — $status"
done
for port in 3310 3312 3314; do
    status=$(curl -s http://localhost:$port/status 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d.get(\"agent_type\",\"?\")}: {d.get(\"status\",\"?\")}')" 2>/dev/null || true)
    [ -n "$status" ] && echo "  port $port — $status (soundness)"
done
codex=$(ps aux | grep "codex exec" | grep -v grep | wc -l | tr -d ' ')
[ "$codex" -gt 0 ] && echo "  codex headless: $codex process(es)"
