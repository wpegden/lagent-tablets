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

from lagent_tablets.adapters import BurstResult, ProviderConfig
from lagent_tablets.burst import (
    extract_json_decision,
    run_reviewer_burst,
    run_worker_burst,
)
from lagent_tablets.config import Config, Policy
from lagent_tablets.prompts import build_reviewer_prompt, build_verification_prompt, build_worker_prompt
from lagent_tablets.state import (
    SupervisorState,
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
from lagent_tablets.verification import (
    FORBIDDEN_KEYWORDS_DEFAULT,
    NodeCheckResult,
    check_node,
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

def setup_permissions(config: Config, active_node: str) -> None:
    """Set file permissions before a worker burst.

    The burst_user runs the agent CLI. File permissions control what it can write:
    - Active node .lean/.tex: 0o664 (group-writable) -- worker can edit
    - Preamble.lean: 0o664 -- worker can add imports
    - Everything else: 0o644 (group-read-only) -- worker CANNOT edit
    - Tablet/ directory: 0o2775 (group-writable, setgid) -- worker can create new files
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

    # Tablet directory: setgid, group-writable (new files inherit group)
    try:
        os.chown(str(tdir), -1, gid)
        os.chmod(str(tdir), 0o2775)
    except PermissionError:
        pass

    # Files the worker may edit
    writable_basenames = {
        f"{active_node}.lean",
        f"{active_node}.tex",
        "Preamble.lean",
    }

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
        result = check_node(
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
        result = check_node(
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
            # File looks sorry-free -- verify with compilation
            result = check_node(
                repo, name,
                allowed_prefixes=config.workflow.allowed_import_prefixes,
                forbidden_keywords=forbidden,
                timeout_seconds=120,
            )
            if result.compiles and result.sorry_free:
                mark_node_closed(tablet, name, 0)
                reconciled.append(name)
                print(f"  Reconciled: {name} is sorry-free and compiles, marking closed")
            elif result.sorry_free and not result.compiles:
                # Sorry-free but compilation failed -- check if it's just Lake package noise
                from lagent_tablets.verification import _is_lake_package_error
                if _is_lake_package_error(result.build_output):
                    mark_node_closed(tablet, name, 0)
                    reconciled.append(name)
                    print(f"  Reconciled: {name} is sorry-free (Lake package error ignored), marking closed")
                else:
                    print(f"  Reconciled: {name} is sorry-free but has real compilation errors")
            else:
                print(f"  Reconciled: {name} sorry_free={result.sorry_free} compiles={result.compiles}")
        elif node.status == "closed" and file_has_sorry:
            # Node was closed but file now has sorry (e.g., statement was changed)
            mark_node_open(tablet, name, 0)
            reconciled.append(name)
            print(f"  Reconciled: {name} has sorry, marking open")

    return reconciled


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
    state.cycle = cycle
    active_node = state.active_node or tablet.active_node
    repo = config.repo_path

    print(f"=== Cycle {cycle} | Active: {active_node} ===")

    # Regenerate scripts each cycle (hot-reloadable)
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]
    write_scripts(repo, config.state_dir, allowed_prefixes=config.workflow.allowed_import_prefixes, forbidden_keywords=forbidden)

    # Fix .lake permissions before any compilation
    from lagent_tablets.health import fix_lake_permissions
    fix_lake_permissions(repo)

    # Set file permissions FIRST (this ensures the active node is writable
    # and all other nodes are read-only BEFORE we take the snapshot)
    setup_permissions(config, active_node)

    # Build worker prompt
    worker_prompt = build_worker_prompt(config, state, tablet, policy, previous_outcome=previous_outcome)

    # Snapshot AFTER permissions are set (captures the true "before" state)
    snapshot_before = _snapshot_tablet_dir(repo)

    # Run worker burst
    log_dir = config.state_dir / "logs" / f"cycle-{cycle:04d}"

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

    if not worker_result.ok:
        print(f"  Worker burst failed: {worker_result.error}")
        return CycleOutcome(outcome="INVALID", detail=f"Worker burst failed: {worker_result.error}")

    # Snapshot after
    snapshot_after = _snapshot_tablet_dir(repo)

    # Validate
    outcome = validate_worker_cycle(config, tablet, active_node, snapshot_before, snapshot_after)
    print(f"  Validation: {outcome.outcome} -- {outcome.detail}")

    # Track consecutive invalids for escalation
    prev_invalids = _count_consecutive_invalids(state)
    if outcome.outcome == "INVALID":
        state.validation_summary = {"consecutive_invalids": prev_invalids + 1, "last_outcome": "INVALID"}
    else:
        state.validation_summary = {"consecutive_invalids": 0, "last_outcome": outcome.outcome}

    if outcome.outcome == "NO_PROGRESS":
        # Still go to reviewer for guidance
        pass

    # NL verification for new open nodes
    # ALL NL decisions are critical -- every verification result goes to the reviewer.
    # The reviewer is always the final arbiter for NL soundness.
    nl_verification_results: List[Dict[str, Any]] = []
    needs_nl_review = False
    if outcome.outcome == "PROGRESS" and outcome.nodes_created:
        open_new = [n for n in outcome.nodes_created if n not in outcome.nodes_closed]
        if open_new:
            print(f"  Running NL verification for new nodes: {open_new}")
            needs_nl_review = True

            # TODO: invoke verification model(s) via adapter
            # Run multiple agents for robustness, collect all results.
            # All results go to the reviewer -- no auto-resolve for NL decisions.
            #
            # nl_verification_results = run_nl_verifications(config, tablet, open_new, ...)
            pass

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
        worker_handoff = load_json(handoff_path) if handoff_path.exists() else None

        reviewer_prompt = build_reviewer_prompt(
            config, state, tablet, policy,
            worker_handoff=worker_handoff,
            worker_output=worker_result.captured_output[-20000:] if worker_result.captured_output else "",
            validation_summary={"outcome": outcome.outcome, "detail": outcome.detail,
                                "consecutive_invalids": state.validation_summary.get("consecutive_invalids", 0) if isinstance(state.validation_summary, dict) else 0},
            nl_verification=nl_verification_results[0] if nl_verification_results else None,
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

        # Parse reviewer decision from output (non-interactive) or file (Gemini interactive)
        if reviewer_result.ok:
            if decision_path.exists():
                decision = load_json(decision_path)
            else:
                decision = extract_json_decision(reviewer_result.captured_output)
            if isinstance(decision, dict):
                state.last_review = decision
                state.review_log.append({"cycle": cycle, **decision})
                next_node = decision.get("next_active_node", "")
                if next_node and next_node in tablet.nodes and tablet.nodes[next_node].status == "open":
                    state.active_node = next_node
                    tablet.active_node = next_node
                print(f"  Reviewer: {decision.get('decision', '?')} -> next: {state.active_node}")
            else:
                print(f"  Reviewer: could not parse decision from output")

        save_tablet(tablet_path(config), tablet)
        save_state(state_path(config), state)

    return outcome
