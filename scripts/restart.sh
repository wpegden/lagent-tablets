#!/bin/bash
# Restart the supervisor after the current cycle completes.
# Usage: ./scripts/restart.sh [repo_path]
REPO="${1:-/home/leanagent/math/extremal_vectors_tablets}"
echo '{}' > "$REPO/.agent-supervisor/restart"
echo "Restart signal written. Supervisor will restart after the current cycle."
