#!/bin/bash
# Force stop the supervisor and all agent processes.
# Usage: ./scripts/stop.sh [repo_path]
REPO="${1:-/home/leanagent/math/extremal_vectors_tablets}"
BURST_USER="${BURST_USER:-lagentworker}"

echo "Stopping supervisor..."
tmux kill-session -t supervisor 2>/dev/null

echo "Killing agentapi servers..."
pgrep -f "agentapi server" | xargs -r kill 2>/dev/null

echo "Killing agent processes..."
sudo -n -u "$BURST_USER" pkill -f "claude --dangerously" 2>/dev/null
sudo -n -u "$BURST_USER" pkill -f "codex exec" 2>/dev/null
sudo -n -u "$BURST_USER" pkill -f "gemini.*yolo" 2>/dev/null

sleep 2
rm -f "$REPO/.agent-supervisor/pause"

remaining=$(ps aux | grep "agentapi\|codex exec\|claude --danger\|gemini.*yolo" | grep -v grep | wc -l)
if [ "$remaining" -gt 0 ]; then
    echo "WARNING: $remaining processes still running"
    ps aux | grep "agentapi\|codex exec\|claude --danger\|gemini.*yolo" | grep -v grep
else
    echo "All processes stopped."
fi
