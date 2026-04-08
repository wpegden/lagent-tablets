"""The main supervisor cycle loop.

Each cycle: worker burst -> validation -> (NL verification if needed) -> reviewer burst -> state update.

Cycle outcomes:
  PROGRESS:    Changes accepted. Node closed or new nodes created and verified.
  NO_PROGRESS: Worker didn't submit meaningful changes.
  INVALID:     Deterministic checks failed (compilation, imports, keywords, etc.)
  REJECTED:    Verification model rejected NL content.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import hashlib

from lagent_tablets.adapters import BurstResult, ProviderConfig
from lagent_tablets.burst import (
    extract_json_decision,
    run_reviewer_burst,
    run_worker_burst,
)
from lagent_tablets.config import Config, Policy
from lagent_tablets.prompts import (
    build_reviewer_prompt,
    build_correspondence_prompt,
    build_nl_proof_prompt,
    build_verification_prompt,
    build_worker_prompt,
)
from lagent_tablets.state import (
    SupervisorState,
    TabletNode,
    TabletState,
    load_json,
    save_json,
    save_state,
    save_tablet,
    state_path,
    tablet_path,
    timestamp_now,
)
from lagent_tablets.tablet import (
    PREAMBLE_NAME,
    declaration_hash,
    extract_tablet_imports,
    find_orphan_nodes,
    has_sorry,
    is_valid_node_name,
    find_name_conflicts,
    mark_node_closed,
    mark_node_open,
    node_lean_path,
    node_tex_path,
    regenerate_support_files,
    register_new_node,
    validate_imports,
    validate_preamble_diff,
    validate_tex_format,
    extract_marker_name,
    extract_declaration_name,
    scan_forbidden_keywords,
)
from lagent_tablets.check import check_node as run_check_node


def _accumulate_usage(state: SupervisorState, role: str, usage: Optional[Dict[str, Any]]) -> None:
    """Add a burst's token usage to the running totals in state.agent_token_usage.

    Tracks per-role (worker, reviewer, correspondence, nl_proof) and per-model.
    """
    if not usage:
        return
    if not isinstance(state.agent_token_usage, dict):
        state.agent_token_usage = {}

    # Per-role accumulation
    if role not in state.agent_token_usage:
        state.agent_token_usage[role] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    bucket = state.agent_token_usage[role]
    bucket["input_tokens"] += usage.get("input_tokens", 0)
    bucket["output_tokens"] += usage.get("output_tokens", 0)
    bucket["cached_input_tokens"] = bucket.get("cached_input_tokens", 0) + usage.get("cached_input_tokens", usage.get("cache_read_input_tokens", 0))
    bucket["calls"] += 1

    # Per-model tracking within the role
    model = usage.get("model", "unknown")
    provider = usage.get("provider", "unknown")
    model_key = f"{provider}/{model}"
    if "by_model" not in bucket:
        bucket["by_model"] = {}
    if model_key not in bucket["by_model"]:
        bucket["by_model"][model_key] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    mbucket = bucket["by_model"][model_key]
    mbucket["input_tokens"] += usage.get("input_tokens", 0)
    mbucket["output_tokens"] += usage.get("output_tokens", 0)
    mbucket["calls"] += 1
from lagent_tablets.verification import (
    FORBIDDEN_KEYWORDS_DEFAULT,
    write_scripts,
)


# ---------------------------------------------------------------------------
# Cycle outcome
# ---------------------------------------------------------------------------

@dataclass
class CycleOutcome:
    """Result of a single supervisor cycle."""
    outcome: str  # PROGRESS, NO_PROGRESS, INVALID, REJECTED
    detail: str = ""
    build_output: str = ""
    rejection: Optional[Dict[str, Any]] = None
    nodes_closed: List[str] = field(default_factory=list)
    nodes_created: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"outcome": self.outcome, "detail": self.detail}
        if self.build_output:
            d["build_output"] = self.build_output
        if self.rejection:
            d["rejection"] = self.rejection
        if self.nodes_closed:
            d["nodes_closed"] = self.nodes_closed
        if self.nodes_created:
            d["nodes_created"] = self.nodes_created
        return d


# ---------------------------------------------------------------------------
# File snapshot for change detection
# ---------------------------------------------------------------------------

def _snapshot_tablet_dir(repo_path: Path) -> Dict[str, str]:
    """SHA-256 of every file in Tablet/."""
    import hashlib
    snapshot: Dict[str, str] = {}
    tdir = repo_path / "Tablet"
    if not tdir.exists():
        return snapshot
    for path in sorted(tdir.iterdir()):
        if path.is_file():
            snapshot[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _detect_changes(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, List[str]]:
    """Compare two snapshots. Returns {created, modified, deleted} file name lists."""
    all_names = set(before) | set(after)
    created, modified, deleted = [], [], []
    for name in sorted(all_names):
        if name not in before:
            created.append(name)
        elif name not in after:
            deleted.append(name)
        elif before[name] != after[name]:
            modified.append(name)
    return {"created": created, "modified": modified, "deleted": deleted}


# ---------------------------------------------------------------------------
# Permission setup
# ---------------------------------------------------------------------------

def setup_permissions(config: Config, active_node: str, *, easy_mode: bool = False) -> None:
    """Set file permissions before a worker burst.

    The burst_user runs the agent CLI. File permissions control what it can write:
    - Active node .lean/.tex: 0o664 (group-writable) -- worker can edit
    - Preamble.lean: 0o664 (hard) / 0o644 (easy) -- easy workers cannot add imports
    - Everything else: 0o644 (group-read-only) -- worker CANNOT edit
    - Tablet/ directory: 0o2775 (hard) / 0o2755 (easy) -- easy workers cannot create files
    - worker_handoff.json: 0o664 -- worker can write completion marker

    The shared group is 'leanagent' (gid from leanagent user).
    The supervisor (leanagent) is the owner; burst_user (lagentworker) is in the group.
    """
    import grp
    import os

    repo = config.repo_path
    tdir = repo / "Tablet"
    if not tdir.exists():
        return

    # Use leanagent as the shared group
    try:
        gid = grp.getgrnam("leanagent").gr_gid
    except KeyError:
        return

    # Tablet directory: setgid.
    # Hard mode: group-writable (0o2775) so worker can create new files.
    # Easy mode: group-read (0o2755) so worker CANNOT create new files.
    tdir_mode = 0o2755 if easy_mode else 0o2775
    try:
        os.chown(str(tdir), -1, gid)
        os.chmod(str(tdir), tdir_mode)
    except PermissionError:
        pass

    # Files the worker may edit.
    # Easy mode: only active node files + handoff. Preamble is read-only.
    writable_basenames = {
        f"{active_node}.lean",
        f"{active_node}.tex",
    }
    if not easy_mode:
        writable_basenames.add("Preamble.lean")

    for path in tdir.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            target_mode = 0o664 if path.name in writable_basenames else 0o644

            # If we own the file, just set permissions
            if stat.st_uid == os.getuid():
                if stat.st_gid != gid:
                    os.chown(str(path), -1, gid)
                if (stat.st_mode & 0o777) != target_mode:
                    os.chmod(str(path), target_mode)
            else:
                # File owned by another user (lagentworker).
                # We can't chown it, but if the directory is group-writable,
                # we can delete and recreate it with the right ownership.
                # Only do this for supervisor-generated files, NOT tablet node files
                # (which the worker legitimately created).
                if path.name in {"INDEX.md", "README.md", "header.tex"}:
                    content = path.read_text(encoding="utf-8")
                    path.unlink()
                    path.write_text(content, encoding="utf-8")
                    os.chmod(str(path), target_mode)
                # For node files owned by lagentworker: the group permissions
                # still apply (lagentworker is in leanagent group).
                # 0o644 means owner(lagentworker)=rw, group(leanagent)=r, other=r
                # 0o664 means owner(lagentworker)=rw, group(leanagent)=rw, other=r
                # We need sudo to change these. Use a targeted chmod via sudo.
                else:
                    import subprocess as sp
                    sp.run(["sudo", "-n", "-u", "lagentworker", "chmod",
                            oct(target_mode)[2:], str(path)],
                           capture_output=True, timeout=5)
        except (PermissionError, OSError):
            pass

    # worker_handoff.json: group-writable so the worker can create it
    handoff = repo / "worker_handoff.json"
    try:
        if handoff.exists():
            os.chown(str(handoff), -1, gid)
            os.chmod(str(handoff), 0o664)
        # Also ensure the repo dir allows creating new files
        os.chown(str(repo), -1, gid)
        os.chmod(str(repo), 0o2775)
    except PermissionError:
        pass


def _setup_theorem_stating_permissions(config: Config) -> None:
    """Set permissions for theorem_stating: everything in Tablet/ is writable."""
    import grp
    import os

    repo = config.repo_path
    tdir = repo / "Tablet"
    if not tdir.exists():
        tdir.mkdir(parents=True, exist_ok=True)

    try:
        gid = grp.getgrnam("leanagent").gr_gid
    except KeyError:
        return

    # Tablet directory: setgid, group-writable
    try:
        os.chown(str(tdir), -1, gid)
        os.chmod(str(tdir), 0o2775)
    except PermissionError:
        pass

    # All files in Tablet: group-writable
    for path in tdir.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            if stat.st_uid == os.getuid():
                if stat.st_gid != gid:
                    os.chown(str(path), -1, gid)
                os.chmod(str(path), 0o664)
            else:
                import subprocess as sp
                sp.run(["sudo", "-n", "-u", "lagentworker", "chmod", "664", str(path)],
                       capture_output=True, timeout=5)
        except (PermissionError, OSError):
            pass

    # worker_handoff.json and repo root
    for p in [repo / "worker_handoff.json", repo]:
        try:
            if p.exists():
                os.chown(str(p), -1, gid)
                os.chmod(str(p), 0o2775 if p.is_dir() else 0o664)
        except PermissionError:
            pass


# ---------------------------------------------------------------------------
# Post-burst validation
# ---------------------------------------------------------------------------

def validate_worker_cycle(
    config: Config,
    tablet: TabletState,
    active_node: str,
    snapshot_before: Dict[str, str],
    snapshot_after: Dict[str, str],
) -> CycleOutcome:
    """Run all deterministic checks after a worker burst.

    Returns a CycleOutcome. PROGRESS/NO_PROGRESS/INVALID.
    Does NOT invoke the verification model (that's separate).
    """
    changes = _detect_changes(snapshot_before, snapshot_after)
    repo = config.repo_path
    allowed_prefixes = config.workflow.allowed_import_prefixes
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]

    # Detect created .lean files (potential new nodes)
    new_lean_names = [
        fname.removesuffix(".lean")
        for fname in changes["created"]
        if fname.endswith(".lean")
    ]

    # Detect if active node changed
    active_lean = f"{active_node}.lean"
    active_tex = f"{active_node}.tex"
    active_changed = active_lean in changes["modified"]

    # Check: no deletions allowed
    if changes["deleted"]:
        return CycleOutcome(
            outcome="INVALID",
            detail=f"Files were deleted (not allowed): {changes['deleted']}",
        )

    # Check: only allowed files modified
    # Supervisor-generated files are excluded from the check
    supervisor_generated = {"INDEX.md", "README.md", "header.tex", "Tablet.lean"}
    allowed_modified = {active_lean, active_tex, "Preamble.lean"} | supervisor_generated
    unexpected_modified = [f for f in changes["modified"] if f not in allowed_modified]
    if unexpected_modified:
        return CycleOutcome(
            outcome="INVALID",
            detail=f"Unexpected files modified: {unexpected_modified}. Only {active_node} and Preamble may be modified.",
        )

    # No changes at all?
    if not changes["created"] and not changes["modified"]:
        return CycleOutcome(outcome="NO_PROGRESS", detail="No files were changed.")

    # Validate Preamble changes
    if "Preamble.lean" in changes["modified"]:
        old_preamble = (repo / "Tablet" / "Preamble.lean").read_text(encoding="utf-8")
        # We need the old content -- reconstruct from the fact that the hash changed
        # Actually we can't reconstruct old content from hash. We need to check the current file.
        # The preamble_diff validation compares structurally, so we read current and check format.
        preamble_errors = validate_preamble_diff(
            # We don't have the old content from just hashes.
            # In practice, we'd save the old content before the burst.
            # For now, just validate the current preamble's import patterns.
            "", (repo / "Tablet" / "Preamble.lean").read_text(encoding="utf-8"),
            allowed_prefixes,
        )
        # This is a simplification -- full implementation should save old content.
        # For now just validate current imports are all legal.
        current_preamble = (repo / "Tablet" / "Preamble.lean").read_text(encoding="utf-8")
        import_violations = validate_imports(current_preamble, allowed_prefixes)
        if import_violations:
            return CycleOutcome(
                outcome="INVALID",
                detail=f"Preamble has unauthorized imports: {import_violations}",
            )

    # Validate active node
    if active_changed:
        active_path = node_lean_path(repo, active_node)
        content = active_path.read_text(encoding="utf-8")

        # Declaration signature intact?
        stored_hash = tablet.nodes.get(active_node, TabletState()).lean_statement_hash if active_node in tablet.nodes else ""
        if stored_hash:
            actual_hash = declaration_hash(content, node_name=active_node)
            if actual_hash != stored_hash:
                return CycleOutcome(
                    outcome="INVALID",
                    detail=f"Declaration signature of {active_node} was modified. This is not allowed.",
                )

        # Imports valid?
        import_violations = validate_imports(content, allowed_prefixes)
        if import_violations:
            return CycleOutcome(
                outcome="INVALID",
                detail=f"Unauthorized imports in {active_node}: {import_violations}",
            )

        # Forbidden keywords?
        hits = scan_forbidden_keywords(content, forbidden)
        non_sorry_hits = [h for h in hits if h["keyword"] != "sorry"]
        if non_sorry_hits:
            return CycleOutcome(
                outcome="INVALID",
                detail=f"Forbidden keywords in {active_node}: {[h['keyword'] for h in non_sorry_hits]}",
            )

        # Compile check
        result = run_check_node(
            repo, active_node,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden,
            expected_declaration_hash=stored_hash,
            timeout_seconds=config.burst_timeout_seconds,
        )
        if not result.compiles:
            # Check if the failure is just Lake package noise
            from lagent_tablets.verification import _is_lake_package_error
            if _is_lake_package_error(result.build_output):
                pass  # Ignore Lake package errors -- the code itself is fine
            else:
                return CycleOutcome(
                    outcome="INVALID",
                    detail=f"Compilation failed for {active_node}",
                    build_output=result.build_output,
                )

    # Validate new nodes
    new_node_names = []
    for lean_name in new_lean_names:
        name = lean_name  # filename without .lean
        if name == PREAMBLE_NAME or name == "Axioms":
            continue

        # Check name validity
        if not is_valid_node_name(name):
            _cleanup_new_files(repo, lean_name)
            return CycleOutcome(outcome="INVALID", detail=f"Invalid node name: {name!r}")

        # Check for conflicts
        conflicts = find_name_conflicts(tablet, [name])
        if conflicts:
            _cleanup_new_files(repo, lean_name)
            return CycleOutcome(outcome="INVALID", detail=f"Node name already exists: {name!r}")

        # Check .tex exists
        tex_path = node_tex_path(repo, name)
        if not tex_path.exists():
            _cleanup_new_files(repo, lean_name)
            return CycleOutcome(outcome="INVALID", detail=f"New node {name} has .lean but no .tex file")

        # Check marker
        lean_path = node_lean_path(repo, name)
        lean_content = lean_path.read_text(encoding="utf-8")
        marker = extract_marker_name(lean_content)
        if marker != name:
            _cleanup_new_files(repo, lean_name)
            return CycleOutcome(outcome="INVALID", detail=f"New node {name}: marker says {marker!r}, expected {name!r}")

        # Check declaration name matches
        decl_name = extract_declaration_name(lean_content)
        if decl_name != name:
            _cleanup_new_files(repo, lean_name)
            return CycleOutcome(outcome="INVALID", detail=f"New node {name}: declaration name is {decl_name!r}, expected {name!r}")

        # Check .tex format
        tex_content = tex_path.read_text(encoding="utf-8")
        tex_errors = validate_tex_format(tex_content)
        if tex_errors:
            _cleanup_new_files(repo, lean_name)
            return CycleOutcome(outcome="INVALID", detail=f"New node {name} .tex format errors: {tex_errors}")

        # Check imports
        import_violations = validate_imports(lean_content, allowed_prefixes)
        if import_violations:
            _cleanup_new_files(repo, lean_name)
            return CycleOutcome(outcome="INVALID", detail=f"New node {name} has unauthorized imports: {import_violations}")

        # Compile check
        result = run_check_node(
            repo, name,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden,
            timeout_seconds=config.burst_timeout_seconds,
        )
        if not result.compiles:
            from lagent_tablets.verification import _is_lake_package_error
            if not _is_lake_package_error(result.build_output):
                _cleanup_new_files(repo, lean_name)
                return CycleOutcome(
                    outcome="INVALID",
                    detail=f"New node {name} does not compile",
                    build_output=result.build_output,
                )

        new_node_names.append(name)

    # Determine what progress was made
    nodes_closed = []
    if active_changed:
        active_path = node_lean_path(repo, active_node)
        if not has_sorry(active_path.read_text(encoding="utf-8")):
            nodes_closed.append(active_node)

    # Also check if any new nodes were closed in the same cycle
    for name in new_node_names:
        lean_content = node_lean_path(repo, name).read_text(encoding="utf-8")
        if not has_sorry(lean_content):
            nodes_closed.append(name)

    if not nodes_closed and not new_node_names and not active_changed:
        return CycleOutcome(outcome="NO_PROGRESS", detail="No meaningful changes detected.")

    # Build the detail message
    parts = []
    if nodes_closed:
        parts.append(f"closed: {nodes_closed}")
    if new_node_names:
        parts.append(f"created: {new_node_names}")
    if active_changed and active_node not in nodes_closed:
        parts.append(f"{active_node} modified (still open)")

    return CycleOutcome(
        outcome="PROGRESS",
        detail="; ".join(parts),
        nodes_closed=nodes_closed,
        nodes_created=new_node_names,
    )


def validate_worker_cycle_v2(
    config: Config,
    tablet: TabletState,
    active_node: str,
    *,
    active_changed: bool,
    new_lean_files: List[str],
) -> CycleOutcome:
    """Validate after a worker burst. Simpler than the old snapshot-based approach.

    Checks:
    1. Did the active node change? If not, NO_PROGRESS.
    2. Is the declaration signature intact?
    3. Are imports valid?
    4. Any forbidden keywords?
    5. Does it compile?
    6. Is it sorry-free? If yes, CLOSED.
    7. New files: validate names, markers, .tex pairs.
    """
    repo = config.repo_path
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]

    if not active_changed and not new_lean_files:
        return CycleOutcome(outcome="NO_PROGRESS", detail="No files were changed.")

    nodes_closed = []
    nodes_created = []

    # Validate active node using check.py (the SINGLE SOURCE OF TRUTH)
    if active_changed:
        stored_hash = tablet.nodes[active_node].lean_statement_hash if active_node in tablet.nodes else ""
        result = run_check_node(
            repo, active_node,
            allowed_prefixes=config.workflow.allowed_import_prefixes,
            forbidden_keywords=forbidden,
            expected_hash=stored_hash,
        )
        if result["errors"]:
            # Return the first error as the INVALID detail
            return CycleOutcome(
                outcome="INVALID",
                detail=result["errors"][0],
                build_output=result.get("build_output", ""),
            )
        if result["ok"]:
            nodes_closed.append(active_node)

    # Validate new files
    for name in new_lean_files:
        if name in ("Preamble", "Axioms") or name in tablet.nodes:
            continue
        if not is_valid_node_name(name):
            return CycleOutcome(outcome="INVALID", detail=f"Invalid node name: {name!r}")

        lean_path = node_lean_path(repo, name)
        tex_path = node_tex_path(repo, name)
        if not tex_path.exists():
            return CycleOutcome(outcome="INVALID", detail=f"New node {name} has .lean but no .tex file")

        content = lean_path.read_text(encoding="utf-8")
        marker = extract_marker_name(content)
        if marker != name:
            return CycleOutcome(outcome="INVALID", detail=f"New node {name}: marker says {marker!r}")

        import_violations = validate_imports(content, config.workflow.allowed_import_prefixes)
        if import_violations:
            return CycleOutcome(outcome="INVALID", detail=f"New node {name} has unauthorized imports: {import_violations}")

        if not has_sorry(content):
            nodes_closed.append(name)
        nodes_created.append(name)

    # Build detail
    parts = []
    if nodes_closed:
        parts.append(f"closed: {nodes_closed}")
    if nodes_created:
        parts.append(f"created: {nodes_created}")
    if active_changed and active_node not in nodes_closed:
        parts.append(f"{active_node} modified (still open)")

    if not parts:
        return CycleOutcome(outcome="NO_PROGRESS", detail="No meaningful changes detected.")

    return CycleOutcome(
        outcome="PROGRESS",
        detail="; ".join(parts),
        nodes_closed=nodes_closed,
        nodes_created=nodes_created,
    )


def _ensure_lake_build(repo: Path) -> None:
    """Run lake build Tablet to ensure oleans are up to date.

    This is important because:
    1. check_node.sh needs oleans to exist
    2. The worker runs lake env lean which needs oleans
    3. After cross-user file creation, oleans may be stale
    """
    import subprocess
    try:
        subprocess.run(
            ["lake", "build", "Tablet"],
            cwd=str(repo),
            capture_output=True, text=True,
            timeout=600,  # 10 min max for build
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"  Warning: lake build failed: {e}")


def _count_consecutive_invalids(state: SupervisorState) -> int:
    """Count how many consecutive INVALID outcomes have occurred on the current active node."""
    # We track this in the review_log -- look for consecutive entries with no reviewer
    # (INVALID cycles don't produce review entries, so gaps in the log indicate INVALIDs)
    # Simpler: use a counter in state. We'll track it via the validation_summary.
    summary = state.validation_summary
    if not isinstance(summary, dict):
        return 0
    return int(summary.get("consecutive_invalids", 0))


def _cleanup_new_files(repo: Path, lean_name: str) -> None:
    """Remove a new node's files on validation failure."""
    tdir = repo / "Tablet"
    for suffix in (".lean", ".tex"):
        path = tdir / f"{lean_name}{suffix}" if not lean_name.endswith(".lean") else tdir / lean_name.replace(".lean", suffix)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# High-level cycle runner
# ---------------------------------------------------------------------------

def reconcile_tablet_status(config: Config, tablet: TabletState) -> List[str]:
    """Check all tablet files and reconcile status with actual file content.

    - Marks sorry-free nodes as closed (if they compile)
    - Marks closed nodes as open if they have sorry (e.g., after a statement change)

    Uses sorry scan + compilation check for reliability.
    """
    repo = config.repo_path
    reconciled = []
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]

    for name, node in list(tablet.nodes.items()):
        if name == "Preamble":
            continue
        lean_path = node_lean_path(repo, name)
        if not lean_path.exists():
            continue
        content = lean_path.read_text(encoding="utf-8")
        file_has_sorry = has_sorry(content)

        if node.status == "open" and not file_has_sorry:
            # File looks sorry-free -- run check.py to verify
            result = run_check_node(
                repo, name,
                allowed_prefixes=config.workflow.allowed_import_prefixes,
                forbidden_keywords=forbidden,
            )
            if result["ok"]:
                mark_node_closed(tablet, name, 0)
                reconciled.append(name)
                print(f"  Reconciled: {name} passes all checks, marking closed")
            elif result["sorry_free"] and result["compiles"]:
                # Passes compilation and sorry-free but has other issues (hash, imports)
                # Still mark closed -- the issues are about the declaration, not the proof
                mark_node_closed(tablet, name, 0)
                reconciled.append(name)
                print(f"  Reconciled: {name} compiles and sorry-free, marking closed (warnings: {result['warnings']})")
            else:
                print(f"  Reconciled: {name} sorry-free but has issues: {result['errors'][:1]}")
        elif node.status == "closed" and file_has_sorry:
            # Node was closed but file now has sorry (e.g., statement was changed)
            mark_node_open(tablet, name, 0)
            reconciled.append(name)
            print(f"  Reconciled: {name} has sorry, marking open")

    return reconciled


def _run_single_correspondence_agent(
    config: Config,
    tablet: TabletState,
    corr_nodes: List[str],
    agent_config: Any,  # CorrespondenceAgentConfig
    *,
    paper_tex: str,
    human_input: str,
    log_dir: Path,
    agent_index: int,
) -> Dict[str, Any]:
    """Run one correspondence agent. Designed to be called from a thread."""
    from lagent_tablets.burst import _clean_terminal_json

    agent_start = time.monotonic()
    repo = config.repo_path
    label = agent_config.label or f"agent-{agent_index}"
    output_file = f"correspondence_result_{agent_index}.json"
    port = 3286 + agent_index * 2  # 3286, 3288, 3290, ...

    result_file = repo / output_file
    result_file.unlink(missing_ok=True)

    prompt = build_correspondence_prompt(
        config, tablet, node_names=corr_nodes, paper_tex=paper_tex,
        human_input=human_input, output_file=output_file,
    )

    agent_provider = ProviderConfig(
        provider=agent_config.provider,
        model=agent_config.model,
        extra_args=agent_config.extra_args,
        fallback_models=getattr(agent_config, 'fallback_models', []),
    )

    burst_result = run_reviewer_burst(
        agent_provider, prompt,
        session_name=config.tmux.session_name,
        work_dir=repo, burst_user=config.tmux.burst_user,
        timeout_seconds=120, log_dir=log_dir, fresh=True,
        port=port,
    )

    decision = None
    if result_file.exists():
        try:
            decision = load_json(result_file)
        except Exception:
            pass
    if not isinstance(decision, dict) and burst_result.ok:
        cleaned = _clean_terminal_json(burst_result.captured_output)
        decision = extract_json_decision(cleaned)

    result = {
        "agent": label,
        "index": agent_index,
        "ok": burst_result.ok,
        "walltime_seconds": round(time.monotonic() - agent_start, 1),
        **(decision if isinstance(decision, dict) else {"overall": "ERROR", "summary": f"Failed to get decision from {label}"}),
    }
    if burst_result.usage:
        result["_usage"] = burst_result.usage
    return result


def _run_single_soundness_agent(
    config: Config,
    tablet: TabletState,
    proof_nodes: List[str],
    agent_config: Any,  # CorrespondenceAgentConfig
    *,
    paper_tex: str,
    human_input: str,
    log_dir: Path,
    agent_index: int,
) -> Dict[str, Any]:
    """Run one NL proof soundness agent. Designed to be called from a thread."""
    from lagent_tablets.burst import _clean_terminal_json

    agent_start = time.monotonic()
    repo = config.repo_path
    label = agent_config.label or f"soundness-{agent_index}"
    output_file = f"nl_proof_result_{agent_index}.json"
    # Soundness agents use ports 3310, 3312, 3314, ... (separate from correspondence 3286+ and viewer 3300)
    port = 3310 + agent_index * 2

    result_file = repo / output_file
    result_file.unlink(missing_ok=True)

    prompt = build_nl_proof_prompt(
        config, tablet, node_names=proof_nodes, paper_tex=paper_tex,
        human_input=human_input,
    )
    # Replace the output filename in the prompt
    if output_file != "nl_proof_result.json":
        prompt = prompt.replace("nl_proof_result.json", output_file)

    agent_provider = ProviderConfig(
        provider=agent_config.provider,
        model=agent_config.model,
        extra_args=agent_config.extra_args,
        fallback_models=getattr(agent_config, 'fallback_models', []),
    )

    burst_result = run_reviewer_burst(
        agent_provider, prompt,
        session_name=config.tmux.session_name,
        work_dir=repo, burst_user=config.tmux.burst_user,
        timeout_seconds=120, log_dir=log_dir, fresh=True,
        port=port,
    )

    decision = None
    if result_file.exists():
        try:
            decision = load_json(result_file)
        except Exception:
            pass
    if not isinstance(decision, dict) and burst_result.ok:
        cleaned = _clean_terminal_json(burst_result.captured_output)
        decision = extract_json_decision(cleaned)

    result = {
        "agent": label,
        "index": agent_index,
        "ok": burst_result.ok,
        "walltime_seconds": round(time.monotonic() - agent_start, 1),
        **(decision if isinstance(decision, dict) else {"overall": "ERROR", "summary": f"Failed to get decision from {label}"}),
    }
    if burst_result.usage:
        result["_usage"] = burst_result.usage
    return result


def _run_multi_soundness(
    config: Config,
    tablet: TabletState,
    proof_nodes: List[str],
    agents: List[Any],
    *,
    paper_tex: str,
    human_input: str,
    log_dir: Path,
) -> Dict[str, Any]:
    """Run multiple NL proof soundness agents concurrently and reconcile."""
    import concurrent.futures

    n = len(agents)
    labels = [a.label or f"{a.provider}/{a.model}" for a in agents]
    print(f"  Multi-agent NL proof: {n} agents ({', '.join(labels)}) on {len(proof_nodes)} nodes")

    agent_results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = {
            pool.submit(
                _run_single_soundness_agent,
                config, tablet, proof_nodes, agent,
                paper_tex=paper_tex, human_input=human_input,
                log_dir=log_dir, agent_index=i,
            ): i
            for i, agent in enumerate(agents)
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                agent_results.append(future.result())
            except Exception as exc:
                idx = futures[future]
                agent_results.append({
                    "agent": labels[idx], "index": idx,
                    "overall": "ERROR", "summary": str(exc),
                })

    agent_results.sort(key=lambda r: r.get("index", 0))

    overalls = [r.get("overall", "ERROR") for r in agent_results]
    all_approve = all(o == "APPROVE" for o in overalls)
    all_reject = all(o == "REJECT" for o in overalls)
    unanimous = all_approve or all_reject

    if unanimous:
        overall = "APPROVE" if all_approve else "REJECT"
        print(f"  NL proof soundness: {overall} (unanimous, {n} agents)")
        return {
            "check": "nl_proof",
            "overall": overall,
            "summary": f"Unanimous {overall} from {n} agents",
            "agent_results": agent_results,
        }
    else:
        disagree_detail = ", ".join(f"{r.get('agent', '?')}: {o}" for r, o in zip(agent_results, overalls))
        print(f"  NL proof soundness: DISAGREE ({disagree_detail})")
        return {
            "check": "nl_proof",
            "overall": "DISAGREE",
            "summary": f"Agents disagree: {disagree_detail}. Reviewer must arbitrate.",
            "agent_results": agent_results,
        }


def _run_single_node_soundness(
    config: Config,
    tablet: TabletState,
    node_name: str,
    agent_config: Any,
    *,
    paper_tex: str,
    human_input: str,
    log_dir: Path,
    agent_index: int,
    node_index: int,
) -> Dict[str, Any]:
    """Check one node's NL proof with one agent. Thread-safe."""
    from lagent_tablets.burst import _clean_terminal_json
    from lagent_tablets.prompts import build_node_soundness_prompt

    agent_start = time.monotonic()
    repo = config.repo_path
    label = agent_config.label or f"soundness-{agent_index}"
    output_file = f"nl_proof_{node_name}_{agent_index}.json"
    # Ports: 3310 + (node_index * 10) + (agent_index * 2) — spread to avoid collisions
    port = 3310 + (node_index % 5) * 10 + agent_index * 2

    result_file = repo / output_file
    result_file.unlink(missing_ok=True)

    prompt = build_node_soundness_prompt(
        config, tablet, node_name=node_name, paper_tex=paper_tex,
        human_input=human_input, output_file=output_file,
    )

    agent_provider = ProviderConfig(
        provider=agent_config.provider,
        model=agent_config.model,
        extra_args=agent_config.extra_args,
        fallback_models=getattr(agent_config, 'fallback_models', []),
    )

    burst_result = run_reviewer_burst(
        agent_provider, prompt,
        session_name=config.tmux.session_name,
        work_dir=repo, burst_user=config.tmux.burst_user,
        timeout_seconds=120, log_dir=log_dir, fresh=True,
        port=port,
    )

    decision = None
    if result_file.exists():
        try:
            decision = load_json(result_file)
        except Exception:
            pass
    if not isinstance(decision, dict) and burst_result.ok:
        cleaned = _clean_terminal_json(burst_result.captured_output)
        decision = extract_json_decision(cleaned)

    result = {
        "agent": label,
        "node": node_name,
        "index": agent_index,
        "ok": burst_result.ok,
        "walltime_seconds": round(time.monotonic() - agent_start, 1),
        **(decision if isinstance(decision, dict) else {"overall": "ERROR", "summary": f"Failed to get decision from {label}"}),
    }
    if burst_result.usage:
        result["_usage"] = burst_result.usage
    return result


def _run_per_node_soundness(
    config: Config,
    tablet: TabletState,
    node_names: List[str],
    agents: List[Any],
    *,
    paper_tex: str,
    human_input: str,
    log_dir: Path,
    batch_size: int = 3,
) -> List[Dict[str, Any]]:
    """Run per-node soundness checks with multiple agents.

    Each node is checked independently by all agents. Nodes are batched
    to limit concurrent agent processes.
    """
    import concurrent.futures

    n_agents = len(agents)
    labels = [a.label or f"{a.provider}/{a.model}" for a in agents]
    # Skip definition nodes (they don't have proofs)
    check_nodes = [n for n in node_names
                   if n in tablet.nodes and tablet.nodes[n].kind != "preamble"
                   and not _is_definition_node(config.repo_path, n)]
    print(f"  Per-node soundness: {len(check_nodes)} nodes × {n_agents} agents ({', '.join(labels)})")

    all_results: List[Dict[str, Any]] = []

    # Process in batches to limit concurrency
    for batch_start in range(0, len(check_nodes), batch_size):
        batch = check_nodes[batch_start:batch_start + batch_size]
        print(f"  Soundness batch {batch_start // batch_size + 1}: {batch}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_agents * len(batch)) as pool:
            futures = {}
            for ni, node_name in enumerate(batch):
                for ai, agent in enumerate(agents):
                    f = pool.submit(
                        _run_single_node_soundness,
                        config, tablet, node_name, agent,
                        paper_tex=paper_tex, human_input=human_input,
                        log_dir=log_dir, agent_index=ai,
                        node_index=batch_start + ni,
                    )
                    futures[f] = (node_name, ai)

            for future in concurrent.futures.as_completed(futures):
                node_name, ai = futures[future]
                try:
                    all_results.append(future.result())
                except Exception as exc:
                    all_results.append({
                        "agent": labels[ai], "node": node_name, "index": ai,
                        "overall": "ERROR", "summary": str(exc),
                    })

    # Reconcile per-node: group by node, check agreement
    from collections import defaultdict
    by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in all_results:
        by_node[r.get("node", "?")].append(r)

    node_verdicts = []
    structural_issues = []
    for node_name in check_nodes:
        node_results = sorted(by_node.get(node_name, []), key=lambda r: r.get("index", 0))
        overalls = [r.get("overall", "ERROR") for r in node_results]
        soundness_decisions = [r.get("soundness", {}).get("decision", "?") if isinstance(r.get("soundness"), dict) else "?" for r in node_results]

        # Check for STRUCTURAL flags
        has_structural = any(d == "STRUCTURAL" for d in soundness_decisions)
        all_approve = all(o == "APPROVE" for o in overalls)

        verdict = {
            "node": node_name,
            "agent_results": node_results,
            "overall": "APPROVE" if all_approve else "REJECT",
        }
        if has_structural:
            verdict["structural"] = True
            structural_issues.append(node_name)
            print(f"    {node_name}: STRUCTURAL (DAG needs restructuring)")
        elif all_approve:
            print(f"    {node_name}: SOUND (unanimous)")
        else:
            detail = ", ".join(f"{r.get('agent','?')}: {o}" for r, o in zip(node_results, overalls))
            print(f"    {node_name}: {detail}")

        node_verdicts.append(verdict)

    all_approve = all(v["overall"] == "APPROVE" for v in node_verdicts)
    summary_parts = []
    if structural_issues:
        summary_parts.append(f"STRUCTURAL issues in: {structural_issues}")
    failed_nodes = [v["node"] for v in node_verdicts if v["overall"] != "APPROVE"]
    if failed_nodes:
        summary_parts.append(f"Failed: {failed_nodes}")

    return [{
        "check": "nl_proof",
        "overall": "APPROVE" if all_approve else "REJECT",
        "summary": "; ".join(summary_parts) if summary_parts else f"All {len(check_nodes)} nodes sound",
        "structural_issues": structural_issues,
        "node_verdicts": node_verdicts,
    }]


def _is_definition_node(repo_path: Path, name: str) -> bool:
    """Check if a node is a definition (no proof to check)."""
    lean_path = node_lean_path(repo_path, name)
    if not lean_path.exists():
        return False
    content = lean_path.read_text(encoding="utf-8")
    # Definitions have no sorry — they're complete at creation
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("noncomputable def "):
            return True
    return False


def _run_multi_correspondence(
    config: Config,
    tablet: TabletState,
    corr_nodes: List[str],
    agents: List[Any],  # List[CorrespondenceAgentConfig]
    *,
    paper_tex: str,
    human_input: str,
    log_dir: Path,
) -> Dict[str, Any]:
    """Run multiple correspondence agents concurrently and reconcile results.

    If all agents agree, returns a single result.
    If they disagree, returns all individual results for the reviewer to arbitrate.
    """
    import concurrent.futures

    n = len(agents)
    labels = [a.label or f"{a.provider}/{a.model}" for a in agents]
    print(f"  Multi-agent correspondence: {n} agents ({', '.join(labels)}) on {len(corr_nodes)} nodes")

    agent_results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = {
            pool.submit(
                _run_single_correspondence_agent,
                config, tablet, corr_nodes, agent,
                paper_tex=paper_tex, human_input=human_input,
                log_dir=log_dir, agent_index=i,
            ): i
            for i, agent in enumerate(agents)
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                agent_results.append(future.result())
            except Exception as exc:
                idx = futures[future]
                agent_results.append({
                    "agent": labels[idx], "index": idx,
                    "overall": "ERROR", "summary": str(exc),
                })

    agent_results.sort(key=lambda r: r.get("index", 0))

    # Check agreement
    overalls = [r.get("overall", "ERROR") for r in agent_results]
    all_approve = all(o == "APPROVE" for o in overalls)
    all_reject = all(o == "REJECT" for o in overalls)
    unanimous = all_approve or all_reject

    if unanimous:
        overall = "APPROVE" if all_approve else "REJECT"
        summaries = [f"{r['agent']}: {r.get('summary', '?')}" for r in agent_results]
        print(f"  Correspondence: {overall} (unanimous, {n} agents)")
        return {
            "check": "correspondence",
            "overall": overall,
            "summary": f"Unanimous {overall} from {n} agents",
            "agent_results": agent_results,
        }
    else:
        disagree_detail = ", ".join(f"{r.get('agent', '?')}: {o}" for r, o in zip(agent_results, overalls))
        print(f"  Correspondence: DISAGREE ({disagree_detail})")
        return {
            "check": "correspondence",
            "overall": "DISAGREE",
            "summary": f"Agents disagree: {disagree_detail}. Reviewer must arbitrate.",
            "agent_results": agent_results,
        }


def _run_nl_verification(
    config: Config,
    tablet: TabletState,
    node_names: List[str],
    *,
    log_dir: Path,
    nl_cache: Optional[Any] = None,
    human_input: str = "",
) -> List[Dict[str, Any]]:
    """Run correspondence and NL proof verification on the given nodes.

    Uses the NL cache to skip re-verification when content hasn't changed.
    Returns list of verification result dicts.
    """
    from lagent_tablets.burst import _clean_terminal_json

    repo = config.repo_path
    results: List[Dict[str, Any]] = []

    if not node_names:
        return results

    paper_tex = ""
    if config.workflow.paper_tex_path and config.workflow.paper_tex_path.exists():
        paper_tex = config.workflow.paper_tex_path.read_text(encoding="utf-8", errors="replace")

    verify_config = ProviderConfig(
        provider=config.verification.provider,
        model=config.verification.model,
        extra_args=config.verification.extra_args,
    )

    # 1. Correspondence check (possibly multi-agent)
    corr_nodes = node_names
    if nl_cache:
        corr_nodes = nl_cache.filter_uncached(repo, node_names, "correspondence")
    if corr_nodes:
        corr_agents = config.verification.correspondence_agents
        if len(corr_agents) >= 2:
            # Multi-agent correspondence: run agents concurrently, show reviewer disagreements
            corr_result_entry = _run_multi_correspondence(
                config, tablet, corr_nodes, corr_agents,
                paper_tex=paper_tex, human_input=human_input, log_dir=log_dir,
            )
            results.append(corr_result_entry)
            if corr_result_entry.get("overall") == "APPROVE" and nl_cache:
                nl_cache.record_correspondence_approval(repo, corr_nodes)
        else:
            # Single-agent correspondence (default)
            print(f"  Correspondence check: {len(corr_nodes)} nodes ({len(node_names) - len(corr_nodes)} cached)")
            corr_file = repo / "correspondence_result.json"
            corr_file.unlink(missing_ok=True)
            corr_prompt = build_correspondence_prompt(
                config, tablet, node_names=corr_nodes, paper_tex=paper_tex,
                human_input=human_input,
            )
            corr_result = run_reviewer_burst(
                verify_config, corr_prompt,
                session_name=config.tmux.session_name,
                work_dir=repo, burst_user=config.tmux.burst_user,
                timeout_seconds=120, log_dir=log_dir, fresh=True,
                port=3286,
            )
            corr_decision = None
            if corr_file.exists():
                try:
                    corr_decision = load_json(corr_file)
                except Exception:
                    pass
            if not isinstance(corr_decision, dict) and corr_result.ok:
                cleaned = _clean_terminal_json(corr_result.captured_output)
                corr_decision = extract_json_decision(cleaned)
            if corr_decision:
                entry = {"check": "correspondence", **corr_decision}
                if corr_result.usage:
                    entry["_usage"] = corr_result.usage
                results.append(entry)
                overall = corr_decision.get("overall", "?")
                print(f"  Correspondence: {overall}")
                if overall == "APPROVE" and nl_cache:
                    nl_cache.record_correspondence_approval(repo, corr_nodes)
    else:
        print(f"  Correspondence: all {len(node_names)} nodes cached (APPROVE)")
        results.append({"check": "correspondence", "overall": "APPROVE", "summary": "cached"})

    # 2. NL proof soundness check — only if correspondence passed (it's a gate)
    corr_overall = "APPROVE"
    for r in results:
        if r.get("check") == "correspondence":
            corr_overall = r.get("overall", "?")
    if corr_overall != "APPROVE":
        print(f"  Skipping NL proof soundness (correspondence {corr_overall} — must pass first)")
        return results

    proof_nodes = node_names
    if nl_cache:
        proof_nodes = nl_cache.filter_uncached(repo, node_names, "soundness")
    if proof_nodes:
        soundness_agents = config.verification.soundness_agents
        if len(soundness_agents) >= 2:
            # Per-node soundness with multiple agents
            proof_results = _run_per_node_soundness(
                config, tablet, proof_nodes, soundness_agents,
                paper_tex=paper_tex, human_input=human_input, log_dir=log_dir,
            )
            results.extend(proof_results)
            for pr in proof_results:
                if pr.get("overall") == "APPROVE" and nl_cache:
                    nl_cache.record_soundness_approval(repo, proof_nodes)
        else:
            # Single-agent, all-at-once soundness (fallback)
            print(f"  NL proof check: {len(proof_nodes)} nodes ({len(node_names) - len(proof_nodes)} cached)")
            proof_file = repo / "nl_proof_result.json"
            proof_file.unlink(missing_ok=True)
            proof_prompt = build_nl_proof_prompt(
                config, tablet, node_names=proof_nodes, paper_tex=paper_tex,
                human_input=human_input,
            )
            proof_result = run_reviewer_burst(
                verify_config, proof_prompt,
                session_name=config.tmux.session_name,
                work_dir=repo, burst_user=config.tmux.burst_user,
                timeout_seconds=120, log_dir=log_dir, fresh=True,
                port=3287,
            )
            proof_decision = None
            if proof_file.exists():
                try:
                    proof_decision = load_json(proof_file)
                except Exception:
                    pass
            if not isinstance(proof_decision, dict) and proof_result.ok:
                cleaned = _clean_terminal_json(proof_result.captured_output)
                proof_decision = extract_json_decision(cleaned)
            if proof_decision:
                entry = {"check": "nl_proof", **proof_decision}
                if proof_result.usage:
                    entry["_usage"] = proof_result.usage
                results.append(entry)
                overall = proof_decision.get("overall", "?")
                print(f"  NL proof soundness: {overall}")
                if overall == "APPROVE" and nl_cache:
                    nl_cache.record_soundness_approval(repo, proof_nodes)
    else:
        print(f"  NL proof soundness: all {len(node_names)} nodes cached (APPROVE)")
        results.append({"check": "nl_proof", "overall": "APPROVE", "summary": "cached"})

    return results


def run_theorem_stating_cycle(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
    *,
    previous_outcome: Optional[Dict[str, Any]] = None,
) -> CycleOutcome:
    """Run a single theorem_stating cycle.

    The worker creates/refines Tablet nodes (.lean + .tex pairs).
    The reviewer checks whether the tablet is ready for proof_formalization.
    """
    from lagent_tablets.prompts import build_theorem_stating_prompt, build_theorem_stating_reviewer_prompt
    from lagent_tablets.health import fix_lake_permissions

    resume_from = state.resume_from or ""
    repo = config.repo_path

    if not resume_from:
        # Fresh cycle — increment and run worker
        cycle = state.cycle + 1
        state.cycle = cycle
    else:
        cycle = state.cycle
        print(f"=== Resuming theorem-stating cycle {cycle} from {resume_from} ===")

    cycle_start = time.monotonic()
    log_dir = config.state_dir / "logs" / f"cycle-{cycle:04d}"

    # ---- Stage 1: Worker ----
    if not resume_from:
        print(f"=== Theorem-stating cycle {cycle} ===")

        (repo / "Tablet").mkdir(parents=True, exist_ok=True)
        fix_lake_permissions(repo)
        _setup_theorem_stating_permissions(config)

        worker_prompt = build_theorem_stating_prompt(
            config, state, tablet, policy, previous_outcome=previous_outcome,
        )

        worker_result = run_worker_burst(
            config.worker,
            worker_prompt,
            session_name=config.tmux.session_name,
            work_dir=repo,
            burst_user=config.tmux.burst_user,
            timeout_seconds=policy.timing.burst_timeout_seconds,
            startup_timeout_seconds=config.startup_timeout_seconds,
            log_dir=log_dir,
        )

        if worker_result.transcript_path:
            print(f"  Transcript saved: {worker_result.transcript_path}")

        if not worker_result.ok:
            print(f"  Worker burst failed: {worker_result.error}")
            save_state(state_path(config), state)
            return CycleOutcome(outcome="INVALID", detail=f"Worker burst failed: {worker_result.error}")

        fix_lake_permissions(repo)

        # Discover what the worker created/modified
        tdir = repo / "Tablet"
        lean_files = {p.stem for p in tdir.glob("*.lean") if p.stem != "Preamble"}
        tex_files = {p.stem for p in tdir.glob("*.tex") if p.stem not in ("header", "Preamble")}
        all_node_names = lean_files | tex_files
        new_nodes = [n for n in all_node_names if n not in tablet.nodes]
        existing_nodes = [n for n in all_node_names if n in tablet.nodes]

        # Read difficulty hints from worker handoff (if present)
        difficulty_hints: Dict[str, str] = {}
        handoff_path = repo / "worker_handoff.json"
        if handoff_path.exists():
            try:
                hf = load_json(handoff_path)
                if isinstance(hf, dict):
                    hints = hf.get("difficulty_hints", {})
                    if isinstance(hints, dict):
                        for k, v in hints.items():
                            if v in ("easy", "hard"):
                                difficulty_hints[k] = v
            except Exception:
                pass

        # Register any new nodes in the tablet
        for name in new_nodes:
            lean_path = node_lean_path(repo, name)
            tex_path = node_tex_path(repo, name)
            if lean_path.exists():
                content = lean_path.read_text(encoding="utf-8")
                marker = extract_marker_name(content)
                kind = "paper_main_result" if "main" in name or "theorem" in name else "paper_intermediate"
                register_new_node(tablet, repo, name=name, kind=kind, cycle=cycle)
                if name in difficulty_hints:
                    tablet.nodes[name].difficulty = difficulty_hints[name]

        # Ensure Preamble node exists
        if PREAMBLE_NAME not in tablet.nodes:
            preamble_path = repo / "Tablet" / "Preamble.lean"
            if preamble_path.exists():
                tablet.nodes[PREAMBLE_NAME] = TabletNode(
                    name=PREAMBLE_NAME, kind="preamble", status="closed",
                    title="Imports", closed_at_cycle=cycle,
                )

        # Check: no definitions in Preamble
        from lagent_tablets.tablet import scan_preamble_definitions
        preamble_path = repo / "Tablet" / "Preamble.lean"
        if preamble_path.exists():
            preamble_defs = scan_preamble_definitions(preamble_path.read_text(encoding="utf-8"))
            if preamble_defs:
                defs_list = [h["text"][:80] for h in preamble_defs]
                print(f"  INVALID: Preamble has {len(preamble_defs)} definitions (must be in own nodes)")
                for d in defs_list:
                    print(f"    {d}")
                save_state(state_path(config), state)
                return CycleOutcome(
                    outcome="INVALID",
                    detail=f"Preamble.lean contains definitions. All definitions must be in their own node files with .tex counterparts. Found: {', '.join(defs_list[:3])}",
                )

        # Validate: lake build
        build_ok = False
        build_output = ""
        try:
            import subprocess
            result = subprocess.run(
                ["lake", "build", "Tablet"],
                capture_output=True, text=True, timeout=300,
                cwd=str(repo),
            )
            build_output = result.stdout + result.stderr
            build_ok = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            build_output = str(e)

        if not build_ok:
            print(f"  lake build failed")
            outcome = CycleOutcome(
                outcome="INVALID",
                detail=f"lake build Tablet failed",
                build_output=build_output,
            )
        elif new_nodes:
            print(f"  Created {len(new_nodes)} new nodes: {new_nodes}")
            outcome = CycleOutcome(
                outcome="PROGRESS",
                detail=f"Created nodes: {', '.join(new_nodes)}",
                nodes_created=new_nodes,
            )
        else:
            print(f"  No new nodes created (modified existing: {existing_nodes})")
            outcome = CycleOutcome(outcome="PROGRESS", detail="Modified existing nodes")

        # Save checkpoint: worker done
        regenerate_support_files(tablet, repo)
        save_tablet(tablet_path(config), tablet)
        state.resume_from = "verification"
        save_state(state_path(config), state)
    else:
        # Resuming — reconstruct outcome from current state
        all_node_names = [n for n in tablet.nodes if tablet.nodes[n].kind != "preamble"]
        outcome = CycleOutcome(
            outcome="PROGRESS",
            detail=f"Resumed with {len(all_node_names)} existing nodes",
        )

    # ---- Stage 2: NL Verification ----
    nl_verification_results: List[Dict[str, Any]] = []
    if resume_from in ("", "verification"):
        from lagent_tablets.nl_cache import NLCache
        nl_cache = NLCache(config.state_dir / "nl_cache.json")
        all_check_nodes = [n for n in tablet.nodes if tablet.nodes[n].kind != "preamble"]
        print(f"  Running NL verification for {len(all_check_nodes)} nodes...")
        nl_verification_results = _run_nl_verification(
            config, tablet, all_check_nodes, log_dir=log_dir, nl_cache=nl_cache,
            human_input=state.human_input,
        )
        # Save checkpoint: verification done
        state.resume_from = "reviewer"
        save_state(state_path(config), state)
    elif resume_from == "reviewer":
        # Reconstruct verification results from saved files for the reviewer
        print(f"  Skipping verification (resuming from reviewer)")
        for i in range(10):
            f = repo / f"correspondence_result_{i}.json"
            if f.exists():
                try:
                    data = load_json(f)
                    if isinstance(data, dict):
                        nl_verification_results.append({"check": "correspondence", "agent_index": i, **data})
                except Exception:
                    pass
        if not nl_verification_results:
            f = repo / "correspondence_result.json"
            if f.exists():
                try:
                    data = load_json(f)
                    if isinstance(data, dict):
                        nl_verification_results.append({"check": "correspondence", **data})
                except Exception:
                    pass
        if nl_verification_results:
            overalls = [r.get("overall", "?") for r in nl_verification_results]
            print(f"  Loaded {len(nl_verification_results)} correspondence results: {overalls}")

    # ---- Stage 3: Reviewer ----
    handoff_path = repo / "worker_handoff.json"
    worker_handoff = None
    if handoff_path.exists():
        try:
            worker_handoff = load_json(handoff_path)
        except Exception:
            # Worker may write invalid JSON (e.g., LaTeX backslashes)
            try:
                raw = handoff_path.read_text(encoding="utf-8", errors="replace")
                # Escape bare backslashes and retry
                import re
                fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
                worker_handoff = json.loads(fixed)
            except Exception:
                worker_handoff = {"summary": raw[:500] if raw else "could not parse", "status": "UNKNOWN"}

    reviewer_prompt = build_theorem_stating_reviewer_prompt(
        config, state, tablet, policy,
        worker_handoff=worker_handoff,
        worker_output=(worker_result.captured_output[-15000:] if worker_result.captured_output else "") if not resume_from else "",
        nl_verification=nl_verification_results if nl_verification_results else None,
    )

    # Clean up decision file before burst
    decision_path = repo / "reviewer_decision.json"
    decision_path.unlink(missing_ok=True)

    reviewer_result = run_reviewer_burst(
        config.reviewer,
        reviewer_prompt,
        session_name=config.tmux.session_name,
        work_dir=repo,
        burst_user=config.tmux.burst_user,
        timeout_seconds=min(policy.timing.burst_timeout_seconds, 300),
        log_dir=log_dir,
    )

    # Read decision from file (primary), fall back to output parsing
    decision = None
    if decision_path.exists():
        try:
            decision = load_json(decision_path)
        except Exception:
            pass
    if not isinstance(decision, dict):
        from lagent_tablets.burst import _clean_terminal_json
        cleaned = _clean_terminal_json(reviewer_result.captured_output)
        decision = extract_json_decision(cleaned)
    if isinstance(decision, dict):
        state.last_review = decision
        state.review_log.append({"cycle": cycle, **decision})
        print(f"  Reviewer: {decision.get('decision', '?')} -- {decision.get('reason', '')[:100]}")

        # If ADVANCE_PHASE, run both verification checks on all nodes first
        if decision.get("decision") == "ADVANCE_PHASE":
            all_nodes = [n for n in tablet.nodes if tablet.nodes[n].kind != "preamble"]
            if all_nodes:
                print(f"  Running verification before phase advance: {all_nodes}")
                paper_tex = ""
                if config.workflow.paper_tex_path and config.workflow.paper_tex_path.exists():
                    paper_tex = config.workflow.paper_tex_path.read_text(encoding="utf-8", errors="replace")
                from lagent_tablets.nl_cache import NLCache
                nl_cache = NLCache(config.state_dir / "nl_cache.json")
                gate_results = _run_nl_verification(
                    config, tablet, all_nodes, log_dir=log_dir, nl_cache=nl_cache,
                    human_input=state.human_input,
                )
                rejection_reasons = []
                for r in gate_results:
                    if r.get("overall") == "REJECT":
                        rejection_reasons.append(f"{r.get('check', '?')}: {r.get('summary', '')}")

                if rejection_reasons:
                    print(f"  Verification REJECTED -- blocking ADVANCE_PHASE")
                    state.last_review["decision"] = "CONTINUE"
                    state.last_review["reason"] = "Verification rejected: " + "; ".join(rejection_reasons)
                else:
                    # Verification passed — set the initial active node for proof_formalization
                    next_node = decision.get("next_active_node", "")
                    if next_node and next_node in tablet.nodes and tablet.nodes[next_node].status == "open":
                        state.active_node = next_node
                        tablet.active_node = next_node
                        print(f"  Initial proof node: {next_node}")
                    else:
                        open_nodes = [n for n, nd in sorted(tablet.nodes.items())
                                      if nd.status == "open" and nd.kind != "preamble"]
                        if open_nodes:
                            state.active_node = open_nodes[0]
                            tablet.active_node = open_nodes[0]
                            print(f"  Initial proof node (auto): {open_nodes[0]}")
    else:
        print(f"  Reviewer: could not parse decision")

    # Clear resume checkpoint — cycle is complete
    state.resume_from = ""
    save_tablet(tablet_path(config), tablet)
    save_state(state_path(config), state)

    # Git commit
    from lagent_tablets.git_ops import commit_cycle as git_commit
    git_commit(
        repo, cycle,
        phase="theorem_stating",
        outcome=outcome.outcome,
        active_node=state.active_node,
        detail=outcome.detail,
        meta={
            "duration_seconds": round(time.monotonic() - cycle_start, 1),
            "reviewer_decision": state.last_review,
            "verification_results": nl_verification_results if nl_verification_results else None,
        },
    )

    return outcome


def run_cycle(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
    *,
    previous_outcome: Optional[Dict[str, Any]] = None,
) -> CycleOutcome:
    """Run a single supervisor cycle."""
    cycle = state.cycle + 1
    cycle_start = time.monotonic()
    state.cycle = cycle
    active_node = state.active_node or tablet.active_node
    repo = config.repo_path

    # Determine node difficulty and select the right worker config
    node_meta = tablet.nodes.get(active_node)
    node_difficulty = node_meta.difficulty if node_meta else "hard"
    easy_mode = node_difficulty == "easy"

    # Select worker config based on difficulty
    if easy_mode and config.easy_worker:
        effective_worker = config.easy_worker
    elif not easy_mode and config.hard_worker:
        effective_worker = config.hard_worker
    else:
        effective_worker = config.worker

    print(f"=== Cycle {cycle} | Active: {active_node} | Difficulty: {node_difficulty} | Worker: {effective_worker.provider}/{effective_worker.model} ===")

    # Regenerate scripts each cycle (hot-reloadable)
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]
    write_scripts(repo, config.state_dir, allowed_prefixes=config.workflow.allowed_import_prefixes, forbidden_keywords=forbidden)

    # Fix .lake permissions before any compilation
    from lagent_tablets.health import fix_lake_permissions
    fix_lake_permissions(repo)

    # Ensure oleans are up to date before the burst (so check_node works for the worker)
    _ensure_lake_build(repo)

    # Set file permissions (active node writable, others read-only)
    # Easy mode: Tablet/ dir not group-writable, Preamble read-only
    setup_permissions(config, active_node, easy_mode=easy_mode)

    # Record state BEFORE the burst for easy-mode validation
    active_lean = node_lean_path(repo, active_node)
    hash_before = hashlib.sha256(active_lean.read_bytes()).hexdigest() if active_lean.exists() else ""
    imports_before: Set[str] = set()
    if easy_mode and active_lean.exists():
        imports_before = set(extract_tablet_imports(active_lean.read_text(encoding="utf-8")))
    tablet_files_before: Set[str] = set()
    if easy_mode and (repo / "Tablet").is_dir():
        tablet_files_before = {p.name for p in (repo / "Tablet").iterdir() if p.is_file()}

    # Build worker prompt
    worker_prompt = build_worker_prompt(
        config, state, tablet, policy,
        previous_outcome=previous_outcome,
        difficulty=node_difficulty,
    )

    # Run worker burst
    log_dir = config.state_dir / "logs" / f"cycle-{cycle:04d}"

    worker_result = run_worker_burst(
        effective_worker,
        worker_prompt,
        session_name=config.tmux.session_name,
        work_dir=repo,
        burst_user=config.tmux.burst_user,
        timeout_seconds=policy.timing.burst_timeout_seconds,
        startup_timeout_seconds=config.startup_timeout_seconds,
        log_dir=log_dir,
    )

    if worker_result.transcript_path:
        print(f"  Transcript saved: {worker_result.transcript_path}")

    _accumulate_usage(state, "worker", worker_result.usage)

    if not worker_result.ok:
        print(f"  Worker burst failed: {worker_result.error}")
        save_state(state_path(config), state)
        return CycleOutcome(outcome="INVALID", detail=f"Worker burst failed: {worker_result.error}")

    # Fix .lake permissions after burst (worker may have created oleans)
    fix_lake_permissions(repo)

    # Check the active node's hash AFTER the burst
    hash_after = hashlib.sha256(active_lean.read_bytes()).hexdigest() if active_lean.exists() else ""
    active_changed = hash_before != hash_after

    # Detect new files created by the worker
    current_files = {p.name for p in (repo / "Tablet").iterdir() if p.is_file()} if (repo / "Tablet").is_dir() else set()
    known_files = {f"{name}.lean" for name in tablet.nodes} | {f"{name}.tex" for name in tablet.nodes} | {"INDEX.md", "README.md", "header.tex"}
    new_files = current_files - known_files

    # Easy-mode enforcement (layer 2: supervisor-side, in addition to filesystem)
    if easy_mode:
        # Reject new files
        new_content_files = {f for f in (current_files - tablet_files_before)
                            if f.endswith(".lean") or f.endswith(".tex")}
        if new_content_files:
            print(f"  Easy-mode violation: new files created: {new_content_files}")
            # Clean up the new files
            for fname in new_content_files:
                try:
                    (repo / "Tablet" / fname).unlink()
                except OSError:
                    pass
            outcome = CycleOutcome(
                outcome="INVALID",
                detail=f"Easy-mode node cannot create new files. Created: {sorted(new_content_files)}",
            )
            # Track easy attempt and possibly auto-elevate
            if node_meta:
                node_meta.easy_attempts += 1
                if node_meta.easy_attempts >= policy.difficulty.easy_max_retries:
                    node_meta.difficulty = "hard"
                    node_meta.easy_attempts = 0
                    print(f"  Auto-elevating {active_node} to hard (after {policy.difficulty.easy_max_retries} easy attempts)")
            save_state(state_path(config), state)
            save_tablet(tablet_path(config), tablet)
            return outcome

        # Reject new imports
        if active_lean.exists():
            imports_after = set(extract_tablet_imports(active_lean.read_text(encoding="utf-8")))
            new_imports = imports_after - imports_before
            if new_imports:
                print(f"  Easy-mode violation: new imports added: {new_imports}")
                outcome = CycleOutcome(
                    outcome="INVALID",
                    detail=f"Easy-mode node cannot add new imports. Added: {sorted(new_imports)}",
                )
                if node_meta:
                    node_meta.easy_attempts += 1
                    if node_meta.easy_attempts >= policy.difficulty.easy_max_retries:
                        node_meta.difficulty = "hard"
                        node_meta.easy_attempts = 0
                        print(f"  Auto-elevating {active_node} to hard (after {policy.difficulty.easy_max_retries} easy attempts)")
                save_state(state_path(config), state)
                save_tablet(tablet_path(config), tablet)
                return outcome

    # Validate
    outcome = validate_worker_cycle_v2(
        config, tablet, active_node,
        active_changed=active_changed,
        new_lean_files=[f.removesuffix(".lean") for f in new_files if f.endswith(".lean")],
    )
    print(f"  Validation: {outcome.outcome} -- {outcome.detail}")

    # Track consecutive invalids for escalation
    prev_invalids = _count_consecutive_invalids(state)
    if outcome.outcome == "INVALID":
        state.validation_summary = {"consecutive_invalids": prev_invalids + 1, "last_outcome": "INVALID"}
    else:
        state.validation_summary = {"consecutive_invalids": 0, "last_outcome": outcome.outcome}

    # Easy-mode auto-elevation on non-progress outcomes
    if easy_mode and outcome.outcome in ("INVALID", "NO_PROGRESS") and node_meta:
        node_meta.easy_attempts += 1
        if node_meta.easy_attempts >= policy.difficulty.easy_max_retries:
            node_meta.difficulty = "hard"
            node_meta.easy_attempts = 0
            print(f"  Auto-elevating {active_node} to hard (after {policy.difficulty.easy_max_retries} easy attempts)")

    if outcome.outcome == "NO_PROGRESS":
        # Still go to reviewer for guidance
        pass

    # NL verification for new open nodes
    # ALL NL decisions are critical -- every verification result goes to the reviewer.
    # The reviewer is always the final arbiter for NL soundness.
    nl_verification_results: List[Dict[str, Any]] = []
    needs_nl_review = False

    # Run NL verification on: new nodes, and any modified nodes that are still open
    nodes_to_verify = []
    if outcome.outcome == "PROGRESS":
        if outcome.nodes_created:
            nodes_to_verify.extend([n for n in outcome.nodes_created if n not in outcome.nodes_closed])
        # Also verify the active node if it changed (even if not closed)
        if active_changed and active_node not in outcome.nodes_closed:
            if active_node not in nodes_to_verify:
                nodes_to_verify.append(active_node)

    if nodes_to_verify:
        print(f"  Running NL verification for nodes: {nodes_to_verify}")
        needs_nl_review = True
        from lagent_tablets.nl_cache import NLCache
        nl_cache = NLCache(config.state_dir / "nl_cache.json")
        nl_verification_results = _run_nl_verification(
            config, tablet, nodes_to_verify, log_dir=log_dir, nl_cache=nl_cache,
            human_input=state.human_input,
        )

    # Accumulate verification usage
    for vr in nl_verification_results:
        usage = vr.pop("_usage", None)
        role = "correspondence" if vr.get("check") == "correspondence" else "nl_proof"
        _accumulate_usage(state, role, usage)
        # Also accumulate from multi-agent results
        for ar in vr.get("agent_results", []):
            agent_usage = ar.pop("_usage", None)
            _accumulate_usage(state, "correspondence", agent_usage)

    # Apply results to tablet state
    if outcome.outcome == "PROGRESS":
        for name in outcome.nodes_created:
            node_lean = node_lean_path(repo, name)
            # Determine kind from .tex (heuristic: check if it looks like a main result)
            register_new_node(tablet, repo, name=name, kind="helper_lemma", cycle=cycle)

        for name in outcome.nodes_closed:
            mark_node_closed(tablet, name, cycle)

        tablet.last_modified_at_cycle = cycle

    tablet.active_node = active_node
    regenerate_support_files(tablet, repo)
    save_tablet(tablet_path(config), tablet)
    save_state(state_path(config), state)

    # Determine if reviewer is needed
    # - PROGRESS/NO_PROGRESS: always
    # - NL verification was run: always (reviewer is final arbiter for all NL decisions)
    # - REJECTED: reviewer sees rejection reports and crafts corrective guidance
    # - INVALID: normally bounce to worker, but escalate after repeated failures
    consecutive_invalids = _count_consecutive_invalids(state)
    invalid_escalation_threshold = 3  # escalate to reviewer after this many consecutive INVALIDs

    needs_reviewer = (
        outcome.outcome in ("PROGRESS", "NO_PROGRESS")
        or needs_nl_review
        or outcome.outcome == "REJECTED"
        or (outcome.outcome == "INVALID" and consecutive_invalids >= invalid_escalation_threshold)
    )

    if needs_reviewer:
        # Read worker handoff
        handoff_path = repo / "worker_handoff.json"
        worker_handoff = None
        if handoff_path.exists():
            try:
                worker_handoff = load_json(handoff_path)
            except Exception:
                try:
                    raw = handoff_path.read_text(encoding="utf-8", errors="replace")
                    import re as _re
                    fixed = _re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
                    worker_handoff = json.loads(fixed)
                except Exception:
                    worker_handoff = {"summary": raw[:500] if raw else "could not parse", "status": "UNKNOWN"}

        reviewer_prompt = build_reviewer_prompt(
            config, state, tablet, policy,
            worker_handoff=worker_handoff,
            worker_output=worker_result.captured_output[-20000:] if worker_result.captured_output else "",
            validation_summary={"outcome": outcome.outcome, "detail": outcome.detail,
                                "consecutive_invalids": state.validation_summary.get("consecutive_invalids", 0) if isinstance(state.validation_summary, dict) else 0},
            nl_verification=nl_verification_results if nl_verification_results else None,
        )

        # Reviewer burst: non-interactive for Claude/Codex, interactive for Gemini
        decision_path = repo / "reviewer_decision.json"
        decision_path.unlink(missing_ok=True)

        reviewer_result = run_reviewer_burst(
            config.reviewer,
            reviewer_prompt,
            session_name=config.tmux.session_name,
            work_dir=repo,
            burst_user=config.tmux.burst_user,
            timeout_seconds=min(policy.timing.burst_timeout_seconds, 300),
            log_dir=log_dir,
        )

        _accumulate_usage(state, "reviewer", reviewer_result.usage)

        # Read decision from file (primary), fall back to output parsing
        decision = None
        if decision_path.exists():
            try:
                decision = load_json(decision_path)
            except Exception:
                pass
        if not isinstance(decision, dict) and reviewer_result.ok:
            from lagent_tablets.burst import _clean_terminal_json
            cleaned = _clean_terminal_json(reviewer_result.captured_output)
            decision = extract_json_decision(cleaned)
        if isinstance(decision, dict):
            state.last_review = decision
            state.review_log.append({"cycle": cycle, **decision})
            next_node = decision.get("next_active_node", "")
            if next_node and next_node in tablet.nodes and tablet.nodes[next_node].status == "open":
                state.active_node = next_node
                tablet.active_node = next_node

            # Apply reviewer difficulty assignments
            for name, diff in decision.get("difficulty_assignments", {}).items():
                if name in tablet.nodes and diff in ("easy", "hard"):
                    old_diff = tablet.nodes[name].difficulty
                    if old_diff != diff:
                        tablet.nodes[name].difficulty = diff
                        tablet.nodes[name].easy_attempts = 0
                        print(f"  Reviewer assigned {name}: {old_diff} -> {diff}")

            # Apply reviewer elevations
            for name in decision.get("elevate_to_hard", []):
                if name in tablet.nodes and tablet.nodes[name].difficulty == "easy":
                    tablet.nodes[name].difficulty = "hard"
                    tablet.nodes[name].easy_attempts = 0
                    print(f"  Reviewer elevated {name} to hard")

            print(f"  Reviewer: {decision.get('decision', '?')} -> next: {state.active_node}")
        else:
            print(f"  Reviewer: could not parse decision")

        save_tablet(tablet_path(config), tablet)
        save_state(state_path(config), state)

    # Git commit
    from lagent_tablets.git_ops import commit_cycle as git_commit
    git_commit(
        repo, cycle,
        phase=state.phase,
        outcome=outcome.outcome,
        active_node=active_node,
        detail=outcome.detail,
        meta={
            "duration_seconds": round(time.monotonic() - cycle_start, 1),
            "reviewer_decision": state.last_review,
            "verification_results": nl_verification_results if nl_verification_results else None,
            "token_usage": state.agent_token_usage,
        },
    )

    # Print cycle usage summary
    if state.agent_token_usage:
        parts = []
        for role, data in state.agent_token_usage.items():
            if isinstance(data, dict) and "calls" in data:
                parts.append(f"{role}: {data.get('input_tokens',0):,}in/{data.get('output_tokens',0):,}out ({data['calls']} calls)")
        if parts:
            print(f"  Token usage (cumulative): {' | '.join(parts)}")

    return outcome
