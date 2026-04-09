#!/bin/bash
# Resume the supervisor from its current state.
# Usage: ./scripts/resume.sh <config> [extra_args...]
#
# Example: ./scripts/resume.sh configs/extremal_vectors_run.json --stop-at-phase-boundary
#          ./scripts/resume.sh configs/extremal_vectors_run.json --resume-from verification

set -euo pipefail

CONFIG="${1:?Usage: resume.sh <config> [extra_args...]}"
shift
EXTRA_ARGS="$*"

# Check no supervisor already running
if tmux has-session -t supervisor 2>/dev/null; then
    echo "ERROR: supervisor tmux session already exists. Stop it first with ./scripts/stop.sh"
    exit 1
fi

echo "Resuming with config: $CONFIG"
echo "  extra args: $EXTRA_ARGS"

tmux new-session -d -s supervisor "python3 -u -m lagent_tablets.cli --config $CONFIG $EXTRA_ARGS 2>&1 | tee /tmp/lagent_run_$(date +%Y%m%d_%H%M%S).log"

sleep 3
LOG=$(ls -t /tmp/lagent_run_*.log 2>/dev/null | head -1)
if [ -n "$LOG" ]; then
    echo "Log: $LOG"
    tail -5 "$LOG"
fi
