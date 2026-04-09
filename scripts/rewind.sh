#!/bin/bash
# Rewind to a specific cycle and stage, cleaning all artifacts.
# Usage: ./scripts/rewind.sh <cycle> [stage] [repo_path] [config]
#   stage: verification (default), reviewer, or worker
#
# Example: ./scripts/rewind.sh 1 verification
#          ./scripts/rewind.sh 3 reviewer /path/to/repo

set -euo pipefail

CYCLE="${1:?Usage: rewind.sh <cycle> [stage] [repo_path]}"
STAGE="${2:-verification}"
REPO="${3:-/home/leanagent/math/extremal_vectors_tablets}"
BURST_USER="${BURST_USER:-lagentworker}"

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

# 3. Clean signal files
echo "3. Cleaning signal files..."
rm -f "$REPO"/correspondence_result*.json \
      "$REPO"/nl_proof_result*.json \
      "$REPO"/reviewer_decision.json \
      "$REPO"/worker_handoff.json \
      "$REPO/.agent-supervisor/nl_cache.json"* \
      "$REPO/.agent-supervisor/pause"

# 4. Clean agent sessions (prevent context poisoning)
echo "4. Clearing agent sessions..."
# Find the project slug
SLUG=$(echo "$REPO" | sed 's|/|-|g; s|^-||')
sudo -n -u "$BURST_USER" rm -rf "/home/$BURST_USER/.claude/projects/$SLUG/" 2>/dev/null || true

# 5. Reset state
echo "5. Resetting state..."
python3 -c "
import json
p = '$REPO/.agent-supervisor/state.json'
s = json.load(open(p))
s['cycle'] = $CYCLE
s['resume_from'] = '$STAGE' if '$STAGE' != 'worker' else ''
s['last_review'] = None
s['review_log'] = []
s['agent_token_usage'] = {}
json.dump(s, open(p, 'w'), indent=2)
print(f'  cycle={s[\"cycle\"]} phase={s[\"phase\"]} resume={s.get(\"resume_from\",\"\")}')
"

# 6. Clear verification status on tablet
echo "6. Clearing verification status..."
python3 -c "
import json
p = '$REPO/.agent-supervisor/tablet.json'
t = json.load(open(p))
nodes = 0
for name, node in t.get('nodes', {}).items():
    if name == 'Preamble': continue
    nodes += 1
    for k in ['correspondence_status', 'soundness_status', 'verification_at_cycle', 'verification_content_hash']:
        node.pop(k, None)
json.dump(t, open(p, 'w'), indent=2)
print(f'  {nodes} nodes, verification status cleared')
"

# 7. Clear stale viewer cache
echo "7. Clearing viewer cache..."
rm -f /home/leanagent/lagent-tablets-web/api/state-at/*.json

echo ""
echo "Done. To resume:"
if [ "$STAGE" = "worker" ]; then
    echo "  tmux new-session -d -s supervisor \"python3 -u -m lagent_tablets.cli --config CONFIG 2>&1 | tee /tmp/run.log\""
else
    echo "  tmux new-session -d -s supervisor \"python3 -u -m lagent_tablets.cli --config CONFIG --resume-from $STAGE 2>&1 | tee /tmp/run.log\""
fi
