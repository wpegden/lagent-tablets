#!/bin/bash
# Pause the supervisor after the current cycle completes.
# Usage: ./scripts/pause.sh [repo_path]
REPO="${1:-/home/leanagent/math/extremal_vectors_tablets}"
echo '{}' > "$REPO/.agent-supervisor/pause"
echo "Pause signal written. Supervisor will stop after current cycle."
