"""Git operations for the supervisor.

Git is the single source of truth for cycle history, diffs, and state.
Each cycle = one commit + lightweight tag. The web viewer reads from git.

On rewind: checkout the target cycle's tag, then clean agent state
to prevent context poisoning (kill servers, clear chat histories).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.chat_history import commit_chat_checkpoint, rewind_chat_history
from lagent_tablets.viewer_state import refresh_project_viewer_cache


GITIGNORE_CONTENT = """\
# Build artifacts
.lake/

# Runtime artifacts (ephemeral, large)
.agent-supervisor/staging/
.agent-supervisor/logs/
.agent-supervisor/history/
.agent-supervisor/chats/
.agent-supervisor/scratch/
.agent-supervisor/viewer/

# Signal files
.agent-supervisor/pause
.agent-supervisor/human_approve.json
.agent-supervisor/human_feedback.json
.agent-supervisor/*.lock
.agent-supervisor/**/*.lock

# Editor / OS
.DS_Store
*.swp
*~
"""


FINAL_CYCLE_TAG_RE = re.compile(r"^cycle-(\d+)$")
CHECKPOINT_TAG_RE = re.compile(r"^cycle-(\d+)-(worker|verification)$")
CHECKPOINT_STAGES = ("worker", "verification")


def _ensure_gitignore(repo: Path) -> None:
    gitignore = repo / ".gitignore"
    current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if current != GITIGNORE_CONTENT:
        gitignore.write_text(GITIGNORE_CONTENT, encoding="utf-8")


def _drop_tracked_runtime_artifacts(repo: Path) -> None:
    # Older repos may already have staging artifacts tracked. Remove them from the
    # index while leaving the working tree files in place.
    _git(repo, "rm", "-r", "--cached", "--ignore-unmatch", ".agent-supervisor/staging", check=False)


def _scrub_checkpoint_temp_files(repo: Path) -> None:
    from lagent_tablets.check import cleanup_axiom_audit_temp_files

    cleanup_axiom_audit_temp_files(repo)


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(repo), check=check,
    )


def _is_git_repo(repo: Path) -> bool:
    try:
        result = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def cycle_tag(cycle: int) -> str:
    return f"cycle-{cycle}"


def checkpoint_tag(cycle: int, checkpoint: str) -> str:
    return f"cycle-{cycle}-{checkpoint}"


def _is_final_cycle_tag(tag: str) -> bool:
    return FINAL_CYCLE_TAG_RE.fullmatch(tag.strip()) is not None


def _cycle_tag_sort_key(tag: str) -> tuple[int, int]:
    tag = tag.strip()
    final_match = FINAL_CYCLE_TAG_RE.fullmatch(tag)
    if final_match:
        return (int(final_match.group(1)), 2)
    checkpoint_match = CHECKPOINT_TAG_RE.fullmatch(tag)
    if checkpoint_match:
        stage_order = {"worker": 0, "verification": 1}
        return (int(checkpoint_match.group(1)), stage_order[checkpoint_match.group(2)])
    return (-1, -1)


def _stage_ref(cycle: int, stage: str) -> str:
    normalized = str(stage or "reviewer").strip().lower()
    if normalized == "reviewer":
        return cycle_tag(cycle)
    if normalized in CHECKPOINT_STAGES:
        return checkpoint_tag(cycle, normalized)
    raise ValueError(f"Unsupported rewind stage: {stage!r}")


def _root_commit(repo: Path) -> Optional[str]:
    result = _git(repo, "rev-list", "--max-parents=0", "HEAD", check=False)
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


def _read_cycle_meta(repo: Path, ref: str) -> Dict[str, Any]:
    meta_result = _git(repo, "show", f"{ref}:.agent-supervisor/cycle_meta.json", check=False)
    if meta_result.returncode != 0:
        return {}
    try:
        return json.loads(meta_result.stdout)
    except json.JSONDecodeError:
        return {}


def list_valid_reset_checkpoints(repo: Path) -> List[Dict[str, Any]]:
    """Return supervisor-legal reset targets for reviewer-directed resets.

    A valid checkpoint is an exact committed state whose recorded outcome is not
    INVALID. We also expose the repository root commit as `initial`.
    """
    checkpoints: List[Dict[str, Any]] = []
    root = _root_commit(repo)
    if root:
        checkpoints.append(
            {
                "ref": "initial",
                "label": "initial setup commit",
                "outcome": "PROGRESS",
                "phase": "",
                "checkpoint": "initial",
                "commit": root,
            }
        )

    result = _git(repo, "tag", "-l", "cycle-*", "--sort=-version:refname", check=False)
    if result.returncode != 0:
        return checkpoints

    for tag in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
        meta = _read_cycle_meta(repo, tag)
        outcome = str(meta.get("outcome", "") or "").strip().upper()
        if outcome == "INVALID":
            continue
        checkpoint_stage = str(meta.get("checkpoint", "") or "").strip().lower()
        if _is_final_cycle_tag(tag):
            stage_label = "reviewer/final"
        elif checkpoint_stage in CHECKPOINT_STAGES:
            stage_label = checkpoint_stage
        else:
            stage_label = "checkpoint"
        cycle_value = meta.get("cycle", "")
        phase = str(meta.get("phase", "") or "").strip()
        label_bits = [f"cycle {cycle_value}", stage_label]
        if phase:
            label_bits.append(phase)
        if outcome:
            label_bits.append(outcome)
        checkpoints.append(
            {
                "ref": tag,
                "label": " | ".join(label_bits),
                "outcome": outcome,
                "phase": phase,
                "checkpoint": checkpoint_stage or ("reviewer" if _is_final_cycle_tag(tag) else ""),
                "cycle": cycle_value,
                "commit": _git(repo, "rev-parse", tag, check=False).stdout.strip(),
            }
        )
    return checkpoints


def _delete_future_cycle_tags(repo: Path, target_key: tuple[int, int]) -> None:
    tags_result = _git(repo, "tag", "-l", "cycle-*", check=False)
    if tags_result.returncode != 0:
        return
    for existing in [t.strip() for t in tags_result.stdout.splitlines() if t.strip()]:
        if _cycle_tag_sort_key(existing) > target_key:
            _git(repo, "tag", "-d", existing, check=False)


def reset_to_checkpoint_ref(
    repo: Path,
    ref: str,
    *,
    burst_user: str = "lagentworker",
) -> bool:
    """Reset the main project repo to an exact committed valid checkpoint and clean it.

    Reviewer-guided resets are exact rewinds, not branches. After reset, both the
    main repo and the nested chats repo should be clean at the requested checkpoint.
    """
    normalized = str(ref or "").strip()
    if not normalized:
        return False

    if normalized == "initial":
        actual_ref = _root_commit(repo)
        target_key = (0, -1)
    else:
        check = _git(repo, "rev-parse", normalized, check=False)
        if check.returncode != 0:
            print(f"Checkpoint {normalized} does not exist")
            return False
        actual_ref = normalized
        target_key = _cycle_tag_sort_key(normalized)

    if not actual_ref:
        print("Could not resolve initial setup commit")
        return False

    subprocess.run(["pkill", "-9", "-f", "agentapi"], capture_output=True)

    _git(repo, "reset", "--hard", actual_ref)
    _git(repo, "clean", "-fdx", "-e", ".agent-supervisor/chats/", timeout=120)
    _delete_future_cycle_tags(repo, target_key)
    rewind_chat_history(repo, tag=normalized)
    print(f"Reset project worktree to {normalized} and cleaned it")
    print(f"  Agent sessions cleared for {burst_user}")
    return True


def _commit_tagged_state(
    repo: Path,
    *,
    cycle: int,
    tag: str,
    phase: str = "",
    outcome: str = "",
    active_node: str = "",
    detail: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Commit the current state and tag it with an exact checkpoint ref."""
    if not _is_git_repo(repo):
        return None
    _ensure_gitignore(repo)
    _drop_tracked_runtime_artifacts(repo)
    _scrub_checkpoint_temp_files(repo)
    meta_path = repo / ".agent-supervisor" / "cycle_meta.json"
    meta_data = {
        "cycle": cycle,
        "phase": phase,
        "outcome": outcome,
        "active_node": active_node,
        "detail": detail,
        **(meta or {}),
    }
    meta_path.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

    _git(repo, "add", "-A")
    result = _git(repo, "diff", "--cached", "--quiet", check=False)
    if result.returncode == 0:
        return None

    summary = f"{tag}: {outcome}"
    if active_node:
        summary += f" on {active_node}"
    if phase:
        summary += f" ({phase})"
    body = f"\n\n{detail}" if detail else ""
    _git(repo, "commit", "-m", f"{summary}{body}")
    _git(repo, "tag", "-d", tag, check=False)
    _git(repo, "tag", tag)
    commit_chat_checkpoint(repo, tag=tag)
    refresh_project_viewer_cache(repo, repo / ".agent-supervisor")
    result = _git(repo, "rev-parse", "HEAD")
    return result.stdout.strip()


def init_repo(repo: Path, *, author_name: str = "lagent-supervisor",
              author_email: str = "lagent@localhost") -> None:
    """Initialize git repo if not already initialized."""
    if not (repo / ".git").exists():
        _git(repo, "init")
        _git(repo, "config", "user.name", author_name)
        _git(repo, "config", "user.email", author_email)

    # Always ensure .gitignore is up to date
    _ensure_gitignore(repo)

    # Set author config (may have changed)
    _git(repo, "config", "user.name", author_name)
    _git(repo, "config", "user.email", author_email)


def commit_cycle(
    repo: Path,
    cycle: int,
    *,
    phase: str = "",
    outcome: str = "",
    active_node: str = "",
    detail: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Commit the current state and tag it as cycle-N.

    Returns the commit hash, or None if nothing to commit.
    """
    return _commit_tagged_state(
        repo,
        cycle=cycle,
        tag=cycle_tag(cycle),
        phase=phase,
        outcome=outcome,
        active_node=active_node,
        detail=detail,
        meta=meta,
    )


def commit_checkpoint(
    repo: Path,
    cycle: int,
    checkpoint: str,
    *,
    phase: str = "",
    outcome: str = "",
    active_node: str = "",
    detail: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Commit an exact subcycle checkpoint such as worker or verification."""
    normalized = str(checkpoint or "").strip().lower()
    if normalized not in CHECKPOINT_STAGES:
        raise ValueError(f"Unsupported checkpoint stage: {checkpoint!r}")
    return _commit_tagged_state(
        repo,
        cycle=cycle,
        tag=checkpoint_tag(cycle, normalized),
        phase=phase,
        outcome=outcome,
        active_node=active_node,
        detail=detail,
        meta={**(meta or {}), "checkpoint": normalized},
    )


def push_remote(repo: Path, *, remote: str = "origin", branch: str = "main") -> bool:
    """Push commits and tags to remote. Returns True on success."""
    try:
        _git(repo, "push", remote, branch, "--tags", timeout=60)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def get_cycle_history(repo: Path) -> List[Dict[str, Any]]:
    """Get cycle history from git tags + commits.

    Returns list of {cycle, hash, timestamp, message, phase, outcome, active_node}.
    """
    result = _git(repo, "tag", "-l", "cycle-*", "--sort=version:refname", check=False)
    if result.returncode != 0:
        return []

    tags = result.stdout.strip().split("\n")
    history = []

    for tag in tags:
        tag = tag.strip()
        if not tag or not _is_final_cycle_tag(tag):
            continue
        cycle_num = int(FINAL_CYCLE_TAG_RE.fullmatch(tag).group(1))

        # Get commit info
        log_result = _git(repo, "log", "-1", "--format=%H%n%aI%n%s%n%b", tag, check=False)
        if log_result.returncode != 0:
            continue
        lines = log_result.stdout.strip().split("\n", 3)
        commit_hash = lines[0] if len(lines) > 0 else ""
        timestamp = lines[1] if len(lines) > 1 else ""
        subject = lines[2] if len(lines) > 2 else ""
        body = lines[3] if len(lines) > 3 else ""

        # Try to read cycle_meta.json from that commit
        meta = {}
        meta_result = _git(repo, "show", f"{tag}:.agent-supervisor/cycle_meta.json", check=False)
        if meta_result.returncode == 0:
            try:
                meta = json.loads(meta_result.stdout)
            except json.JSONDecodeError:
                pass

        history.append({
            "cycle": cycle_num,
            "hash": commit_hash,
            "timestamp": timestamp,
            "message": subject,
            "body": body.strip(),
            **meta,
        })

    return history


def get_cycle_diff(repo: Path, cycle: int) -> str:
    """Get the unified diff for a specific cycle (vs previous cycle)."""
    tag = cycle_tag(cycle)
    prev_tag = cycle_tag(cycle - 1)

    # Check if previous tag exists
    check = _git(repo, "rev-parse", prev_tag, check=False)
    if check.returncode != 0:
        # First cycle — diff against empty tree
        result = _git(repo, "diff", "4b825dc642cb6eb9a060e54bf899d15f3bc9", tag,
                      "--", "Tablet/", check=False, timeout=10)
    else:
        result = _git(repo, "diff", prev_tag, tag, "--", "Tablet/", check=False, timeout=10)

    return result.stdout if result.returncode == 0 else ""


def get_file_at_cycle(repo: Path, cycle: int, file_path: str) -> str:
    """Get file content at a specific cycle."""
    tag = cycle_tag(cycle)
    result = _git(repo, "show", f"{tag}:{file_path}", check=False)
    return result.stdout if result.returncode == 0 else ""


def get_tablet_at_cycle(repo: Path, cycle: int) -> Dict[str, Any]:
    """Get tablet.json at a specific cycle."""
    content = get_file_at_cycle(repo, cycle, ".agent-supervisor/tablet.json")
    if content:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
    return {}


def get_state_at_cycle(repo: Path, cycle: int) -> Dict[str, Any]:
    """Get state.json at a specific cycle."""
    content = get_file_at_cycle(repo, cycle, ".agent-supervisor/state.json")
    if content:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
    return {}


def rewind_to_cycle(
    repo: Path,
    cycle: int,
    *,
    stage: str = "reviewer",
    burst_user: str = "lagentworker",
) -> bool:
    """Rewind the repo to an exact committed checkpoint.

    This:
    1. Resets hard to the exact committed checkpoint tag
    2. Cleans the worktree completely
    2. Kills all agentapi servers
    3. Clears agent chat histories (prevents context poisoning)
    4. Deletes future cycle/checkpoint tags for destructive rewind semantics

    Returns True on success.
    """
    try:
        tag = _stage_ref(cycle, stage)
    except ValueError as exc:
        print(str(exc))
        return False

    # Verify tag exists
    check = _git(repo, "rev-parse", tag, check=False)
    if check.returncode != 0:
        print(f"Tag {tag} does not exist")
        return False

    # Kill all agent servers
    subprocess.run(["pkill", "-9", "-f", "agentapi"], capture_output=True)
    import time
    time.sleep(2)

    # Exact restore from the committed checkpoint.
    _git(repo, "reset", "--hard", tag)
    rewind_chat_history(repo, tag=tag)
    _git(repo, "clean", "-fdx", "-e", ".agent-supervisor/chats/", timeout=120)

    # Clear agent chat histories to prevent context poisoning
    project_name = repo.name
    project_name_hyphen = project_name.replace("_", "-")

    for variant in [project_name, project_name_hyphen]:
        gemini_chats = Path(f"/home/{burst_user}/.gemini/tmp/{variant}/chats")
        if gemini_chats.exists():
            subprocess.run(
                ["sudo", "-n", "-u", burst_user, "rm", "-rf", str(gemini_chats)],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["sudo", "-n", "-u", burst_user, "mkdir", "-p", str(gemini_chats)],
                capture_output=True, timeout=10,
            )

    # Clear Claude sessions
    claude_slug = str(repo).replace("/", "-").lstrip("-")
    claude_dir = Path(f"/home/{burst_user}/.claude/projects/{claude_slug}")
    if claude_dir.exists():
        subprocess.run(
            ["sudo", "-n", "-u", burst_user, "rm", "-rf", str(claude_dir)],
            capture_output=True, timeout=10,
        )

    # Delete future cycle/checkpoint tags so replay semantics stay exact.
    tags_result = _git(repo, "tag", "-l", "cycle-*", check=False)
    if tags_result.returncode == 0:
        target_key = _cycle_tag_sort_key(tag)
        for existing in [t.strip() for t in tags_result.stdout.splitlines() if t.strip()]:
            if _cycle_tag_sort_key(existing) > target_key:
                _git(repo, "tag", "-d", existing, check=False)

    print(f"Rewound to {tag}")
    print(f"  Agent sessions cleared for {burst_user}")
    return True


def current_cycle_from_git(repo: Path) -> int:
    """Get the latest cycle number from git tags."""
    result = _git(repo, "tag", "-l", "cycle-*", "--sort=-version:refname", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return 0
    for tag in [t.strip() for t in result.stdout.splitlines() if t.strip()]:
        match = FINAL_CYCLE_TAG_RE.fullmatch(tag)
        if match:
            return int(match.group(1))
    return 0
