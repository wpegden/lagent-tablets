#!/bin/bash
# Setup or reseed a formalization repo for lagent-tablets.
#
# Usage:
#   ./scripts/setup_repo.sh [--reset] <repo_path> <paper_tex_path> [project_slug]

set -euo pipefail
umask 0002

usage() {
  cat <<'EOF'
Usage: ./scripts/setup_repo.sh [--reset] <repo_path> <paper_tex_path> [project_slug]

  --reset         Stop any existing project process and recreate the repo from scratch
  repo_path       Where to create the formalization repo
  paper_tex_path  Path to the source paper .tex file
  project_slug    Optional viewer/session slug (defaults to basename(repo_path))
EOF
}

RESET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset|--force)
      RESET=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [ $# -lt 2 ]; then
  usage >&2
  exit 1
fi

REPO="$1"
PAPER="$2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_SLUG="$(basename "$REPO" | sed -E 's/_tablets?$//')"
PROJECT_SLUG="${3:-${PROJECT_SLUG:-$DEFAULT_SLUG}}"
CONFIG_TEMPLATE="${CONFIG_TEMPLATE:-$SOURCE_ROOT/configs/extremal_vectors_run.json}"
POLICY_TEMPLATE="${POLICY_TEMPLATE:-${CONFIG_TEMPLATE%.json}.policy.json}"
CONFIG_OUT="$REPO/lagent.config.json"
POLICY_OUT="$REPO/lagent.policy.json"
STATIC_OUT="${STATIC_OUT:-/home/leanagent/lagent-tablets-web}"
PROJECT_STATIC_DIR="$STATIC_OUT/$PROJECT_SLUG"
BURST_USER="${BURST_USER:-lagentworker}"
BURST_GROUP="${BURST_GROUP:-leanagent}"
MATHLIB_TOOLCHAIN="${MATHLIB_TOOLCHAIN:-leanprover/lean4:v4.17.0}"
BURST_HOME="${BURST_HOME:-/home/$BURST_USER}"
ELAN_HOME="${ELAN_HOME:-/home/leanagent/.elan}"
NODE_BIN="${NODE_BIN:-/home/leanagent/.nvm/versions/node/v22.22.2/bin}"
BURST_PATH="${BURST_PATH:-/home/leanagent/.local/bin:$ELAN_HOME/bin:$NODE_BIN:/usr/local/bin:/usr/bin:/bin}"

if [ ! -f "$PAPER" ]; then
  echo "ERROR: Paper not found: $PAPER" >&2
  exit 1
fi
if [ ! -f "$CONFIG_TEMPLATE" ]; then
  echo "ERROR: Config template not found: $CONFIG_TEMPLATE" >&2
  exit 1
fi

PAPER_NAME="$(basename "$PAPER")"

echo "Setting up repo at: $REPO"
echo "  Paper: $PAPER ($PAPER_NAME)"
echo "  Project slug: $PROJECT_SLUG"
echo "  Burst user: $BURST_USER"
echo "  Burst group: $BURST_GROUP"
echo "  Config out: $CONFIG_OUT"

if ! sudo -n -u "$BURST_USER" true >/dev/null 2>&1; then
  echo "ERROR: passwordless sudo to $BURST_USER is required for setup and runtime bursts." >&2
  echo "       Expected: sudo -n -u $BURST_USER true" >&2
  exit 1
fi

if [ "$RESET" -eq 1 ]; then
  echo "  Resetting existing project artifacts..."
  "$SCRIPT_DIR/stop.sh" "$REPO" >/dev/null 2>&1 || true
  tmux kill-session -t "$PROJECT_SLUG" >/dev/null 2>&1 || true
  rm -rf "$REPO"
  rm -rf "$PROJECT_STATIC_DIR"
fi

if [ -e "$REPO" ]; then
  echo "ERROR: Repo path already exists. Re-run with --reset to recreate it." >&2
  exit 1
fi

mkdir -p "$REPO/paper" "$REPO/Tablet"
mkdir -p "$REPO/.agent-supervisor/logs" "$REPO/.agent-supervisor/scripts" "$REPO/.agent-supervisor/checkpoints"
mkdir -p "$REPO/.agent-supervisor/staging" "$REPO/.agent-supervisor/viewer/state-at" "$REPO/.agent-supervisor/chats" "$REPO/.agent-supervisor/scratch"

cp "$PAPER" "$REPO/paper/$PAPER_NAME"
echo "  Copied paper to $REPO/paper/$PAPER_NAME"

cat > "$REPO/lakefile.lean" <<'LAKEFILE'
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
echo "  Wrote lakefile.lean"

echo "$MATHLIB_TOOLCHAIN" > "$REPO/lean-toolchain"
echo "  Wrote lean-toolchain ($MATHLIB_TOOLCHAIN)"

cat > "$REPO/Tablet/Preamble.lean" <<'PREAMBLE'
-- Preamble: shared imports for all tablet nodes.
-- Add specific Mathlib imports here (never `import Mathlib`).
PREAMBLE
echo "  Wrote Tablet/Preamble.lean"

cat > "$REPO/APPROVED_AXIOMS.json" <<'AXIOMS'
{
  "global": [],
  "nodes": {}
}
AXIOMS
echo "  Wrote APPROVED_AXIOMS.json"

cat > "$REPO/HUMAN_INPUT.md" <<'HUMAN'
# Human Input

Write human guidance for the supervisor here when requested.
HUMAN

cat > "$REPO/INPUT_REQUEST.md" <<'REQUEST'
# Input Request

The supervisor will write explicit requests for human input here when needed.
REQUEST

PYTHONPATH="$SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$REPO" "$CONFIG_TEMPLATE" "$CONFIG_OUT" "$POLICY_TEMPLATE" "$POLICY_OUT" "$PAPER_NAME" "$PROJECT_SLUG" <<'PY'
import json
import shutil
import sys
from pathlib import Path

from lagent_tablets.check import write_scripts
from lagent_tablets.config import FORBIDDEN_KEYWORDS_DEFAULT, load_config
from lagent_tablets.git_ops import init_repo
from lagent_tablets.project_paths import project_chats_dir, project_scratch_dir
from lagent_tablets.state import SupervisorState, TabletState, save_state, save_tablet, state_path, tablet_path
from lagent_tablets.tablet import regenerate_support_files
from lagent_tablets.viewer_state import viewer_state_path, write_live_viewer_state

repo = Path(sys.argv[1]).resolve()
config_template = Path(sys.argv[2]).resolve()
config_out = Path(sys.argv[3]).resolve()
policy_template = Path(sys.argv[4]).resolve()
policy_out = Path(sys.argv[5]).resolve()
paper_name = sys.argv[6]
slug = sys.argv[7]
state_dir = repo / ".agent-supervisor"

init_repo(repo)
save_state(state_path(state_dir), SupervisorState(cycle=0, phase="theorem_stating"))
save_tablet(tablet_path(state_dir), TabletState())
regenerate_support_files(TabletState(), repo)

parsed = json.loads(config_template.read_text(encoding="utf-8"))
if not isinstance(parsed, dict):
    raise SystemExit("Config template must be a JSON object")
data = parsed
data["repo_path"] = str(repo)
data["state_dir"] = ".agent-supervisor"
data["policy_path"] = "lagent.policy.json"

tmux = data.setdefault("tmux", {})
tmux["session_name"] = slug

workflow = data.setdefault("workflow", {})
workflow["paper_tex_path"] = f"paper/{paper_name}"
workflow["approved_axioms_path"] = "APPROVED_AXIOMS.json"
workflow["human_input_path"] = "HUMAN_INPUT.md"
workflow["input_request_path"] = "INPUT_REQUEST.md"

chat = data.setdefault("chat", {})
chat["root_dir"] = str(project_chats_dir(state_dir))
chat["repo_name"] = slug
chat["project_name"] = slug.replace("_", " ").title() + " Formalization"

git_cfg = data.setdefault("git", {})
git_cfg.setdefault("remote_url", None)
git_cfg.setdefault("remote_name", "origin")
git_cfg.setdefault("branch", "master")
git_cfg.setdefault("author_name", "lagent-supervisor")
git_cfg.setdefault("author_email", "lagent@localhost")

config_out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

if policy_template.exists():
    shutil.copyfile(policy_template, policy_out)

config = load_config(config_out)
forbidden = [
    kw for kw in FORBIDDEN_KEYWORDS_DEFAULT
    if kw not in config.workflow.forbidden_keyword_allowlist
]
write_scripts(
    config.repo_path,
    config.state_dir,
    allowed_prefixes=config.workflow.allowed_import_prefixes,
    forbidden_keywords=forbidden,
)

write_live_viewer_state(
    viewer_state_path(state_dir),
    repo,
    TabletState(),
    SupervisorState(cycle=0, phase="theorem_stating"),
    source="setup",
    fast=True,
)

scratch_dir = project_scratch_dir(state_dir)
scratch_dir.mkdir(parents=True, exist_ok=True)
readme_path = scratch_dir / "README.md"
if not readme_path.exists():
    readme_path.write_text(
        "# Worker Scratchpad\n\n"
        "Use this directory for repo-local Lean experiments and temporary notes.\n"
        "Files here are intentionally excluded from the main formalization history.\n",
        encoding="utf-8",
    )
example_path = scratch_dir / "example.lean"
example_path.write_text(
    "import Tablet.Preamble\n"
    "import Mathlib.Data.Nat.Basic\n\n"
    "-- Example scratch file: project-local, ignored by the main repo, and buildable with\n"
    "--   lake env lean .agent-supervisor/scratch/example.lean\n"
    "#check Nat.succ\n"
    "example : Nat.succ 0 = 1 := rfl\n",
    encoding="utf-8",
)
PY
echo "  Initialized state, config, scripts, and viewer payload"

python3 - "$REPO" "$BURST_GROUP" <<'PY'
import grp
import os
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
group = sys.argv[2]
gid = grp.getgrnam(group).gr_gid

def chmod_dir(path: Path, mode: int) -> None:
    try:
        os.chown(str(path), -1, gid)
    except (PermissionError, OSError):
        pass
    try:
        os.chmod(str(path), mode)
    except (PermissionError, OSError):
        pass

def chmod_file(path: Path, mode: int) -> None:
    try:
        os.chown(str(path), -1, gid)
    except (PermissionError, OSError):
        pass
    try:
        os.chmod(str(path), mode)
    except (PermissionError, OSError):
        pass

skip_dirs = {'.git'}
for root, dirs, files in os.walk(repo):
    root_path = Path(root)
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    chmod_dir(root_path, 0o2775)
    for name in files:
        path = root_path / name
        mode = 0o664
        if path.parent.name == 'scripts' or path.suffix == '.sh':
            mode = 0o775
        chmod_file(path, mode)
PY
echo "  Normalized working-tree permissions for shared use"

git -C "$REPO" config core.sharedRepository group

echo "  Initializing nested local chats git repo..."
git -C "$REPO/.agent-supervisor/chats" init >/dev/null 2>&1
git -C "$REPO/.agent-supervisor/chats" config user.name "lagent-chats" >/dev/null 2>&1
git -C "$REPO/.agent-supervisor/chats" config user.email "lagent-chats@localhost" >/dev/null 2>&1
cat > "$REPO/.agent-supervisor/chats/README.md" <<'CHATREADME'
# Local Chat History

This nested git repo stores project-local chat/session history.
It is intentionally outside the parent formalization repo history.
CHATREADME
git -C "$REPO/.agent-supervisor/chats" add README.md >/dev/null 2>&1
git -C "$REPO/.agent-supervisor/chats" commit -m "Initialize local chat history repo" >/dev/null 2>&1 || true

echo "  Prewarming Lean dependencies and build artifacts as $BURST_USER..."
sudo -n -u "$BURST_USER" env \
  HOME="$BURST_HOME" \
  ELAN_HOME="$ELAN_HOME" \
  PATH="$BURST_PATH" \
  bash -lc "
    set -euo pipefail
    umask 0002
    git config --global --add safe.directory '$REPO' >/dev/null 2>&1 || true
    cd '$REPO'
    lake update
    lake exe cache get >/dev/null 2>&1 || true
    lake build Tablet.Preamble
    lake build Tablet
    python3 .agent-supervisor/scripts/check.py tablet '$REPO'
  "

PYTHONPATH="$SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$REPO" "$BURST_USER" <<'PY'
import sys
from pathlib import Path

from lagent_tablets.health import fix_lake_permissions

repo = Path(sys.argv[1]).resolve()
burst_user = sys.argv[2]
fix_lake_permissions(repo, burst_user=burst_user, include_package_builds=True)
PY
echo "  Fixed shared Lean build permissions for supervisor access"

echo "  Validating supervisor-side deterministic checks..."
python3 "$REPO/.agent-supervisor/scripts/check.py" tablet "$REPO"

mkdir -p "$PROJECT_STATIC_DIR"
ln -sfn "$REPO/.agent-supervisor/viewer" "$PROJECT_STATIC_DIR/api"
ln -sfn "$SOURCE_ROOT/viewer/public/index.html" "$PROJECT_STATIC_DIR/index.html"
echo "  Linked project viewer route to repo-local viewer data"

find "$REPO/.agent-supervisor" -name '*.lock' -delete 2>/dev/null || true
git -C "$REPO" add -A
git -C "$REPO" commit -m "Initial repo setup with paper and Lean project" >/dev/null 2>&1 || true

echo ""
echo "Setup complete."
echo "  Repo:          $REPO"
echo "  Config:        $CONFIG_OUT"
echo "  Viewer route:  /lagent-tablets/$PROJECT_SLUG/"
echo "  Verified with worker-side and supervisor-side tablet checks."
