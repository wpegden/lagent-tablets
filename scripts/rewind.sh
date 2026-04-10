#!/bin/bash
# Rewind to a specific cycle and stage, cleaning all artifacts.
# Usage: ./scripts/rewind.sh <cycle> [stage] [repo_path]
# Optional env:
#   FRESH_THEOREM_TARGET=1  Clear the persisted theorem-stating soundness target
#                           so the resumed run selects a fresh deepest unresolved node.
#   stage: verification (default), reviewer, or worker
#
# Example: ./scripts/rewind.sh 1 verification
#          ./scripts/rewind.sh 3 reviewer /path/to/repo

set -euo pipefail

CYCLE="${1:?Usage: rewind.sh <cycle> [stage] [repo_path]}"
STAGE="${2:-verification}"
REPO="${3:-/home/leanagent/math/extremal_vectors_tablets}"
BURST_USER="${BURST_USER:-lagentworker}"
FRESH_THEOREM_TARGET="${FRESH_THEOREM_TARGET:-0}"

if [[ "$STAGE" != "verification" && "$STAGE" != "reviewer" && "$STAGE" != "worker" ]]; then
    echo "ERROR: stage must be 'verification', 'reviewer', or 'worker' (got '$STAGE')"
    exit 1
fi

TAG="cycle-${CYCLE}"
if ! git -C "$REPO" rev-parse "$TAG" >/dev/null 2>&1; then
    echo "ERROR: tag $TAG not found in repo"
    echo "Available tags:"
    git -C "$REPO" tag -l 'cycle-*'
    exit 1
fi

echo "Rewinding to $TAG, resume from $STAGE"
echo "  repo: $REPO"

# 1. Stop everything
echo "1. Stopping processes..."
tmux kill-session -t supervisor 2>/dev/null || true
pgrep -f "agentapi server" | xargs -r kill 2>/dev/null || true
sudo -n -u "$BURST_USER" pkill -f "claude --dangerously" 2>/dev/null || true
sudo -n -u "$BURST_USER" pkill -f "codex exec" 2>/dev/null || true
sudo -n -u "$BURST_USER" pkill -f "gemini.*yolo" 2>/dev/null || true
sleep 2

# 2. Reset git to the cycle's commit
echo "2. Resetting git to $TAG..."
git -C "$REPO" reset --hard "$TAG"

# 2b. Drop future cycle tags for destructive rewind semantics.
echo "2b. Deleting future cycle tags..."
git -C "$REPO" tag -l 'cycle-*' | while read -r existing; do
    [ -n "$existing" ] || continue
    num=${existing#cycle-}
    if [[ "$num" =~ ^[0-9]+$ ]] && (( num > CYCLE )); then
        git -C "$REPO" tag -d "$existing" >/dev/null
        echo "  deleted tag $existing"
    fi
done

# 3. Clean ephemeral signal files (NOT result files — those are tracked in git)
echo "3. Cleaning ephemeral files..."
rm -f "$REPO/.agent-supervisor/nl_cache.json"* \
      "$REPO/.agent-supervisor/pause" \
      "$REPO/.agent-supervisor/restart" \
      "$REPO/.agent-supervisor/human_approve.json" \
      "$REPO/.agent-supervisor/human_feedback.json"
# Result files (correspondence_result_*.json, etc.) are preserved in git history.
# The git reset --hard already restores the correct versions for this cycle.
for f in "$REPO"/correspondence_result*.json \
         "$REPO"/reviewer_decision.json \
         "$REPO"/worker_handoff.json \
         "$REPO"/nl_proof*.json \
         "$REPO"/soundness_result*.json; do
    [ -e "$f" ] || continue
    rel=$(basename "$f")
    if ! git -C "$REPO" ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
        rm -f "$f"
    fi
done

# 3b. Clear future-cycle artifacts and stale runtime logs.
echo "3b. Clearing future artifacts..."
LOG_ROOT="$REPO/.agent-supervisor/logs"
if [ -d "$LOG_ROOT" ]; then
    find "$LOG_ROOT" -maxdepth 1 -mindepth 1 -type d -name 'cycle-*' | while read -r dir; do
        base=$(basename "$dir")
        num=${base#cycle-}
        if [[ "$num" =~ ^[0-9]+$ ]]; then
            if [ "$STAGE" = "worker" ]; then
                if (( 10#$num > CYCLE )); then
                    rm -rf "$dir"
                fi
            else
                if (( 10#$num >= CYCLE )); then
                    rm -rf "$dir"
                fi
            fi
        fi
    done
    rm -f "$LOG_ROOT"/agentapi-reviewer*.log \
          "$LOG_ROOT"/reviewer-transcript.json \
          "$LOG_ROOT"/health.jsonl \
          "$LOG_ROOT"/health.jsonl.lock
fi
if [ -d "$REPO/.agent-supervisor/checkpoints" ]; then
    rm -rf "$REPO/.agent-supervisor/checkpoints"/*
fi
if [ -d "$REPO/.agent-supervisor/staging" ]; then
    find "$REPO/.agent-supervisor/staging" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi

# 4. Clean agent sessions (prevent context poisoning)
echo "4. Clearing agent sessions..."
# Find the project slug
SLUG=$(echo "$REPO" | sed 's|/|-|g; s|^-||')
sudo -n -u "$BURST_USER" rm -rf "/home/$BURST_USER/.claude/projects/$SLUG/" 2>/dev/null || true

# 5. Reset state
echo "5. Resetting state..."
REPO_ENV="$REPO" CYCLE_ENV="$CYCLE" STAGE_ENV="$STAGE" python3 - <<'PY'
import json
import os
from pathlib import Path

from lagent_tablets.cycle import _reconcile_theorem_stating_open_rejections
from lagent_tablets.state import normalize_open_rejections

repo = Path(os.environ["REPO_ENV"])
cycle = int(os.environ["CYCLE_ENV"])
stage = os.environ["STAGE_ENV"]
fresh_theorem_target = os.environ.get("FRESH_THEOREM_TARGET", "0") == "1"

state_path = repo / ".agent-supervisor" / "state.json"
state = json.loads(state_path.read_text(encoding="utf-8"))

state["cycle"] = cycle
state["resume_from"] = stage if stage != "worker" else ""
state["agent_token_usage"] = {}
state["awaiting_human_input"] = False
if fresh_theorem_target and state.get("phase") == "theorem_stating":
    state["theorem_soundness_target"] = ""
    state["theorem_target_edit_mode"] = "repair"

review_log = state.get("review_log", [])
if isinstance(review_log, list):
    if stage == "worker":
        state["review_log"] = [entry for entry in review_log if isinstance(entry, dict) and int(entry.get("cycle", 0) or 0) <= cycle]
    else:
        state["review_log"] = [entry for entry in review_log if isinstance(entry, dict) and int(entry.get("cycle", 0) or 0) < cycle]
else:
    state["review_log"] = []

if stage != "worker":
    state["last_review"] = None
    state["open_rejections"] = []
else:
    if state.get("phase") == "theorem_stating":
        decision_path = repo / "reviewer_decision.json"
        decision = {}
        if decision_path.exists():
            try:
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
            except Exception:
                decision = {}

        preferred = []
        if isinstance(state.get("last_review"), dict):
            preferred = state["last_review"].get("open_rejections", [])
        if not preferred and isinstance(decision, dict):
            preferred = decision.get("open_rejections", [])

        agent_results = []
        for path in sorted(repo.glob("correspondence_result*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                data.setdefault("agent", path.stem)
                agent_results.append(data)

        if agent_results:
            open_rejections = _reconcile_theorem_stating_open_rejections(
                [{"check": "correspondence", "agent_results": agent_results}],
                preferred,
                include_preferred_extras=True,
            )
        else:
            open_rejections = normalize_open_rejections(preferred)

        state["open_rejections"] = open_rejections
        if isinstance(state.get("last_review"), dict):
            state["last_review"]["open_rejections"] = open_rejections
        if isinstance(decision, dict) and decision:
            decision["open_rejections"] = open_rejections
            decision_path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
        for entry in state["review_log"]:
            if isinstance(entry, dict) and int(entry.get("cycle", 0) or 0) == cycle:
                entry["open_rejections"] = open_rejections
    else:
        state["open_rejections"] = []

state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
print(f"  cycle={state['cycle']} phase={state['phase']} resume={state.get('resume_from','')}")
if fresh_theorem_target and state.get("phase") == "theorem_stating":
    print("  theorem_soundness_target cleared for fresh deepest-first replay")
if stage == "worker" and state.get("phase") == "theorem_stating":
    print(f"  open_rejections={len(state.get('open_rejections', []))}")
PY

# 6. Keep the committed tablet verification snapshot from the rewind target.
echo "6. Preserving committed verification status from target cycle..."
REPO_ENV="$REPO" python3 - <<'PY'
import json
import os
from pathlib import Path

repo = Path(os.environ["REPO_ENV"])
tablet = json.loads((repo / ".agent-supervisor" / "tablet.json").read_text(encoding="utf-8"))
nodes = [name for name in tablet.get("nodes", {}) if name != "Preamble"]
with_verification = sum(
    1 for name in nodes
    if any(k in tablet["nodes"][name] for k in ("correspondence_status", "soundness_status", "verification_at_cycle", "verification_content_hash"))
)
print(f"  {len(nodes)} nodes, verification metadata preserved on {with_verification}")
PY

# 7. Historical viewer snapshots are now sourced from git and legacy backfill.
# No generated history cache reset is required here.
echo "7. Viewer history snapshots preserved (git/backfill sourced)."

echo ""
echo "Done. To resume:"
if [ "$STAGE" = "worker" ]; then
    echo "  tmux new-session -d -s supervisor \"python3 -u -m lagent_tablets.cli --config CONFIG 2>&1 | tee /tmp/run.log\""
else
    echo "  tmux new-session -d -s supervisor \"python3 -u -m lagent_tablets.cli --config CONFIG --resume-from $STAGE 2>&1 | tee /tmp/run.log\""
fi
