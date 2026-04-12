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
  --yes           Skip the target confirmation prompt
  --main-result-labels labels
                  Comma-separated paper TeX labels to use as the human-reviewed target set.
                  If omitted, setup infers all paper theorem/corollary statements, using labels
                  when present and line ranges when not.
  repo_path       Where to create the formalization repo
  paper_tex_path  Path to the source paper .tex file
  project_slug    Optional viewer/session slug (defaults to basename(repo_path))
EOF
}

RESET=0
ASSUME_YES=0
MAIN_RESULT_LABELS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset|--force)
      RESET=1
      shift
      ;;
    --yes|-y)
      ASSUME_YES=1
      shift
      ;;
    --main-result-labels)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --main-result-labels requires a comma-separated argument" >&2
        exit 1
      fi
      MAIN_RESULT_LABELS="$2"
      shift 2
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
if [ -e "$REPO" ] && [ "$RESET" -ne 1 ]; then
  echo "ERROR: Repo path already exists. Re-run with --reset to recreate it." >&2
  exit 1
fi

PAPER_NAME="$(basename "$PAPER")"
TARGETS_JSON="$(mktemp)"
TARGETS_PREVIEW="$(mktemp)"
cleanup_preview_files() {
  rm -f "$TARGETS_JSON" "$TARGETS_PREVIEW"
}
trap cleanup_preview_files EXIT

echo "Setting up repo at: $REPO"
echo "  Paper: $PAPER ($PAPER_NAME)"
echo "  Project slug: $PROJECT_SLUG"
echo "  Burst user: $BURST_USER"
echo "  Burst group: $BURST_GROUP"
echo "  Config out: $CONFIG_OUT"
if [[ -n "$MAIN_RESULT_LABELS" ]]; then
  echo "  Main-result labels: $MAIN_RESULT_LABELS"
fi

PYTHONPATH="$SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$PAPER" "$MAIN_RESULT_LABELS" "$TARGETS_JSON" "$TARGETS_PREVIEW" <<'PY'
import json
import sys
from pathlib import Path

from lagent_tablets.tablet import (
    extract_paper_statement_blocks,
    extract_paper_statement_labels,
    format_main_result_target,
    infer_main_result_targets_from_paper,
    main_result_target_key,
    resolve_main_result_targets,
)

paper_path = Path(sys.argv[1]).resolve()
raw_main_result_labels = sys.argv[2]
targets_json = Path(sys.argv[3]).resolve()
targets_preview = Path(sys.argv[4]).resolve()

if raw_main_result_labels.strip():
    labels = []
    seen = set()
    for raw_label in raw_main_result_labels.split(","):
        label = raw_label.strip()
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    available_labels = extract_paper_statement_labels(paper_path)
    missing = [label for label in labels if label not in available_labels]
    if missing:
        raise SystemExit(
            "Configured main_result_labels are not present as labeled paper statements: "
            + ", ".join(missing)
        )
    targets = resolve_main_result_targets(paper_path=paper_path, raw_labels=labels)
else:
    labels = []
    targets = infer_main_result_targets_from_paper(paper_path)

paper_text = paper_path.read_text(encoding="utf-8", errors="replace")
blocks = extract_paper_statement_blocks(paper_text)
preview_lines = ["Resolved main-result targets:"]

def match_block(target: dict) -> dict | None:
    label = str(target.get("tex_label", "") or "").strip()
    start_line = int(target.get("start_line", 0) or 0)
    end_line = int(target.get("end_line", 0) or 0)
    if label and start_line and end_line:
        for block in blocks:
            if (
                label in block.get("labels", [])
                and int(block.get("start_line", 0) or 0) == start_line
                and int(block.get("end_line", 0) or 0) == end_line
            ):
                return block
    if label:
        for block in blocks:
            if label in block.get("labels", []):
                return block
    if start_line and end_line:
        for block in blocks:
            if (
                int(block.get("start_line", 0) or 0) == start_line
                and int(block.get("end_line", 0) or 0) == end_line
            ):
                return block
    return None

if not targets:
    preview_lines.append("(none)")
else:
    for idx, target in enumerate(targets, start=1):
        block = match_block(target)
        if block is None:
            raise SystemExit(
                f"Could not locate paper text for resolved main-result target {format_main_result_target(target)}."
            )
        target_header = f"{idx}. {format_main_result_target(target)} [{block.get('env', '').strip()}]"
        preview_lines.append(target_header)
        preview_lines.append(str(block.get("text", "") or "").strip())
        preview_lines.append("")

targets_json.write_text(
    json.dumps({"labels": labels, "targets": targets}, indent=2) + "\n",
    encoding="utf-8",
)
targets_preview.write_text("\n".join(preview_lines).rstrip() + "\n", encoding="utf-8")
PY

echo ""
cat "$TARGETS_PREVIEW"
echo ""
if [ "$ASSUME_YES" -eq 0 ]; then
  if [ ! -t 0 ]; then
    echo "ERROR: setup requires target confirmation. Re-run with --yes in non-interactive mode." >&2
    exit 1
  fi
  read -r -p "Proceed with these targets? [y/N] " TARGET_CONFIRM
  case "$TARGET_CONFIRM" in
    y|Y|yes|YES)
      ;;
    *)
      echo "Aborted."
      exit 1
      ;;
  esac
fi

if ! sudo -n -u "$BURST_USER" true >/dev/null 2>&1; then
  echo "ERROR: passwordless sudo to $BURST_USER is required for setup and runtime bursts." >&2
  echo "       Expected: sudo -n -u $BURST_USER true" >&2
  exit 1
fi
if ! command -v bwrap >/dev/null 2>&1; then
  echo "ERROR: bwrap is required for sandboxed agent bursts." >&2
  exit 1
fi
if ! PYTHONPATH="$SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$REPO" "$BURST_USER" "$BURST_HOME" <<'PY'
import sys
from pathlib import Path

from lagent_tablets.config import SandboxConfig
from lagent_tablets.sandbox import probe_sandbox

repo = Path(sys.argv[1]).resolve()
burst_user = sys.argv[2]
burst_home = Path(sys.argv[3]).resolve()
ok, detail = probe_sandbox(
    sandbox=SandboxConfig(enabled=True, backend="bwrap"),
    work_dir=repo.parent if repo.exists() else Path.cwd(),
    burst_user=burst_user,
    burst_home=burst_home,
)
if not ok:
    print(detail, file=sys.stderr)
    raise SystemExit(1)
PY
then
  echo "ERROR: bwrap exists but is not usable for sandboxed bursts on this host." >&2
  exit 1
fi

if [ "$RESET" -eq 1 ]; then
  echo "  Resetting existing project artifacts..."
  "$SCRIPT_DIR/stop.sh" "$REPO" >/dev/null 2>&1 || true
  tmux kill-session -t "$PROJECT_SLUG" >/dev/null 2>&1 || true
  rm -rf "$REPO"
  rm -rf "$PROJECT_STATIC_DIR"
fi

mkdir -p "$REPO/paper" "$REPO/Tablet"
mkdir -p "$REPO/.agent-supervisor/logs" "$REPO/.agent-supervisor/scripts" "$REPO/.agent-supervisor/checkpoints"
mkdir -p "$REPO/.agent-supervisor/staging" "$REPO/.agent-supervisor/viewer/state-at" "$REPO/.agent-supervisor/chats" "$REPO/.agent-supervisor/scratch"
mkdir -p "$REPO/.agent-supervisor/runtime"

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

PYTHONPATH="$SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$REPO" "$CONFIG_TEMPLATE" "$CONFIG_OUT" "$POLICY_TEMPLATE" "$POLICY_OUT" "$PAPER_NAME" "$PROJECT_SLUG" "$TARGETS_JSON" <<'PY'
import json
import shutil
import sys
from pathlib import Path

from lagent_tablets.check import write_scripts
from lagent_tablets.config import FORBIDDEN_KEYWORDS_DEFAULT, load_config
from lagent_tablets.git_ops import init_repo
from lagent_tablets.project_paths import project_chats_dir, project_scratch_dir
from lagent_tablets.state import SupervisorState, TabletState, save_state, save_tablet, state_path, tablet_path
from lagent_tablets.tablet import (
    regenerate_support_files,
)
from lagent_tablets.viewer_state import viewer_state_path, write_live_viewer_state

repo = Path(sys.argv[1]).resolve()
config_template = Path(sys.argv[2]).resolve()
config_out = Path(sys.argv[3]).resolve()
policy_template = Path(sys.argv[4]).resolve()
policy_out = Path(sys.argv[5]).resolve()
paper_name = sys.argv[6]
slug = sys.argv[7]
resolved_targets_path = Path(sys.argv[8]).resolve()
state_dir = repo / ".agent-supervisor"
paper_path = repo / "paper" / paper_name

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

sandbox = data.setdefault("sandbox", {})
sandbox["enabled"] = True
sandbox["backend"] = "bwrap"

tmux = data.setdefault("tmux", {})
tmux["session_name"] = slug

workflow = data.setdefault("workflow", {})
workflow["paper_tex_path"] = f"paper/{paper_name}"
workflow["approved_axioms_path"] = "APPROVED_AXIOMS.json"
workflow["human_input_path"] = "HUMAN_INPUT.md"
workflow["input_request_path"] = "INPUT_REQUEST.md"

resolved_targets = json.loads(resolved_targets_path.read_text(encoding="utf-8"))
if not isinstance(resolved_targets, dict):
    raise SystemExit("Resolved main-result targets must be a JSON object")
labels = resolved_targets.get("labels", [])
targets = resolved_targets.get("targets", [])
workflow["main_result_labels"] = labels
workflow["main_result_targets"] = targets

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

echo "  Prewarming Lean dependencies and build artifacts as supervisor user..."
env \
  HOME="${HOME:-/home/leanagent}" \
  ELAN_HOME="$ELAN_HOME" \
  PATH="$BURST_PATH" \
  bash -lc "
    set -euo pipefail
    umask 0002
    cd '$REPO'
    lake update
    lake exe cache get
    lake build Tablet.Preamble
    lake build Tablet
    lake env lean .agent-supervisor/scratch/example.lean
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

echo "  Validating worker-side shared access..."
sudo -n -u "$BURST_USER" env \
  HOME="$BURST_HOME" \
  ELAN_HOME="$ELAN_HOME" \
  PATH="$BURST_PATH" \
  bash -lc "
    set -euo pipefail
    umask 0002
    git config --global --add safe.directory '$REPO' >/dev/null 2>&1 || true
    cd '$REPO'
    lake env lean .agent-supervisor/scratch/example.lean
  "

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
