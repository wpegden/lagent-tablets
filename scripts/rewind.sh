#!/bin/bash
# Rewind to an exact committed checkpoint tag and clean the worktree.
# Usage: ./scripts/rewind.sh <cycle> [stage] [repo_path]
#   stage: worker | verification | reviewer   (default: reviewer)
#
# This script no longer edits state.json after git reset. Rewinds are exact:
# it restores the committed checkpoint ref and runs git clean so the worktree
# matches that ref perfectly.

set -euo pipefail

CYCLE="${1:?Usage: rewind.sh <cycle> [stage] [repo_path]}"
STAGE="${2:-reviewer}"
REPO="${3:-/home/leanagent/math/extremal_vectors_tablets}"
BURST_USER="${BURST_USER:-lagentworker}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "$STAGE" != "worker" && "$STAGE" != "verification" && "$STAGE" != "reviewer" ]]; then
    echo "ERROR: stage must be 'worker', 'verification', or 'reviewer' (got '$STAGE')"
    exit 1
fi

echo "Rewinding to committed checkpoint:"
echo "  repo:  $REPO"
echo "  cycle: $CYCLE"
echo "  stage: $STAGE"

PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
REPO_ENV="$REPO" \
CYCLE_ENV="$CYCLE" \
STAGE_ENV="$STAGE" \
BURST_USER_ENV="$BURST_USER" \
python3 - <<'PY'
import os
import sys
from pathlib import Path

from lagent_tablets.git_ops import rewind_to_cycle

repo = Path(os.environ["REPO_ENV"]).resolve()
cycle = int(os.environ["CYCLE_ENV"])
stage = os.environ["STAGE_ENV"]
burst_user = os.environ["BURST_USER_ENV"]

ok = rewind_to_cycle(repo, cycle, stage=stage, burst_user=burst_user)
raise SystemExit(0 if ok else 1)
PY

echo ""
echo "Done. Restart normally from the committed state:"
echo "  ./scripts/resume.sh /path/to/repo"
