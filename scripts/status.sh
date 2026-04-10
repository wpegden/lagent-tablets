#!/bin/bash
# Check status of all agents and the supervisor.
# Usage: ./scripts/status.sh [repo_path]
REPO="${1:-/home/leanagent/math/extremal_vectors_tablets}"

echo "=== Supervisor ==="
SUPERVISOR_RUNNING=0
if tmux has-session -t supervisor 2>/dev/null; then
    SUPERVISOR_RUNNING=1
    echo "  tmux: running"
    LOG=$(ls -t /tmp/lagent_run_*.log /tmp/extremal_vectors_run*.log 2>/dev/null | head -1)
    [ -n "$LOG" ] && echo "  latest log: $LOG"
else
    echo "  tmux: stopped"
fi

echo ""
echo "=== State ==="
STATE_INFO=$(python3 -c "
import json
from pathlib import Path
s = json.load(open('$REPO/.agent-supervisor/state.json'))
t = json.load(open('$REPO/.agent-supervisor/tablet.json'))
nodes = [n for n in t['nodes'] if n != 'Preamble']
closed = [n for n in nodes if t['nodes'][n].get('status') == 'closed']
print(f'  cycle={s[\"cycle\"]} phase={s[\"phase\"]} resume={s.get(\"resume_from\",\"\")}')
print(f'  tablet: {len(closed)}/{len(nodes)} closed')
" 2>/dev/null)
echo "$STATE_INFO"

LATEST_DIR=$(find "$REPO/.agent-supervisor/logs" -maxdepth 1 -type d -name 'cycle-*' 2>/dev/null | sed 's#.*/cycle-##' | sort -n | tail -1)
STATE_CYCLE=$(python3 -c "import json; print(json.load(open('$REPO/.agent-supervisor/state.json')).get('cycle', 0))" 2>/dev/null)
if [ "$SUPERVISOR_RUNNING" -eq 1 ] && [ -n "$LATEST_DIR" ] && [ -n "$STATE_CYCLE" ] && [ "$LATEST_DIR" -gt "$STATE_CYCLE" ] 2>/dev/null; then
    echo "  in-flight artifacts: cycle=$((10#$LATEST_DIR))"
fi

echo ""
echo "=== Agents ==="
for port in 3284 3285 3286 3288 3290; do
    status=$(curl -s http://localhost:$port/status 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d.get(\"agent_type\",\"?\")}: {d.get(\"status\",\"?\")}')" 2>/dev/null)
    [ -n "$status" ] && echo "  port $port — $status"
done
for port in 3310 3312 3314; do
    status=$(curl -s http://localhost:$port/status 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d.get(\"agent_type\",\"?\")}: {d.get(\"status\",\"?\")}')" 2>/dev/null)
    [ -n "$status" ] && echo "  port $port — $status (soundness)"
done
codex=$(ps aux | grep "codex exec" | grep -v grep | wc -l)
[ "$codex" -gt 0 ] && echo "  codex headless: $codex process(es)"

echo ""
echo "=== Root Result Files ==="
echo "  note: these files are from the most recent committed/reviewed cycle and may lag an in-flight worker cycle"
for i in 0 1 2; do
    for check in correspondence nl_proof; do
        f="$REPO/${check}_result_${i}.json"
        [ -f "$f" ] && echo "  ${check}_${i}: $(python3 -c "import json; print(json.load(open('$f')).get('overall','?'))" 2>/dev/null)"
    done
done
f="$REPO/reviewer_decision.json"
[ -f "$f" ] && echo "  reviewer: $(python3 -c "import json; print(json.load(open('$f')).get('decision','?'))" 2>/dev/null)"

echo ""
echo "=== Thoughts ==="
for port in 3286 3288; do
    name=$(curl -s http://localhost:$port/status 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_type','?'))" 2>/dev/null)
    [ -z "$name" ] && continue
    echo "  $name ($port):"
    curl -s "http://localhost:$port/internal/screen" -H "Accept: text/event-stream" --max-time 3 2>/dev/null | grep "^data:" | head -1 | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line[5:])
    lines = [l.strip() for l in d.get('screen','').split('\n') if l.strip() and len(l.strip()) > 5 and l.strip() != '│']
    for l in lines[-3:]: print(f'    {l[:100]}')
" 2>/dev/null
done
