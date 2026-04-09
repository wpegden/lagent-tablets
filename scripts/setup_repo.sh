#!/bin/bash
# Setup a new formalization repo for lagent-tablets.
#
# Usage: ./scripts/setup_repo.sh <repo_path> <paper_tex_path>
#
# Creates the directory structure, copies the paper, initializes the Lean project,
# and sets up git. The burst_user (lagentworker) must be in the leanagent group.

set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <repo_path> <paper_tex_path>"
    echo "  repo_path:     where to create the formalization repo"
    echo "  paper_tex_path: path to the source paper .tex file"
    exit 1
fi

REPO="$1"
PAPER="$2"
BURST_USER="${BURST_USER:-lagentworker}"
BURST_GROUP="${BURST_GROUP:-leanagent}"
MATHLIB_TOOLCHAIN="${MATHLIB_TOOLCHAIN:-leanprover/lean4:v4.17.0}"

if [ ! -f "$PAPER" ]; then
    echo "ERROR: Paper not found: $PAPER"
    exit 1
fi

PAPER_NAME=$(basename "$PAPER")

echo "Setting up repo at: $REPO"
echo "  Paper: $PAPER ($PAPER_NAME)"
echo "  Burst user: $BURST_USER"
echo "  Burst group: $BURST_GROUP"

# Create directory structure
mkdir -p "$REPO/paper"
mkdir -p "$REPO/Tablet"
mkdir -p "$REPO/.agent-supervisor/logs"
mkdir -p "$REPO/.agent-supervisor/scripts"
mkdir -p "$REPO/.agent-supervisor/skills"
mkdir -p "$REPO/.agent-supervisor/checkpoints"

# Copy paper
cp "$PAPER" "$REPO/paper/$PAPER_NAME"
echo "  Copied paper to $REPO/paper/$PAPER_NAME"

# Create lakefile if it doesn't exist
if [ ! -f "$REPO/lakefile.lean" ]; then
    cat > "$REPO/lakefile.lean" << 'LAKEFILE'
import Lake
open Lake DSL

package «tablet» where
  leanOptions := #[
    ⟨`autoImplicit, false⟩
  ]

@[default_target]
lean_lib «Tablet» where
  srcDir := "."

require mathlib from git
  "https://github.com/leanprover-community/mathlib4" @ "master"
LAKEFILE
    echo "  Created lakefile.lean"
fi

# Create lean-toolchain if it doesn't exist
if [ ! -f "$REPO/lean-toolchain" ]; then
    echo "$MATHLIB_TOOLCHAIN" > "$REPO/lean-toolchain"
    echo "  Created lean-toolchain ($MATHLIB_TOOLCHAIN)"
fi

# Create Tablet/Preamble.lean if it doesn't exist
if [ ! -f "$REPO/Tablet/Preamble.lean" ]; then
    cat > "$REPO/Tablet/Preamble.lean" << 'PREAMBLE'
-- Preamble: shared imports for all tablet nodes.
-- Add specific Mathlib imports here (never `import Mathlib`).
PREAMBLE
    echo "  Created Tablet/Preamble.lean"
fi

# Create APPROVED_AXIOMS.json if it doesn't exist
if [ ! -f "$REPO/APPROVED_AXIOMS.json" ]; then
    cat > "$REPO/APPROVED_AXIOMS.json" << 'AXIOMS'
{
  "global": [],
  "nodes": {}
}
AXIOMS
    echo "  Created APPROVED_AXIOMS.json"
fi

# Set group ownership and permissions
chgrp -R "$BURST_GROUP" "$REPO" 2>/dev/null || true
chmod -R g+rw "$REPO" 2>/dev/null || true
find "$REPO" -type d -exec chmod g+s {} \; 2>/dev/null || true

# Initialize git
if [ ! -d "$REPO/.git" ]; then
    git -C "$REPO" init
    git -C "$REPO" config user.name "lagent-supervisor"
    git -C "$REPO" config user.email "lagent@localhost"
fi

# Create .gitignore
cat > "$REPO/.gitignore" << 'GITIGNORE'
.lake/
lake-packages/
*.olean
*.ilean
*.trace
.agent-supervisor/logs/
.agent-supervisor/checkpoints/
.agent-supervisor/nl_cache.json*
.agent-supervisor/policy.json*
.agent-supervisor/pause
# Signal files (ephemeral)
human_approve.json
human_feedback.json
# Note: correspondence_result*.json, nl_proof_result*.json, reviewer_decision.json,
# and worker_handoff.json are TRACKED in git for complete history.
__pycache__/
*.pyc
node_modules/
GITIGNORE
echo "  Created .gitignore"

# Initial commit
git -C "$REPO" add -A
git -C "$REPO" commit -m "Initial repo setup with paper and Lean project" 2>/dev/null || echo "  (nothing to commit)"

# Fetch mathlib (this takes a while on first run)
if [ ! -d "$REPO/.lake" ]; then
    echo "  Running lake update (fetching mathlib — this may take a few minutes)..."
    (cd "$REPO" && lake update 2>&1 | tail -3) || echo "  WARNING: lake update failed. Run manually."
fi

echo ""
echo "Done. Next steps:"
echo "  1. Create a config JSON pointing to $REPO"
echo "  2. Run: python -m lagent_tablets.cli --config <config.json>"
echo "  3. Or dry-run first: python -m lagent_tablets.cli --config <config.json> --dry-run"
