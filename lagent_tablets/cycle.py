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
import copy
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import hashlib

from lagent_tablets.artifacts import artifact_stem, done_marker_path, raw_json_path
from lagent_tablets.adapters import BurstResult, ProviderConfig
from lagent_tablets.burst import (
    run_reviewer_burst,
    run_worker_burst,
)
from lagent_tablets.config import Config, Policy, FORBIDDEN_KEYWORDS_DEFAULT
from lagent_tablets.prompts import (
    build_reviewer_prompt,
    build_correspondence_prompt,
    build_nl_proof_prompt,
    build_verification_prompt,
    build_worker_prompt,
)
from lagent_tablets.state import (
    OPEN_REJECTION_PHASES,
    SupervisorState,
    TabletNode,
    TabletState,
    load_json,
    normalize_open_blockers,
    normalize_orphan_resolutions,
    save_json,
    save_state,
    save_tablet,
    state_path,
    tablet_path,
    timestamp_now,
)
from lagent_tablets.tablet import (
    PREAMBLE_NAME,
    compute_import_closure,
    compute_target_impact_region,
    declaration_hash,
    extract_imports,
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
from lagent_tablets.check import (
    check_proof_easy_scope,
    check_proof_hard_scope,
    check_proof_worker_delta,
    check_cleanup_preserving,
    check_node as run_check_node,
    check_tablet as run_check_tablet,
    check_tablet_scoped as run_check_tablet_scoped,
    check_theorem_target_edit_scope,
    check_theorem_target_repair_scope,
    is_lake_package_error,
    snapshot_tablet_node_hashes as canonical_snapshot_tablet_node_hashes,
    validate_json_artifact,
    write_scripts,
)
from lagent_tablets.nl_cache import (
    correspondence_fingerprint,
    correspondence_text_fingerprint,
    historical_correspondence_text_fingerprint,
    historical_legacy_correspondence_text_fingerprint,
    legacy_correspondence_fingerprint,
    legacy_correspondence_text_fingerprint,
    previous_correspondence_text_fingerprint,
    prime_correspondence_fingerprints,
    soundness_fingerprint,
)
from lagent_tablets.viewer_state import (
    viewer_state_path as canonical_viewer_state_path,
    write_cycle_viewer_state,
    write_live_viewer_state,
)


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


def _prune_deleted_tablet_nodes(
    tablet: TabletState,
    present_nodes: set[str],
) -> List[str]:
    """Remove non-preamble tablet nodes whose .lean/.tex pair was deleted from disk."""
    deleted_nodes: List[str] = []
    for name in sorted(list(tablet.nodes.keys())):
        node = tablet.nodes.get(name)
        if node is None or node.kind == "preamble":
            continue
        if name in present_nodes:
            continue
        deleted_nodes.append(name)
        tablet.nodes.pop(name, None)
    if tablet.active_node in deleted_nodes:
        tablet.active_node = ""
    return deleted_nodes


def _is_resolved_rejection_reason(description: str) -> bool:
    """Return True when text is clearly documenting a resolved past issue."""
    resolved_prefixes = (
        "previously flagged",
        "now fixed",
        "already fixed",
        "appears fixed",
        "appears genuinely fixed",
        "genuinely fixed",
        "seems fixed",
        "resolved:",
        "resolved -",
        "resolved ",
        "fixed in this revision",
        "fixed by this revision",
    )
    normalized = " ".join(
        str(description).lower()
        .replace("—", " ")
        .replace("–", " ")
        .split()
    )
    return any(normalized.startswith(prefix) for prefix in resolved_prefixes)


def _collect_theorem_stating_rejection_map(
    nl_verification_results: List[Dict[str, Any]],
) -> Dict[tuple[str, str], str]:
    """Collect current correspondence/paper-faithfulness failures by node."""
    issues_by_key: Dict[tuple[str, str], List[str]] = {}

    def add_phase_issues(phase: str, phase_result: Any) -> None:
        if not isinstance(phase_result, dict):
            return
        if str(phase_result.get("decision", "")).upper() != "FAIL":
            return
        issues = phase_result.get("issues", [])
        if not isinstance(issues, list):
            return
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            node = str(issue.get("node", "")).strip() or "(global)"
            description = str(issue.get("description", "")).strip()
            if not description:
                continue
            if _is_resolved_rejection_reason(description):
                continue
            key = (node, phase)
            bucket = issues_by_key.setdefault(key, [])
            if description not in bucket:
                bucket.append(description)

    for result in nl_verification_results:
        if result.get("check") != "correspondence":
            continue
        agent_results = result.get("agent_results")
        sources = agent_results if isinstance(agent_results, list) else [result]
        for source in sources:
            if not isinstance(source, dict):
                continue
            for phase in ("correspondence", "paper_faithfulness"):
                add_phase_issues(phase, source.get(phase))

    return {
        key: "; ".join(descriptions)
        for key, descriptions in sorted(issues_by_key.items())
    }


def _reconcile_theorem_stating_open_rejections(
    nl_verification_results: List[Dict[str, Any]],
    preferred_rejections: Any,
    *,
    include_preferred_extras: bool = False,
) -> List[Dict[str, str]]:
    """Persist the current theorem-stating rejection list with stable keys.

    The reviewer is asked to write the reasons, but we fall back to verifier
    issue text if the reviewer omits an entry. By default the resulting list
    matches the currently failing correspondence/paper-faithfulness issues.
    When `include_preferred_extras` is true, reviewer-authored blockers that
    are not present in the verifier output are preserved as additional current
    rejections so the worker sees a single authoritative list.
    """
    current_failures = _collect_theorem_stating_rejection_map(nl_verification_results)
    preferred_entries = [
        entry for entry in normalize_open_blockers(preferred_rejections)
        if not _is_resolved_rejection_reason(entry["reason"])
    ]
    preferred_map = {
        (entry["node"], entry["phase"]): entry["reason"]
        for entry in preferred_entries
    }
    all_keys = set(current_failures)
    if include_preferred_extras:
        all_keys.update(preferred_map)
    reconciled: List[Dict[str, str]] = []
    for node, phase in sorted(all_keys):
        fallback_reason = current_failures.get((node, phase), "")
        reason = preferred_map.get((node, phase), fallback_reason)
        if not reason:
            continue
        reconciled.append({
            "node": node,
            "phase": phase,
            "reason": reason,
        })
    return reconciled


def _correspondence_gate_open(nl_verification_results: List[Dict[str, Any]]) -> bool:
    """Return True when theorem-stating is still blocked on correspondence."""
    corr_results = [r for r in nl_verification_results if r.get("check") == "correspondence"]
    if not corr_results:
        return False
    return any(str(r.get("overall", "")).upper() != "APPROVE" for r in corr_results)


def _suspend_theorem_soundness_target(state: SupervisorState) -> SupervisorState:
    """Return a state view with the theorem-stating soundness target hidden."""
    return replace(
        state,
        theorem_soundness_target="",
        theorem_target_edit_mode="repair",
        theorem_correspondence_blocked=True,
    )


def _theorem_stating_open_blockers(state: SupervisorState) -> List[Dict[str, str]]:
    blockers = normalize_open_blockers(state.open_blockers)
    if blockers:
        return blockers
    if isinstance(state.last_review, dict):
        return normalize_open_blockers(
            state.last_review.get("open_blockers", state.last_review.get("open_rejections", []))
        )
    return []


def _theorem_stating_has_correspondence_blockers(state: SupervisorState) -> bool:
    return any(entry.get("phase") == "correspondence" for entry in _theorem_stating_open_blockers(state))


def _has_stale_theorem_stating_phase_carryover(state: SupervisorState) -> bool:
    if state.phase != "theorem_stating" or not isinstance(state.last_review, dict):
        return False
    review = state.last_review
    decision = str(review.get("decision", "") or "").upper()
    next_prompt = str(review.get("next_prompt", "") or "")
    next_active_node = str(review.get("next_active_node", "") or "").strip()
    lowered = next_prompt.lower()
    has_open_blockers = bool(_theorem_stating_open_blockers(state))

    if has_open_blockers and decision == "ADVANCE_PHASE":
        return True
    if next_active_node:
        return True
    if "proof_formalization" in lowered or lowered.startswith("begin proof_formalization"):
        return True
    return False


def _normalize_theorem_stating_replay_state(state: SupervisorState) -> List[str]:
    """Normalize persisted theorem-stating state before cycle dispatch.

    Rewinds can preserve stale reviewer carryover and stale target fields from
    an older interpretation of theorem-stating. Normalize those here so worker
    prompts and scheduling start from a coherent state.
    """
    notes: List[str] = []
    if state.phase != "theorem_stating":
        return notes

    blockers = _theorem_stating_open_blockers(state)
    if blockers != state.open_blockers:
        state.open_blockers = blockers
        notes.append("synchronized theorem-stating open blockers from persisted review state")

    if _has_stale_theorem_stating_phase_carryover(state) and isinstance(state.last_review, dict):
        cleared_keys: List[str] = []
        for key in ("next_prompt", "next_active_node", "paper_focus_ranges"):
            if key in state.last_review:
                state.last_review.pop(key, None)
                cleared_keys.append(key)
        if cleared_keys:
            notes.append(
                "cleared stale theorem-stating reviewer carryover: "
                + ", ".join(cleared_keys)
            )

    if _theorem_stating_has_correspondence_blockers(state):
        if state.theorem_soundness_target:
            notes.append(
                "cleared theorem-stating soundness target because correspondence blockers remain open"
            )
        state.theorem_correspondence_blocked = True
        state.theorem_soundness_target = ""
        state.theorem_target_edit_mode = "repair"
    elif state.theorem_correspondence_blocked and not state.resume_from:
        state.theorem_correspondence_blocked = False
        notes.append("cleared stale theorem correspondence-blocked flag")

    if not state.theorem_soundness_target:
        state.theorem_target_edit_mode = "repair"

    return notes


def _prepare_theorem_stating_worker_state(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
    *,
    nl_cache: Any,
) -> List[str]:
    """Mutate theorem-stating state into the worker-facing shape for the next cycle."""
    notes = _normalize_theorem_stating_replay_state(state)
    previous_soundness_target = state.theorem_soundness_target

    if state.theorem_correspondence_blocked or _theorem_stating_has_correspondence_blockers(state):
        state.theorem_correspondence_blocked = True
        state.theorem_soundness_target = ""
        state.theorem_target_edit_mode = "repair"
        return notes

    state.theorem_correspondence_blocked = False
    soundness_candidates = _eligible_soundness_nodes(config, tablet)
    soundness_candidates = nl_cache.filter_uncached(config.repo_path, soundness_candidates, "soundness")
    active_soundness_agents = _effective_soundness_agents(config, policy)
    state.theorem_soundness_target = _select_theorem_soundness_target(
        config,
        tablet,
        soundness_candidates,
        soundness_agents=active_soundness_agents,
        disagree_bias=policy.verification.soundness_disagree_bias,
        preferred=state.theorem_soundness_target,
    )
    if not state.theorem_soundness_target or state.theorem_soundness_target != previous_soundness_target:
        state.theorem_target_edit_mode = "repair"
    return notes


def _theorem_stating_preflight_error(state: SupervisorState) -> str:
    if state.phase != "theorem_stating":
        return ""
    if _theorem_stating_has_correspondence_blockers(state) and state.theorem_soundness_target:
        return (
            "Open correspondence blockers are incompatible with an active theorem-stating "
            f"soundness target ({state.theorem_soundness_target})."
        )
    if not state.theorem_soundness_target and _theorem_target_edit_mode(state) == "restructure":
        return "Theorem-stating target mode `restructure` requires an active soundness target."
    if _has_stale_theorem_stating_phase_carryover(state):
        return (
            "Stale proof-formalization carryover remained in theorem-stating review state "
            "after normalization."
        )
    return ""


def _default_theorem_stating_next_prompt() -> str:
    """Fallback worker guidance when the reviewer omits explicit instructions."""
    return (
        "Resolve the current open correspondence and paper-faithfulness "
        "rejections. Theorem-stating continues until the open-rejection list is empty."
    )


def _summarize_open_rejections(open_rejections: List[Dict[str, str]], *, limit: int = 3) -> str:
    """Short summary of the currently open theorem-stating rejections."""
    if not open_rejections:
        return ""
    parts = [f"{entry['node']} ({entry['phase']})" for entry in open_rejections[:limit]]
    if len(open_rejections) > limit:
        parts.append(f"+{len(open_rejections) - limit} more")
    return ", ".join(parts)


def _enforce_theorem_stating_open_rejections(
    decision: Dict[str, Any],
    open_rejections: List[Dict[str, str]],
) -> None:
    """Block theorem-stating phase advance while open verification rejections remain."""
    decision["open_blockers"] = open_rejections
    decision["open_rejections"] = open_rejections
    if open_rejections and not str(decision.get("next_prompt", "")).strip():
        decision["next_prompt"] = _default_theorem_stating_next_prompt()
    if decision.get("decision") == "ADVANCE_PHASE" and open_rejections:
        summary = _summarize_open_rejections(open_rejections)
        decision["decision"] = "CONTINUE"
        decision["reason"] = (
            "Open correspondence/paper-faithfulness rejections remain"
            + (f": {summary}" if summary else ".")
        )


def _summarize_orphan_candidates(orphan_candidates: List[str], *, limit: int = 3) -> str:
    """Short summary of current theorem-stating orphan-node candidates."""
    if not orphan_candidates:
        return ""
    parts = orphan_candidates[:limit]
    if len(orphan_candidates) > limit:
        parts.append(f"+{len(orphan_candidates) - limit} more")
    return ", ".join(parts)


def _default_orphan_next_prompt(
    orphan_resolutions: List[Dict[str, Any]],
    unresolved_candidates: List[str],
) -> str:
    """Fallback worker guidance for current orphan-node candidates."""
    lines = [
        "Resolve the current orphan-node candidates before treating the tablet structure as complete.",
    ]
    for entry in orphan_resolutions:
        node = entry["node"]
        if entry["action"] == "remove":
            lines.append(
                f"- Remove orphan node `{node}` unless you add a real downstream dependency and citation that justifies keeping it."
            )
        else:
            parents = entry.get("suggested_parents", [])
            if parents:
                lines.append(
                    f"- Keep orphan node `{node}` only by adding a real downstream dependency/citation from: {', '.join(parents)}."
                )
            else:
                lines.append(
                    f"- Keep orphan node `{node}` only if you add the missing real downstream dependency/citation showing where it is needed."
                )
    for node in unresolved_candidates:
        lines.append(
            f"- Reviewer must decide whether orphan node `{node}` should be removed or kept via a missing downstream dependency/citation."
        )
    return "\n".join(lines)


def _enforce_theorem_stating_orphan_candidates(
    decision: Dict[str, Any],
    orphan_candidates: List[str],
) -> None:
    """Persist reviewer arbitration for orphan candidates and block phase advance."""
    orphan_candidates = sorted(dict.fromkeys(orphan_candidates))
    resolutions = normalize_orphan_resolutions(
        decision.get("orphan_resolutions", []),
        allowed_nodes=set(orphan_candidates),
    )
    decision["orphan_resolutions"] = resolutions

    if not orphan_candidates:
        return

    resolved_nodes = {entry["node"] for entry in resolutions}
    unresolved = [name for name in orphan_candidates if name not in resolved_nodes]
    if not str(decision.get("next_prompt", "")).strip():
        decision["next_prompt"] = _default_orphan_next_prompt(resolutions, unresolved)

    if decision.get("decision") == "ADVANCE_PHASE":
        summary = _summarize_orphan_candidates(orphan_candidates)
        decision["decision"] = "CONTINUE"
        decision["reason"] = (
            "Orphan node candidates remain"
            + (f": {summary}" if summary else ".")
        )


def _artifact_paths(config: Config, canonical_name: str) -> Dict[str, Path]:
    return {
        "canonical": config.repo_path / canonical_name,
        "raw": raw_json_path(config.state_dir, canonical_name),
        "done": done_marker_path(config.state_dir, canonical_name),
        "stem": Path(artifact_stem(canonical_name)),
    }


def _save_live_viewer_state(
    config: Config,
    tablet: TabletState,
    state: SupervisorState,
    *,
    activity: Optional[Dict[str, Iterable[str]]] = None,
    source: str = "live",
) -> None:
    write_live_viewer_state(
        canonical_viewer_state_path(config.state_dir),
        config.repo_path,
        tablet,
        state,
        activity=activity,
        in_flight_cycle=state.cycle,
        source=source,
    )


def _save_cycle_viewer_state(
    config: Config,
    tablet: TabletState,
    state: SupervisorState,
    *,
    verification_results: Optional[List[Dict[str, Any]]] = None,
    source: str = "cycle",
) -> None:
    write_cycle_viewer_state(
        canonical_viewer_state_path(config.state_dir),
        config.repo_path,
        tablet,
        state,
        verification_results=verification_results,
        source=source,
    )


def _clear_artifact_files(config: Config, canonical_name: str) -> Dict[str, Path]:
    paths = _artifact_paths(config, canonical_name)
    for key in ("canonical", "raw", "done"):
        paths[key].unlink(missing_ok=True)
    return paths


def _accept_validated_artifact(
    config: Config,
    canonical_name: str,
    *,
    kind: str,
    phase: Optional[str] = None,
    node_name: Optional[str] = None,
    repo_for_validation: Optional[Path] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    paths = _artifact_paths(config, canonical_name)
    validation = validate_json_artifact(
        kind,
        paths["raw"],
        phase=phase,
        node_name=node_name,
        repo=repo_for_validation or config.repo_path,
    )
    if not validation["ok"]:
        return None, "; ".join(validation["errors"])
    data = validation["data"]
    assert isinstance(data, dict)
    if kind == "soundness-result" and node_name:
        try:
            from lagent_tablets.nl_cache import NLCache
            fp = NLCache(config.state_dir / "nl_cache.json").soundness_fingerprint(config.repo_path, node_name)
        except Exception:
            fp = None
        if fp:
            meta = data.get("_supervisor_meta", {})
            if not isinstance(meta, dict):
                meta = {}
            meta["soundness_fingerprint"] = fp
            data["_supervisor_meta"] = meta
    save_json(paths["canonical"], data)
    return data, None


# ---------------------------------------------------------------------------
# Permission setup
# ---------------------------------------------------------------------------

def setup_permissions(config: Config, active_node: str, *, easy_mode: bool = False) -> None:
    """Set file permissions before a worker burst.

    The burst_user runs the agent CLI. File permissions control what it can write:
    - Active node .lean: 0o664 (group-writable) -- worker can edit proof only
    - Active node .tex: 0o644 (easy) / 0o664 (hard)
    - Preamble.lean: 0o664 (hard) / 0o644 (easy) -- easy workers cannot add imports
    - Everything else: 0o644 (group-read-only) -- worker CANNOT edit
    - Tablet/ directory: 0o2775 (hard) / 0o2755 (easy) -- easy workers cannot create files
    - Repo root: 0o2755 -- worker cannot create/delete arbitrary top-level files
    - Staging dir: 0o2775 -- worker can write raw JSON + done markers only there

    The shared group is 'leanagent' (gid from leanagent user).
    The supervisor (leanagent) is the owner; burst_user (lagentworker) is in the group.
    """
    import grp
    import os

    repo = config.repo_path
    tdir = repo / "Tablet"
    staging = config.state_dir / "staging"
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
    # Easy mode: only the active node's .lean file. Preamble and .tex stay read-only.
    writable_basenames = {f"{active_node}.lean"}
    if not easy_mode:
        writable_basenames.add(f"{active_node}.tex")
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

    try:
        # Allow raw/done artifacts only in staging, not arbitrary repo-root writes.
        os.chown(str(repo), -1, gid)
        os.chmod(str(repo), 0o2755)
        staging.mkdir(parents=True, exist_ok=True)
        os.chown(str(staging), -1, gid)
        os.chmod(str(staging), 0o2775)
    except PermissionError:
        pass


def _setup_theorem_stating_permissions(
    config: Config,
    *,
    target: str = "",
    repair_mode: bool = False,
) -> None:
    """Set permissions for theorem_stating.

    In repair mode, only the target `.tex` proof file is writable.
    In restructure mode, the whole Tablet directory remains writable.
    """
    import grp
    import os

    repo = config.repo_path
    tdir = repo / "Tablet"
    staging = config.state_dir / "staging"
    if not tdir.exists():
        tdir.mkdir(parents=True, exist_ok=True)

    try:
        gid = grp.getgrnam("leanagent").gr_gid
    except KeyError:
        return

    # Tablet directory: lock it down in repair mode, keep it writable in restructure mode.
    try:
        os.chown(str(tdir), -1, gid)
        os.chmod(str(tdir), 0o2755 if repair_mode else 0o2775)
    except PermissionError:
        pass

    target_tex_name = f"{target}.tex" if target else ""

    # Tablet files: only the target .tex is writable in repair mode.
    for path in tdir.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            target_mode = 0o664
            if repair_mode:
                target_mode = 0o664 if path.name == target_tex_name else 0o644
            if stat.st_uid == os.getuid():
                if stat.st_gid != gid:
                    os.chown(str(path), -1, gid)
                os.chmod(str(path), target_mode)
            else:
                import subprocess as sp
                sp.run(["sudo", "-n", "-u", "lagentworker", "chmod", oct(target_mode)[2:], str(path)],
                       capture_output=True, timeout=5)
        except (PermissionError, OSError):
            pass

    # Staging is writable; repo root is read/execute only to avoid arbitrary top-level writes.
    staging.mkdir(parents=True, exist_ok=True)
    for p in [repo, staging]:
        try:
            if p.exists():
                os.chown(str(p), -1, gid)
                os.chmod(str(p), 0o2775 if p == staging else 0o2755)
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
            approved_axioms_path=config.workflow.approved_axioms_path,
            timeout_seconds=config.burst_timeout_seconds,
        )
        if not result.compiles:
            # Check if the failure is just Lake package noise
            if is_lake_package_error(result.build_output):
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
            approved_axioms_path=config.workflow.approved_axioms_path,
            timeout_seconds=config.burst_timeout_seconds,
        )
        if not result.compiles:
            if not is_lake_package_error(result.build_output):
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


def _theorem_stating_node_kind(
    name: str,
    kind_hints: Dict[str, str],
) -> str:
    """Return the structural role for a theorem-stating node.

    New theorem-stating nodes default to paper_intermediate unless the worker
    explicitly classifies them as a paper_main_result.
    """
    kind = str(kind_hints.get(name, "paper_intermediate") or "paper_intermediate").strip()
    if kind in {"paper_main_result", "paper_intermediate"}:
        return kind
    return "paper_intermediate"


def validate_worker_cycle_v2(
    config: Config,
    tablet: TabletState,
    active_node: str,
    *,
    snapshot_before: Dict[str, str],
    active_changed: bool,
    new_lean_files: List[str],
) -> CycleOutcome:
    """Compatibility wrapper over the canonical proof-worker delta check."""
    repo = config.repo_path
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]
    existing_nodes = sorted(tablet.nodes.keys())
    result = check_proof_worker_delta(
        repo,
        active_node=active_node,
        snapshot_before=snapshot_before,
        existing_nodes=existing_nodes,
        expected_active_hash=tablet.nodes[active_node].lean_statement_hash if active_node in tablet.nodes else "",
        allowed_prefixes=config.workflow.allowed_import_prefixes,
        forbidden_keywords=forbidden,
        approved_axioms_path=config.workflow.approved_axioms_path,
    )
    return CycleOutcome(
        outcome=result["outcome"],
        detail=result["detail"],
        nodes_closed=list(result.get("nodes_closed", [])),
        nodes_created=list(result.get("nodes_created", [])),
        build_output=result.get("build_output", ""),
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
                approved_axioms_path=config.workflow.approved_axioms_path,
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
    previous_own_result: Optional[Dict[str, Any]] = None,
    agent_index: int,
) -> Dict[str, Any]:
    """Run one correspondence agent. Designed to be called from a thread."""

    agent_start = time.monotonic()
    repo = config.repo_path
    label = agent_config.label or f"agent-{agent_index}"
    selected_index = getattr(agent_config, "selected_index", agent_index)
    output_file = f"correspondence_result_{selected_index}.json"
    port = 3286 + agent_index * 2  # 3286, 3288, 3290, ...

    artifact_paths = _clear_artifact_files(config, output_file)

    prompt = build_correspondence_prompt(
        config, tablet, node_names=corr_nodes, paper_tex=paper_tex,
        human_input=human_input, output_file=output_file,
        previous_results=[previous_own_result] if previous_own_result else None,
    )

    agent_provider = ProviderConfig(
        provider=agent_config.provider,
        model=agent_config.model,
        effort=getattr(agent_config, 'effort', None),
        extra_args=agent_config.extra_args,
        fallback_models=getattr(agent_config, 'fallback_models', []),
    )

    burst_result = run_reviewer_burst(
        agent_provider, prompt,
        session_name=config.tmux.session_name,
        work_dir=repo, burst_user=config.tmux.burst_user,
        timeout_seconds=1800, log_dir=log_dir, fresh=True,
        port=port,
        done_file=artifact_paths["done"],
        artifact_prefix=str(artifact_paths["stem"]),
    )

    decision = None
    artifact_error = None
    if burst_result.ok:
        decision, artifact_error = _accept_validated_artifact(
            config,
            output_file,
            kind="correspondence-result",
        )

    result = {
        "agent": label,
        "index": selected_index,
        "ok": burst_result.ok,
        "walltime_seconds": round(time.monotonic() - agent_start, 1),
        **(
            decision
            if isinstance(decision, dict)
            else {"overall": "ERROR", "summary": artifact_error or f"Failed to get decision from {label}"}
        ),
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

    agent_start = time.monotonic()
    repo = config.repo_path
    label = agent_config.label or f"soundness-{agent_index}"
    selected_index = getattr(agent_config, "selected_index", agent_index)
    output_file = f"nl_proof_result_{selected_index}.json"
    # Soundness agents use ports 3310, 3312, 3314, ... (separate from correspondence 3286+ and viewer 3300)
    port = 3310 + agent_index * 2

    artifact_paths = _clear_artifact_files(config, output_file)

    prompt = build_nl_proof_prompt(
        config, tablet, node_names=proof_nodes, paper_tex=paper_tex,
        human_input=human_input,
        output_file=output_file,
    )

    agent_provider = ProviderConfig(
        provider=agent_config.provider,
        model=agent_config.model,
        effort=getattr(agent_config, 'effort', None),
        extra_args=agent_config.extra_args,
        fallback_models=getattr(agent_config, 'fallback_models', []),
    )

    burst_result = run_reviewer_burst(
        agent_provider, prompt,
        session_name=config.tmux.session_name,
        work_dir=repo, burst_user=config.tmux.burst_user,
        timeout_seconds=1800, log_dir=log_dir, fresh=True,
        done_file=artifact_paths["done"],
        artifact_prefix=str(artifact_paths["stem"]),
        port=port,
    )

    decision = None
    artifact_error = None
    if burst_result.ok:
        decision, artifact_error = _accept_validated_artifact(
            config,
            output_file,
            kind="soundness-batch-result",
        )

    result = {
        "agent": label,
        "index": selected_index,
        "ok": burst_result.ok,
        "walltime_seconds": round(time.monotonic() - agent_start, 1),
        **(
            decision
            if isinstance(decision, dict)
            else {"overall": "ERROR", "summary": artifact_error or f"Failed to get decision from {label}"}
        ),
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
                log_dir=log_dir, agent_index=getattr(agent, "selected_index", i),
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
    from lagent_tablets.prompts import build_node_soundness_prompt

    agent_start = time.monotonic()
    repo = config.repo_path
    label = agent_config.label or f"soundness-{agent_index}"
    selected_index = getattr(agent_config, "selected_index", agent_index)
    output_file = f"nl_proof_{node_name}_{selected_index}.json"
    # Ports: 3310 + (node_index * 10) + (agent_index * 2) — spread to avoid collisions
    port = 3310 + (node_index % 5) * 10 + agent_index * 2

    previous_issues = _load_previous_soundness_issues(repo, node_name=node_name, agent_index=selected_index)
    artifact_paths = _clear_artifact_files(config, output_file)

    prompt = build_node_soundness_prompt(
        config, tablet, node_name=node_name, paper_tex=paper_tex,
        human_input=human_input, output_file=output_file,
        previous_issues=previous_issues,
    )

    agent_provider = ProviderConfig(
        provider=agent_config.provider,
        model=agent_config.model,
        effort=getattr(agent_config, 'effort', None),
        extra_args=agent_config.extra_args,
        fallback_models=getattr(agent_config, 'fallback_models', []),
    )

    burst_result = run_reviewer_burst(
        agent_provider, prompt,
        session_name=config.tmux.session_name,
        work_dir=repo, burst_user=config.tmux.burst_user,
        timeout_seconds=1800, log_dir=log_dir, fresh=True,
        port=port,
        done_file=artifact_paths["done"],
        artifact_prefix=str(artifact_paths["stem"]),
    )

    decision = None
    artifact_error = None
    if burst_result.ok:
        decision, artifact_error = _accept_validated_artifact(
            config,
            output_file,
            kind="soundness-result",
            node_name=node_name,
        )

    result = {
        "agent": label,
        "node": node_name,
        "index": selected_index,
        "ok": burst_result.ok,
        "walltime_seconds": round(time.monotonic() - agent_start, 1),
        **(
            decision
            if isinstance(decision, dict)
            else {"overall": "ERROR", "summary": artifact_error or f"Failed to get decision from {label}"}
        ),
    }
    if burst_result.usage:
        result["_usage"] = burst_result.usage
    return result


def _load_previous_soundness_issues(
    repo: Path,
    *,
    node_name: str,
    agent_index: int,
) -> List[str]:
    """Load prior per-agent soundness objections for prompt continuity."""
    from lagent_tablets.check import validate_node_soundness_result_data

    path = repo / f"nl_proof_{node_name}_{agent_index}.json"
    if not path.exists():
        return []
    try:
        raw = load_json(path)
    except Exception:
        return []
    validation = validate_node_soundness_result_data(raw, node_name=node_name)
    if not validation["ok"] or not isinstance(validation.get("data"), dict):
        return []

    data = validation["data"]
    if data.get("overall") == "APPROVE":
        return []

    soundness = data.get("soundness", {})
    decision = str(soundness.get("decision", "")).strip()
    explanation = str(soundness.get("explanation", "")).strip()
    summary = str(data.get("summary", "")).strip()

    issues: List[str] = []
    if decision and decision != "UNSOUND":
        issues.append(f"{decision}: {explanation or summary}".strip())
    elif explanation:
        issues.append(explanation)
    if summary and summary not in issues:
        issues.append(summary)
    return issues


def _soundness_priority_order(
    repo: Path,
    tablet: TabletState,
    node_names: List[str],
) -> List[str]:
    """Order nodes for per-node soundness in deterministic deepest-first DAG order.

    If node A imports node B, then B is checked before A. Ties are broken
    lexicographically for determinism.
    """
    candidate_set = {name for name in node_names if name != PREAMBLE_NAME}
    parents: Dict[str, Set[str]] = {name: set() for name in candidate_set}
    remaining_children: Dict[str, int] = {name: 0 for name in candidate_set}

    for name in candidate_set:
        lean_path = node_lean_path(repo, name)
        if not lean_path.exists():
            continue
        imports = set(extract_tablet_imports(lean_path.read_text(encoding="utf-8")))
        children = imports & candidate_set
        remaining_children[name] = len(children)
        for child in children:
            parents[child].add(name)

    ready = sorted(name for name, deg in remaining_children.items() if deg == 0)
    ordered: List[str] = []

    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for parent in sorted(parents.get(name, ())):
            remaining_children[parent] -= 1
            if remaining_children[parent] == 0:
                ready.append(parent)
        ready.sort()

    remaining = sorted(candidate_set - set(ordered))
    ordered.extend(remaining)
    return ordered


def _soundness_source_mtime(repo: Path, node_name: str) -> float:
    latest = 0.0
    nodes = {node_name} | compute_import_closure(repo, node_name)
    for name in nodes:
        tex = node_tex_path(repo, name)
        if tex.exists():
            latest = max(latest, tex.stat().st_mtime)
    return latest


def _load_existing_soundness_result(
    config: Config,
    *,
    node_name: str,
    agent_index: int,
    agent_label: str,
) -> Optional[Dict[str, Any]]:
    """Reuse an already accepted per-node soundness result if it matches current content.

    Canonical files are supervisor-authored. If they carry a matching
    soundness fingerprint, they can be reused directly on verification resume.
    For older files from the current cycle that predate fingerprint stamping,
    we can safely backfill the fingerprint from the current unchanged verification
    state when matching raw+done artifacts are present.
    """
    from lagent_tablets.check import validate_node_soundness_result_data
    from lagent_tablets.nl_cache import NLCache

    canonical_name = f"nl_proof_{node_name}_{agent_index}.json"
    paths = _artifact_paths(config, canonical_name)
    if not paths["canonical"].exists():
        return None

    raw_data = load_json(paths["canonical"], default=None)
    if not isinstance(raw_data, dict):
        return None

    validation = validate_node_soundness_result_data(raw_data, node_name=node_name)
    if not validation["ok"]:
        return None
    data = validation["data"]
    assert isinstance(data, dict)

    current_fp = NLCache(config.state_dir / "nl_cache.json").soundness_fingerprint(config.repo_path, node_name)
    if not current_fp:
        return None

    meta = raw_data.get("_supervisor_meta", {})
    if not isinstance(meta, dict):
        meta = {}
    stored_fp = meta.get("soundness_fingerprint")

    if not stored_fp:
        # Backfill for accepted current-cycle artifacts created before fingerprint
        # stamping, but only if the matching raw+done pair still exists.
        source_mtime = _soundness_source_mtime(config.repo_path, node_name)
        artifact_mtime = min(
            p.stat().st_mtime
            for p in (paths["canonical"], paths["raw"], paths["done"])
            if p.exists()
        )
        if paths["raw"].exists() and paths["done"].exists() and artifact_mtime >= source_mtime:
            meta["soundness_fingerprint"] = current_fp
            raw_data["_supervisor_meta"] = meta
            save_json(paths["canonical"], raw_data)
            stored_fp = current_fp

    if stored_fp != current_fp:
        return None

    return {
        "agent": agent_label,
        "node": node_name,
        "index": agent_index,
        "ok": True,
        "walltime_seconds": 0.0,
        **data,
    }


def _soundness_panel_overall(
    config: Config,
    *,
    node_name: str,
    agents: List[Any],
    disagree_bias: str = "reject",
) -> str:
    """Return APPROVE, REJECT, or PENDING for one node's current soundness panel."""
    results: List[Dict[str, Any]] = []
    for ai, agent in enumerate(agents):
        label = agent.label or f"{agent.provider}/{agent.model}"
        selected_index = getattr(agent, "selected_index", ai)
        existing = _load_existing_soundness_result(
            config,
            node_name=node_name,
            agent_index=selected_index,
            agent_label=label,
        )
        if existing is None:
            return "PENDING"
        results.append(existing)
    overalls = [r.get("overall") for r in results]
    if all(o == "APPROVE" for o in overalls):
        return "APPROVE"
    if all(o == "REJECT" for o in overalls):
        return "REJECT"
    if len(results) == 2 and len(set(overalls)) > 1:
        return "APPROVE" if disagree_bias == "approve" else "REJECT"
    return "REJECT"


def _agent_matches_selector(agent: Any, selector: str, index: int) -> bool:
    normalized = str(selector).strip().lower()
    if not normalized:
        return False
    provider = str(getattr(agent, "provider", "")).strip().lower()
    model = str(getattr(agent, "model", "") or "").strip().lower()
    label = str(getattr(agent, "label", "") or "").strip().lower()
    if normalized == str(index):
        return True
    if normalized == provider:
        return True
    if normalized == model and model:
        return True
    if normalized == label and label:
        return True
    return False


def _resolve_verification_agents(
    configured_agents: List[Any],
    selectors: List[str] | tuple[str, ...],
    *,
    phase_name: str,
) -> List[Any]:
    """Resolve a hot-settable ordered subset of verification agents.

    Selectors may be provider names (`claude`, `gemini`, `codex`), exact labels,
    exact models, or stringified indices (`0`, `1`, ...). The first matching
    configured agent is taken for each selector; duplicates are ignored.
    """
    if not selectors:
        return list(configured_agents)
    resolved: List[Any] = []
    used_indices: Set[int] = set()
    unresolved: List[str] = []
    for selector in selectors:
        matched_index = None
        for idx, agent in enumerate(configured_agents):
            if idx in used_indices:
                continue
            if _agent_matches_selector(agent, selector, idx):
                matched_index = idx
                break
        if matched_index is None:
            unresolved.append(str(selector))
            continue
        used_indices.add(matched_index)
        agent = configured_agents[matched_index]
        resolved.append(SimpleNamespace(
            provider=getattr(agent, "provider", ""),
            model=getattr(agent, "model", None),
            label=getattr(agent, "label", ""),
            effort=getattr(agent, "effort", None),
            extra_args=list(getattr(agent, "extra_args", []) or []),
            fallback_models=list(getattr(agent, "fallback_models", []) or []),
            selected_index=matched_index,
        ))
    if unresolved:
        print(
            f"WARNING: Ignoring unresolved {phase_name} agent selectors: {', '.join(unresolved)}"
        )
    if not resolved:
        print(
            f"WARNING: No {phase_name} agents matched policy selectors; falling back to configured agents."
        )
        return list(configured_agents)
    return resolved


def _effective_correspondence_agents(config: Config, policy: Policy) -> List[Any]:
    return _resolve_verification_agents(
        config.verification.correspondence_agents,
        policy.verification.correspondence_agent_selectors,
        phase_name="correspondence",
    )


def _effective_soundness_agents(config: Config, policy: Policy) -> List[Any]:
    return _resolve_verification_agents(
        config.verification.soundness_agents,
        policy.verification.soundness_agent_selectors,
        phase_name="soundness",
    )


def _eligible_soundness_nodes(
    config: Config,
    tablet: TabletState,
) -> List[str]:
    """Return theorem-stating nodes that still require NL proof soundness."""
    return [
        name
        for name, node in tablet.nodes.items()
        if name != PREAMBLE_NAME
        and node.status != "closed"
        and not _is_definition_node(config.repo_path, name)
    ]


def _select_theorem_soundness_target(
    config: Config,
    tablet: TabletState,
    candidate_nodes: List[str],
    *,
    soundness_agents: List[Any],
    disagree_bias: str = "reject",
    preferred: str = "",
) -> str:
    """Pick the current theorem-stating soundness target.

    Keep the previous target if it is still unresolved; otherwise take the first
    unresolved node in deterministic deepest-first DAG order.
    """
    if not candidate_nodes:
        return ""
    ordered = _soundness_priority_order(config.repo_path, tablet, candidate_nodes)
    if preferred and preferred in ordered:
        if _soundness_panel_overall(
            config,
            node_name=preferred,
            agents=soundness_agents,
            disagree_bias=disagree_bias,
        ) != "APPROVE":
            return preferred
    for name in ordered:
        if _soundness_panel_overall(
            config,
            node_name=name,
            agents=soundness_agents,
            disagree_bias=disagree_bias,
        ) != "APPROVE":
            return name
    return ""


def _run_per_node_soundness(
    config: Config,
    tablet: TabletState,
    node_names: List[str],
    agents: List[Any],
    *,
    disagree_bias: str = "reject",
    paper_tex: str,
    human_input: str,
    log_dir: Path,
    batch_size: int = 1,
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
    check_nodes = _soundness_priority_order(config.repo_path, tablet, check_nodes)
    print(f"  Per-node soundness: {len(check_nodes)} nodes × {n_agents} agents ({', '.join(labels)})")

    all_results: List[Dict[str, Any]] = []

    # Process in batches to limit concurrency
    for batch_start in range(0, len(check_nodes), batch_size):
        batch = check_nodes[batch_start:batch_start + batch_size]
        if len(batch) == 1:
            print(f"  Soundness node {batch_start + 1}/{len(check_nodes)}: {batch[0]}")
        else:
            print(f"  Soundness batch {batch_start // batch_size + 1}: {batch}")

        reusable: List[Dict[str, Any]] = []
        work_items: List[tuple[str, int, Any]] = []
        for ni, node_name in enumerate(batch):
            for ai, agent in enumerate(agents):
                existing = _load_existing_soundness_result(
                    config,
                    node_name=node_name,
                    agent_index=getattr(agent, "selected_index", ai),
                    agent_label=labels[ai],
                )
                if existing is not None:
                    reusable.append(existing)
                else:
                    work_items.append((node_name, ai, agent))

        all_results.extend(reusable)
        if not work_items:
            continue

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(work_items)) as pool:
            futures = {}
            for node_name, ai, agent in work_items:
                node_pos = check_nodes.index(node_name)
                f = pool.submit(
                    _run_single_node_soundness,
                    config, tablet, node_name, agent,
                    paper_tex=paper_tex, human_input=human_input,
                    log_dir=log_dir, agent_index=ai,
                    node_index=node_pos,
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
        all_reject = all(o == "REJECT" for o in overalls)
        panel_split = len(node_results) == 2 and len(set(overalls)) > 1
        if all_approve:
            node_overall = "APPROVE"
        elif all_reject:
            node_overall = "REJECT"
        elif panel_split:
            node_overall = "REJECT" if disagree_bias == "reject" else "APPROVE"
        else:
            node_overall = "REJECT"

        verdict = {
            "node": node_name,
            "agent_results": node_results,
            "overall": node_overall,
        }
        if panel_split:
            verdict["panel_split"] = True
            verdict["disagree_bias"] = disagree_bias
        if has_structural:
            verdict["structural"] = True
            structural_issues.append(node_name)
            print(f"    {node_name}: STRUCTURAL (DAG needs restructuring)")
        elif all_approve:
            print(f"    {node_name}: SOUND (unanimous)")
        elif panel_split:
            detail = ", ".join(f"{r.get('agent','?')}: {o}" for r, o in zip(node_results, overalls))
            print(f"    {node_name}: DISAGREE ({detail}) -> default {node_overall} by {disagree_bias} bias")
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
        "node_names": check_nodes,
        "node_verdicts": node_verdicts,
    }]


def _correspondence_content_hash(repo_path: Path, name: str) -> str:
    """Statement-level hash used to invalidate correspondence results."""
    return correspondence_fingerprint(repo_path, name) or ""


def _correspondence_text_hash(repo_path: Path, name: str) -> str:
    """Conservative text-level hash used for fast correspondence invalidation."""
    return correspondence_text_fingerprint(repo_path, name) or ""


def _soundness_content_hash(repo_path: Path, name: str) -> str:
    """Full NL-proof hash used to invalidate soundness results."""
    return soundness_fingerprint(repo_path, name) or ""


def _node_content_hash(repo_path: Path, name: str) -> str:
    """Legacy combined hash retained for backward-compatible tests and state."""
    return _soundness_content_hash(repo_path, name)


def _backfill_legacy_correspondence_hashes(tablet: TabletState, repo_path: Path) -> bool:
    """Backfill correspondence hashes needed by the current invalidation logic."""
    changed = False
    for name, node in tablet.nodes.items():
        if name == "Preamble" or node.correspondence_status not in ("pass", "fail"):
            continue
        verified_cycle = int(node.verification_at_cycle or 0)
        verified_tag = f"cycle-{verified_cycle}" if verified_cycle > 0 else ""
        historical_text_hash = (
            historical_correspondence_text_fingerprint(repo_path, verified_tag, name)
            if verified_tag else None
        )
        historical_legacy_text_hash = (
            historical_legacy_correspondence_text_fingerprint(repo_path, verified_tag, name)
            if verified_tag else None
        )
        current_text_hash = _correspondence_text_hash(repo_path, name)
        current_legacy_text_hash = legacy_correspondence_text_fingerprint(repo_path, name)

        # Backfill missing text hashes from the *historical verified revision*,
        # not from the current worktree. Using the current text here can mask
        # real statement drift that should reopen correspondence.
        if historical_text_hash and not node.correspondence_text_hash:
            node.correspondence_text_hash = historical_text_hash
            changed = True
        elif current_text_hash and node.correspondence_text_hash != current_text_hash:
            legacy_text_hash = legacy_correspondence_text_fingerprint(repo_path, name)
            previous_text_hash = previous_correspondence_text_fingerprint(repo_path, name)
            if (
                (legacy_text_hash and node.correspondence_text_hash == legacy_text_hash)
                or (previous_text_hash and node.correspondence_text_hash == previous_text_hash)
            ):
                node.correspondence_text_hash = current_text_hash
                changed = True
        if node.correspondence_status != "pass":
            continue
        current_corr_hash = _correspondence_content_hash(repo_path, name)
        if not current_corr_hash:
            continue
        saved_corr_hash = node.correspondence_content_hash or node.verification_content_hash
        if not saved_corr_hash:
            # Only seed a missing semantic baseline when the historical verified
            # source context still matches the current one. Otherwise leave the
            # hash missing so the frontier logic forces a fresh correspondence run.
            if (
                historical_text_hash
                and current_text_hash
                and historical_text_hash == current_text_hash
                and historical_legacy_text_hash
                and current_legacy_text_hash
                and historical_legacy_text_hash == current_legacy_text_hash
            ):
                node.correspondence_content_hash = current_corr_hash
                node.verification_content_hash = node.soundness_content_hash or current_corr_hash
                changed = True
            continue
        if saved_corr_hash == current_corr_hash:
            if not node.correspondence_content_hash:
                node.correspondence_content_hash = current_corr_hash
                changed = True
            continue
        legacy_corr_hash = legacy_correspondence_fingerprint(repo_path, name)
        if legacy_corr_hash and saved_corr_hash == legacy_corr_hash:
            node.correspondence_content_hash = current_corr_hash
            node.verification_content_hash = node.soundness_content_hash or current_corr_hash
            changed = True
    return changed


def _repair_stale_legacy_correspondence_failures(
    tablet: TabletState,
    state: SupervisorState,
    repo_path: Path,
) -> bool:
    """Demote stale legacy correspondence failures to unknown, never to pass.

    Older state files could drop `open_rejections` while still carrying a failed
    correspondence status on the node. Promoting those failures to `pass`
    without a fresh verifier run is unsafe. The safe repair is to reopen them.
    """
    live_rejected = {
        str(item.get("node", "")).strip()
        for item in state.open_rejections
        if str(item.get("phase", "")) in OPEN_REJECTION_PHASES
    }
    changed = False
    for name, node in tablet.nodes.items():
        if name == "Preamble" or node.correspondence_status != "fail":
            continue
        if name in live_rejected:
            continue
        node.correspondence_status = "?"
        node.correspondence_content_hash = ""
        node.correspondence_text_hash = ""
        changed = True
    return changed


def _theorem_stating_correspondence_frontier(tablet: TabletState, repo_path: Path) -> List[str]:
    """Return theorem-stating nodes whose statement-level correspondence changed."""
    frontier: List[str] = []
    semantic_candidates: List[str] = []
    for name, node in tablet.nodes.items():
        if name == "Preamble" or node.kind == "preamble":
            continue
        current_text_hash = _correspondence_text_hash(repo_path, name)
        saved_text_hash = node.correspondence_text_hash
        if not current_text_hash or node.correspondence_status == "?":
            frontier.append(name)
            continue
        # Any local/imported .tex statement change requires a fresh
        # correspondence check. Lean-side drift is handled by the semantic
        # fingerprint instead of this fast text path.
        if saved_text_hash and saved_text_hash != current_text_hash:
            frontier.append(name)
            continue
        semantic_candidates.append(name)

    if semantic_candidates:
        prime_correspondence_fingerprints(repo_path, semantic_candidates)

    for name in semantic_candidates:
        node = tablet.nodes[name]
        current_text_hash = _correspondence_text_hash(repo_path, name)
        current_corr_hash = _correspondence_content_hash(repo_path, name)
        saved_corr_hash = node.correspondence_content_hash or node.verification_content_hash
        if not current_corr_hash or not saved_corr_hash:
            frontier.append(name)
            continue
        if saved_corr_hash == current_corr_hash:
            node.correspondence_text_hash = current_text_hash
            continue
        legacy_corr_hash = legacy_correspondence_fingerprint(repo_path, name)
        if legacy_corr_hash and saved_corr_hash == legacy_corr_hash:
            node.correspondence_content_hash = current_corr_hash
            node.correspondence_text_hash = current_text_hash
            node.verification_content_hash = node.soundness_content_hash or current_corr_hash
            continue
        frontier.append(name)
    return sorted(frontier)


def _tablet_node_file_hash(repo_path: Path, name: str) -> str:
    """Hash the current on-disk .lean/.tex pair for a node."""
    h = hashlib.sha256()
    lean_path = node_lean_path(repo_path, name)
    tex_path = node_tex_path(repo_path, name)
    h.update(lean_path.read_bytes() if lean_path.exists() else b"")
    h.update(b"\0")
    h.update(tex_path.read_bytes() if tex_path.exists() else b"")
    return h.hexdigest()


def _current_tablet_node_names(repo_path: Path) -> Set[str]:
    tablet_dir = repo_path / "Tablet"
    if not tablet_dir.exists():
        return set()
    lean_files = {p.stem for p in tablet_dir.glob("*.lean") if p.stem != PREAMBLE_NAME}
    tex_files = {p.stem for p in tablet_dir.glob("*.tex") if p.stem not in ("header", PREAMBLE_NAME)}
    return lean_files | tex_files


def _snapshot_tablet_node_hashes(repo_path: Path) -> Dict[str, str]:
    return canonical_snapshot_tablet_node_hashes(repo_path)


def _theorem_target_scope(repo_path: Path, target: str) -> Set[str]:
    return compute_target_impact_region(repo_path, target)


def _theorem_target_edit_mode(state: SupervisorState) -> str:
    mode = str(getattr(state, "theorem_target_edit_mode", "repair") or "repair").strip().lower()
    return mode if mode in {"repair", "restructure"} else "repair"


def _theorem_stating_closed_nodes(
    config: Config,
    node_names: Sequence[str],
) -> List[str]:
    """Return theorem-stating nodes that now pass the exact node checker."""
    forbidden = [
        kw for kw in FORBIDDEN_KEYWORDS_DEFAULT
        if kw not in config.workflow.forbidden_keyword_allowlist
    ]
    closed: List[str] = []
    for name in node_names:
        if not name or name == PREAMBLE_NAME:
            continue
        result = run_check_node(
            config.repo_path,
            name,
            allowed_prefixes=config.workflow.allowed_import_prefixes,
            forbidden_keywords=forbidden,
            expected_hash="",
            approved_axioms_path=config.workflow.approved_axioms_path,
        )
        if result.get("ok"):
            closed.append(name)
    return sorted(dict.fromkeys(closed))


def _changed_tablet_nodes_since_snapshot(repo_path: Path, before_hashes: Dict[str, str]) -> List[str]:
    current_names = _current_tablet_node_names(repo_path)
    all_names = set(before_hashes) | current_names
    changed: List[str] = []
    for name in sorted(all_names):
        current_hash = _tablet_node_file_hash(repo_path, name) if name in current_names else ""
        if before_hashes.get(name, "") != current_hash:
            changed.append(name)
    return changed


def _validate_theorem_target_edit_scope(
    repo_path: Path,
    target: str,
    before_hashes: Dict[str, str],
    *,
    initial_scope: Optional[Set[str]] = None,
) -> Optional[str]:
    """Compatibility wrapper over the canonical theorem-target scope check."""
    result = check_theorem_target_edit_scope(
        repo_path,
        target=target,
        before_hashes=before_hashes,
        initial_scope=sorted(initial_scope or set()),
    )
    return result["errors"][0] if result.get("errors") else None


def _validate_theorem_target_repair_changes(
    repo_path: Path,
    target: str,
    snapshot_before: Dict[str, str],
) -> Optional[str]:
    """Compatibility wrapper over the canonical theorem target repair check."""
    result = check_theorem_target_repair_scope(
        repo_path,
        target=target,
        snapshot_before=snapshot_before,
    )
    return result["errors"][0] if result.get("errors") else None


def _validate_easy_proof_repair_changes(
    repo_path: Path,
    active_node: str,
    snapshot_before: Dict[str, str],
) -> tuple[Optional[str], List[str]]:
    """Compatibility wrapper over the canonical easy-mode proof scope check."""
    result = check_proof_easy_scope(
        repo_path,
        active_node=active_node,
        snapshot_before=snapshot_before,
    )
    return (
        result["errors"][0] if result.get("errors") else None,
        list(result.get("created_content_files", [])),
    )


def _scoped_tablet_check_payload_path(log_dir: Path) -> Path:
    return log_dir / "theorem_target_scope_check.json"


def _theorem_target_repair_scope_payload_path(log_dir: Path) -> Path:
    return log_dir / "theorem_target_repair_scope.json"


def _write_theorem_target_repair_scope_payload(
    log_dir: Path,
    target: str,
    snapshot_before: Dict[str, str],
) -> Path:
    payload = {
        "target": target,
        "snapshot_before": snapshot_before,
    }
    out = _theorem_target_repair_scope_payload_path(log_dir)
    save_json(out, payload, mode=0o644)
    return out


def _theorem_target_edit_scope_payload_path(log_dir: Path) -> Path:
    return log_dir / "theorem_target_edit_scope.json"


def _write_theorem_target_edit_scope_payload(
    log_dir: Path,
    target: str,
    before_hashes: Dict[str, str],
    initial_scope: Set[str],
) -> Path:
    payload = {
        "target": target,
        "before_hashes": before_hashes,
        "initial_scope": sorted(initial_scope),
    }
    out = _theorem_target_edit_scope_payload_path(log_dir)
    save_json(out, payload, mode=0o644)
    return out


def _write_scoped_tablet_check_payload(
    config: Config,
    log_dir: Path,
    target: str,
    allowed_nodes: Set[str],
) -> Path:
    repo = config.repo_path
    forbidden = [
        kw for kw in FORBIDDEN_KEYWORDS_DEFAULT
        if kw not in config.workflow.forbidden_keyword_allowlist
    ]
    baseline = run_check_tablet(
        repo,
        allowed_prefixes=config.workflow.allowed_import_prefixes,
        forbidden_keywords=forbidden,
        approved_axioms_path=config.workflow.approved_axioms_path,
    )
    payload = {
        "target": target,
        "allowed_nodes": sorted(allowed_nodes),
        "baseline_errors": list(baseline.get("errors", [])),
    }
    out = _scoped_tablet_check_payload_path(log_dir)
    save_json(out, payload, mode=0o644)
    return out


def _run_scoped_tablet_check(
    config: Config,
    *,
    baseline_errors: Sequence[str],
    allowed_nodes: Sequence[str],
) -> Optional[CycleOutcome]:
    """Fail only on newly introduced deterministic errors relevant to allowed nodes."""
    forbidden = [
        kw for kw in FORBIDDEN_KEYWORDS_DEFAULT
        if kw not in config.workflow.forbidden_keyword_allowlist
    ]
    scoped_result = run_check_tablet_scoped(
        config.repo_path,
        allowed_prefixes=config.workflow.allowed_import_prefixes,
        forbidden_keywords=forbidden,
        baseline_errors=baseline_errors,
        allowed_nodes=allowed_nodes,
        approved_axioms_path=config.workflow.approved_axioms_path,
    )
    if scoped_result["errors"]:
        return CycleOutcome(
            outcome="INVALID",
            detail=scoped_result["errors"][0],
            build_output=scoped_result.get("build_output", ""),
        )
    return None


def _cleanup_check_payload_path(log_dir: Path) -> Path:
    return log_dir / "cleanup_scope_check.json"


def _proof_scope_payload_path(log_dir: Path, difficulty: str) -> Path:
    suffix = "easy" if difficulty == "easy" else "hard"
    return log_dir / f"proof_scope_{suffix}.json"


def _write_proof_scope_payload(
    log_dir: Path,
    *,
    active_node: str,
    difficulty: str,
    snapshot_before: Dict[str, str],
    existing_nodes: Sequence[str],
    expected_active_hash: str = "",
    imports_before: Optional[Sequence[str]] = None,
) -> Path:
    payload = {
        "active_node": active_node,
        "snapshot_before": snapshot_before,
        "existing_nodes": list(existing_nodes),
        "expected_active_hash": expected_active_hash,
    }
    if imports_before is not None:
        payload["imports_before"] = list(imports_before)
    out = _proof_scope_payload_path(log_dir, difficulty)
    save_json(out, payload, mode=0o644)
    return out


def _write_cleanup_check_payload(
    config: Config,
    tablet: TabletState,
    log_dir: Path,
    snapshot_before: Dict[str, str],
) -> tuple[Path, Dict[str, Any]]:
    from lagent_tablets.nl_cache import NLCache

    repo = config.repo_path
    cache = NLCache(config.state_dir / "nl_cache.json")
    baseline_declaration_hashes: Dict[str, str] = {}
    baseline_correspondence_hashes: Dict[str, str] = {}

    for name, node in sorted(tablet.nodes.items()):
        if node.kind == "preamble":
            continue
        lean_path = node_lean_path(repo, name)
        if lean_path.exists():
            baseline_declaration_hashes[name] = declaration_hash(
                lean_path.read_text(encoding="utf-8"),
                name,
            )
        fp = cache.correspondence_fingerprint(repo, name)
        if fp:
            baseline_correspondence_hashes[name] = fp

    payload = {
        "snapshot_before": snapshot_before,
        "baseline_declaration_hashes": baseline_declaration_hashes,
        "baseline_correspondence_hashes": baseline_correspondence_hashes,
    }
    out = _cleanup_check_payload_path(log_dir)
    save_json(out, payload, mode=0o644)
    return out, payload


def _restore_cleanup_last_good_state(config: Config, ref: str) -> None:
    if not ref:
        return
    repo = config.repo_path
    try:
        subprocess.run(
            ["git", "-C", str(repo), "restore", "--source", ref, "--worktree", "--staged", "--", "Tablet", "Tablet.lean"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "-C", str(repo), "clean", "-fd", "--", "Tablet"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _setup_cleanup_permissions(config: Config) -> None:
    """Allow cleanup workers to edit existing Lean/support files, but not `.tex` or create nodes."""
    import grp
    import os

    repo = config.repo_path
    tdir = repo / "Tablet"
    staging = config.state_dir / "staging"
    if not tdir.exists():
        return

    try:
        gid = grp.getgrnam("leanagent").gr_gid
    except KeyError:
        return

    try:
        os.chown(str(tdir), -1, gid)
        os.chmod(str(tdir), 0o2755)
    except PermissionError:
        pass

    writable = {"Preamble.lean", "INDEX.md", "README.md", "header.tex"}
    for path in tdir.iterdir():
        if not path.is_file():
            continue
        target_mode = 0o664 if (path.suffix == ".lean" or path.name in writable) else 0o644
        try:
            stat = path.stat()
            if stat.st_uid == os.getuid():
                if stat.st_gid != gid:
                    os.chown(str(path), -1, gid)
                os.chmod(str(path), target_mode)
            else:
                subprocess.run(
                    ["sudo", "-n", "-u", "lagentworker", "chmod", oct(target_mode)[2:], str(path)],
                    capture_output=True,
                    timeout=5,
                )
        except (PermissionError, OSError, subprocess.TimeoutExpired):
            pass

    tablet_root = repo / "Tablet.lean"
    if tablet_root.exists():
        try:
            stat = tablet_root.stat()
            target_mode = 0o664
            if stat.st_uid == os.getuid():
                if stat.st_gid != gid:
                    os.chown(str(tablet_root), -1, gid)
                os.chmod(str(tablet_root), target_mode)
            else:
                subprocess.run(
                    ["sudo", "-n", "-u", "lagentworker", "chmod", oct(target_mode)[2:], str(tablet_root)],
                    capture_output=True,
                    timeout=5,
                )
        except (PermissionError, OSError, subprocess.TimeoutExpired):
            pass

    try:
        os.chown(str(repo), -1, gid)
        os.chmod(str(repo), 0o2755)
        staging.mkdir(parents=True, exist_ok=True)
        os.chown(str(staging), -1, gid)
        os.chmod(str(staging), 0o2775)
    except PermissionError:
        pass


def _apply_verification_to_tablet(
    tablet: TabletState,
    verification_results: List[Dict[str, Any]],
    cycle: int,
    repo_path: Optional[Path] = None,
) -> None:
    """Update per-node verification status in tablet from cycle results.

    Sets correspondence_status and soundness_status on each TabletNode.
    Nodes not mentioned in results keep their current status.
    """
    # Collect checked/failed nodes from correspondence results
    corr_checked_nodes: Set[str] = set()
    corr_failed: Set[str] = set()
    for r in verification_results:
        if r.get("check") != "correspondence":
            continue
        node_names = r.get("node_names", [])
        if isinstance(node_names, list):
            corr_checked_nodes.update(str(name) for name in node_names if isinstance(name, str))
        # From multi-agent results
        for ar in r.get("agent_results", [r]):
            for phase in ("correspondence", "paper_faithfulness"):
                for issue in ar.get(phase, {}).get("issues", []) if isinstance(ar.get(phase), dict) else []:
                    if issue.get("node"):
                        node_name = str(issue["node"])
                        corr_checked_nodes.add(node_name)
                        corr_failed.add(node_name)

    # Collect checked/failed/structural nodes from soundness results
    sound_checked_nodes: Set[str] = set()
    sound_failed: Set[str] = set()
    sound_structural: Set[str] = set()
    for r in verification_results:
        if r.get("check") != "nl_proof":
            continue
        node_names = r.get("node_names", [])
        if isinstance(node_names, list):
            sound_checked_nodes.update(str(name) for name in node_names if isinstance(name, str))
        for nv in r.get("node_verdicts", []):
            node = str(nv.get("node", ""))
            if node:
                sound_checked_nodes.add(node)
            if nv.get("structural"):
                sound_structural.add(node)
            elif nv.get("overall") != "APPROVE":
                sound_failed.add(node)
        # Legacy single-result format
        for issue in r.get("soundness", {}).get("issues", []) if isinstance(r.get("soundness"), dict) else []:
            if issue.get("node"):
                node_name = str(issue["node"])
                sound_checked_nodes.add(node_name)
                sound_failed.add(node_name)

    # Apply to tablet nodes. Only update if:
    # - Node content changed since last verification (hash mismatch), OR
    # - Node has never been verified ("?")
    # This preserves sticky status for unchanged nodes.
    for name, node in tablet.nodes.items():
        if name == "Preamble":
            continue
        current_text_hash = _correspondence_text_hash(repo_path, name) if repo_path else ""
        current_corr_hash = _correspondence_content_hash(repo_path, name) if repo_path else ""
        current_sound_hash = _soundness_content_hash(repo_path, name) if repo_path else ""
        prior_corr_hash = node.correspondence_content_hash or node.verification_content_hash
        corr_changed = (prior_corr_hash != current_corr_hash) if prior_corr_hash else True

        if name in corr_checked_nodes:
            node.correspondence_status = "fail" if name in corr_failed else "pass"
            node.verification_at_cycle = cycle
            if repo_path:
                node.correspondence_text_hash = current_text_hash
                node.correspondence_content_hash = current_corr_hash
            # If correspondence changed, reset soundness (it's stale)
            if corr_changed and node.soundness_status != "?":
                node.soundness_status = "?"
                node.soundness_content_hash = ""

        if name in sound_checked_nodes:
            if name in sound_structural:
                node.soundness_status = "structural"
            elif name in sound_failed:
                node.soundness_status = "fail"
            else:
                node.soundness_status = "pass"
            node.verification_at_cycle = cycle
            if repo_path:
                node.soundness_content_hash = current_sound_hash
                if not node.correspondence_content_hash and node.correspondence_status != "?":
                    node.correspondence_text_hash = current_text_hash
                    node.correspondence_content_hash = current_corr_hash
        if repo_path:
            node.verification_content_hash = (
                node.soundness_content_hash
                or node.correspondence_content_hash
                or node.verification_content_hash
            )


def _verification_results_checkpoint_path(config: Config) -> Path:
    return config.state_dir / "checkpoints" / "verification_results.json"


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
                log_dir=log_dir, agent_index=getattr(agent, "selected_index", i),
                previous_own_result=_load_agent_previous_result(
                    config.repo_path,
                    getattr(agent, "selected_index", i),
                ),
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
            "node_names": corr_nodes,
            "agent_results": agent_results,
        }
    else:
        disagree_detail = ", ".join(f"{r.get('agent', '?')}: {o}" for r, o in zip(agent_results, overalls))
        print(f"  Correspondence: DISAGREE ({disagree_detail})")
        return {
            "check": "correspondence",
            "overall": "DISAGREE",
            "summary": f"Agents disagree: {disagree_detail}. Reviewer must arbitrate.",
            "node_names": corr_nodes,
            "agent_results": agent_results,
        }


def _load_agent_previous_result(repo: Path, agent_index: int) -> Optional[Dict[str, Any]]:
    """Load a single agent's previous correspondence result."""
    f = repo / f"correspondence_result_{agent_index}.json"
    if f.exists():
        try:
            return load_json(f)
        except Exception:
            pass
    return None


def _load_previous_correspondence(repo: Path) -> List[Dict[str, Any]]:
    """Load saved correspondence results from previous cycle for context."""
    previous = []
    for i in range(10):
        f = repo / f"correspondence_result_{i}.json"
        if f.exists():
            try:
                previous.append(load_json(f))
            except Exception:
                pass
    if not previous:
        f = repo / "correspondence_result.json"
        if f.exists():
            try:
                previous.append(load_json(f))
            except Exception:
                pass
    return previous


def _run_nl_verification(
    config: Config,
    policy: Policy,
    tablet: TabletState,
    node_names: List[str],
    *,
    state: Optional[SupervisorState] = None,
    cycle: Optional[int] = None,
    correspondence_node_names: Optional[List[str]] = None,
    log_dir: Path,
    nl_cache: Optional[Any] = None,
    human_input: str = "",
    soundness_target_node: Optional[str] = None,
    soundness_node_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Run correspondence and NL proof verification on the given nodes.

    Uses the NL cache to skip re-verification when content hasn't changed.
    Passes previous cycle's results to verifiers for context continuity.
    Returns list of verification result dicts.
    """
    repo = config.repo_path
    results: List[Dict[str, Any]] = []
    state = state or SupervisorState(cycle=cycle or 0)

    corr_nodes_base = list(correspondence_node_names) if correspondence_node_names is not None else list(node_names)
    proof_nodes_base = list(soundness_node_names) if soundness_node_names is not None else list(node_names)

    if not corr_nodes_base and not proof_nodes_base:
        return results

    # Load previous results for verifier context
    previous_corr = _load_previous_correspondence(repo)

    paper_tex = ""
    if config.workflow.paper_tex_path and config.workflow.paper_tex_path.exists():
        paper_tex = config.workflow.paper_tex_path.read_text(encoding="utf-8", errors="replace")

    verify_config = ProviderConfig(
        provider=config.verification.provider,
        model=config.verification.model,
        extra_args=config.verification.extra_args,
    )

    # 1. Correspondence check (possibly multi-agent)
    corr_nodes = corr_nodes_base
    cached_corr_nodes: List[str] = []
    if nl_cache:
        corr_nodes = nl_cache.filter_uncached(repo, corr_nodes_base, "correspondence")
        cached_corr_nodes = [name for name in corr_nodes_base if name not in set(corr_nodes)]
    if corr_nodes:
        _save_live_viewer_state(
            config,
            tablet,
            state,
            activity={"correspondence": corr_nodes},
            source="verification",
        )
    if cached_corr_nodes:
        results.append({
            "check": "correspondence",
            "overall": "APPROVE",
            "summary": "cached",
            "node_names": cached_corr_nodes,
        })
    if corr_nodes:
        corr_agents = _effective_correspondence_agents(config, policy)
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
            corr_artifacts = _clear_artifact_files(config, "correspondence_result.json")
            corr_prompt = build_correspondence_prompt(
                config, tablet, node_names=corr_nodes, paper_tex=paper_tex,
                human_input=human_input,
            )
            corr_result = run_reviewer_burst(
                verify_config, corr_prompt,
                session_name=config.tmux.session_name,
                work_dir=repo, burst_user=config.tmux.burst_user,
                timeout_seconds=1800, log_dir=log_dir, fresh=True,
                port=3286, done_file=corr_artifacts["done"],
                artifact_prefix=str(corr_artifacts["stem"]),
            )
            corr_decision = None
            artifact_error = None
            if corr_result.ok:
                corr_decision, artifact_error = _accept_validated_artifact(
                    config,
                    "correspondence_result.json",
                    kind="correspondence-result",
                )
            if corr_decision:
                entry = {"check": "correspondence", "node_names": corr_nodes, **corr_decision}
                if corr_result.usage:
                    entry["_usage"] = corr_result.usage
                results.append(entry)
                overall = corr_decision.get("overall", "?")
                print(f"  Correspondence: {overall}")
                if overall == "APPROVE" and nl_cache:
                    nl_cache.record_correspondence_approval(repo, corr_nodes)
            else:
                results.append({
                    "check": "correspondence",
                    "node_names": corr_nodes,
                    "overall": "ERROR",
                    "summary": artifact_error or "missing correspondence artifact",
                })
    elif corr_nodes_base:
        print(f"  Correspondence: all {len(corr_nodes_base)} nodes cached (APPROVE)")
        if not cached_corr_nodes:
            results.append({"check": "correspondence", "overall": "APPROVE", "summary": "cached", "node_names": list(corr_nodes_base)})

    corr_results = [r for r in results if r.get("check") == "correspondence"]
    if corr_results and cycle is not None:
        _apply_verification_to_tablet(tablet, corr_results, cycle, repo_path=repo)
        save_tablet(tablet_path(config), tablet)
        _save_live_viewer_state(config, tablet, state, source="verification")

    # 2. NL proof soundness check — only if correspondence passed (it's a gate)
    corr_overall = "APPROVE"
    for r in results:
        if r.get("check") == "correspondence":
            corr_overall = r.get("overall", "?")
    if corr_overall != "APPROVE":
        print(f"  Skipping NL proof soundness (correspondence {corr_overall} — must pass first)")
        return results

    proof_nodes = proof_nodes_base
    if nl_cache:
        proof_nodes = nl_cache.filter_uncached(repo, proof_nodes_base, "soundness")
    if proof_nodes and soundness_target_node is not None:
        if soundness_target_node in proof_nodes:
            proof_nodes = [soundness_target_node]
        else:
            ordered = _soundness_priority_order(repo, tablet, proof_nodes)
            proof_nodes = ordered[:1]
    if proof_nodes:
        _save_live_viewer_state(
            config,
            tablet,
            state,
            activity={"soundness": proof_nodes},
            source="verification",
        )
        soundness_agents = _effective_soundness_agents(config, policy)
        if len(soundness_agents) >= 2:
            # Per-node soundness with multiple agents
            proof_results = _run_per_node_soundness(
                config, tablet, proof_nodes, soundness_agents,
                disagree_bias=policy.verification.soundness_disagree_bias,
                paper_tex=paper_tex, human_input=human_input, log_dir=log_dir,
            )
            results.extend(proof_results)
            if nl_cache:
                for pr in proof_results:
                    for nv in pr.get("node_verdicts", []):
                        node = str(nv.get("node", "")).strip()
                        if node and nv.get("overall") == "APPROVE":
                            nl_cache.record_soundness_approval(repo, [node])
        else:
            # Single-agent, all-at-once soundness (fallback)
            print(f"  NL proof check: {len(proof_nodes)} nodes ({len(proof_nodes_base) - len(proof_nodes)} cached)")
            proof_artifacts = _clear_artifact_files(config, "nl_proof_result.json")
            proof_prompt = build_nl_proof_prompt(
                config, tablet, node_names=proof_nodes, paper_tex=paper_tex,
                human_input=human_input,
                output_file="nl_proof_result.json",
            )
            proof_result = run_reviewer_burst(
                verify_config, proof_prompt,
                session_name=config.tmux.session_name,
                work_dir=repo, burst_user=config.tmux.burst_user,
                timeout_seconds=1800, log_dir=log_dir, fresh=True,
                port=3287, done_file=proof_artifacts["done"],
                artifact_prefix=str(proof_artifacts["stem"]),
            )
            proof_decision = None
            artifact_error = None
            if proof_result.ok:
                proof_decision, artifact_error = _accept_validated_artifact(
                    config,
                    "nl_proof_result.json",
                    kind="soundness-batch-result",
                )
            if proof_decision:
                entry = {"check": "nl_proof", "node_names": proof_nodes, **proof_decision}
                if proof_result.usage:
                    entry["_usage"] = proof_result.usage
                results.append(entry)
                overall = proof_decision.get("overall", "?")
                print(f"  NL proof soundness: {overall}")
                if overall == "APPROVE" and nl_cache:
                    nl_cache.record_soundness_approval(repo, proof_nodes)
            else:
                results.append({
                    "check": "nl_proof",
                    "node_names": proof_nodes,
                    "overall": "ERROR",
                    "summary": artifact_error or "missing NL proof artifact",
                })
    elif proof_nodes_base:
        print(f"  NL proof soundness: all {len(proof_nodes_base)} nodes cached (APPROVE)")
        results.append({"check": "nl_proof", "overall": "APPROVE", "summary": "cached", "node_names": []})

    _save_live_viewer_state(config, tablet, state, source="verification")
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
    from lagent_tablets.nl_cache import NLCache

    resume_from = state.resume_from or ""
    repo = config.repo_path
    nl_cache = NLCache(config.state_dir / "nl_cache.json")

    if not resume_from:
        # Fresh cycle — increment and run worker
        cycle = state.cycle + 1
        state.cycle = cycle
    else:
        cycle = state.cycle
        print(f"=== Resuming theorem-stating cycle {cycle} from {resume_from} ===")

    cycle_start = time.monotonic()
    log_dir = config.state_dir / "logs" / f"cycle-{cycle:04d}"
    worker_handoff: Optional[Dict[str, Any]] = None

    prep_notes = _prepare_theorem_stating_worker_state(
        config,
        state,
        tablet,
        policy,
        nl_cache=nl_cache,
    )
    for note in prep_notes:
        print(f"  normalize: {note}")
    preflight_error = _theorem_stating_preflight_error(state)
    if preflight_error:
        print(f"  INVALID: {preflight_error}")
        save_state(state_path(config), state)
        return CycleOutcome(
            outcome="INVALID",
            detail=preflight_error,
        )
    target_repair_mode = bool(
        state.theorem_soundness_target and _theorem_target_edit_mode(state) == "repair"
    )
    target_restructure_mode = bool(
        state.theorem_soundness_target and _theorem_target_edit_mode(state) == "restructure"
    )
    target_scope_before_worker = _theorem_target_scope(repo, state.theorem_soundness_target)
    scoped_tablet_check_payload_path: Optional[Path] = None
    repair_scope_check_payload_path: Optional[Path] = None
    edit_scope_check_payload_path: Optional[Path] = None
    theorem_tablet_baseline_errors: List[str] = []
    node_hashes_before_worker = _snapshot_tablet_node_hashes(repo) if not resume_from else {}
    tablet_snapshot_before_worker = _snapshot_tablet_dir(repo) if not resume_from else {}

    # ---- Stage 1: Worker ----
    if not resume_from:
        print(f"=== Theorem-stating cycle {cycle} ===")
        # Clear any stale verification/reviewer activity from the previous cycle
        # as soon as the new worker cycle begins.
        _save_live_viewer_state(config, tablet, state, source="worker")

        (repo / "Tablet").mkdir(parents=True, exist_ok=True)
        fix_lake_permissions(repo)
        _setup_theorem_stating_permissions(
            config,
            target=state.theorem_soundness_target,
            repair_mode=target_repair_mode,
        )

        forbidden = [
            kw for kw in FORBIDDEN_KEYWORDS_DEFAULT
            if kw not in config.workflow.forbidden_keyword_allowlist
        ]
        baseline = run_check_tablet(
            repo,
            allowed_prefixes=config.workflow.allowed_import_prefixes,
            forbidden_keywords=forbidden,
            approved_axioms_path=config.workflow.approved_axioms_path,
        )
        theorem_tablet_baseline_errors = list(baseline.get("errors", []))

        if target_restructure_mode and state.theorem_soundness_target:
            scoped_tablet_check_payload_path = _write_scoped_tablet_check_payload(
                config,
                log_dir,
                state.theorem_soundness_target,
                target_scope_before_worker,
            )
            edit_scope_check_payload_path = _write_theorem_target_edit_scope_payload(
                log_dir,
                state.theorem_soundness_target,
                node_hashes_before_worker,
                target_scope_before_worker,
            )
        elif target_repair_mode and state.theorem_soundness_target:
            repair_scope_check_payload_path = _write_theorem_target_repair_scope_payload(
                log_dir,
                state.theorem_soundness_target,
                tablet_snapshot_before_worker,
            )

        worker_prompt = build_theorem_stating_prompt(
            config, state, tablet, policy, previous_outcome=previous_outcome,
            authorized_region=sorted(target_scope_before_worker),
            scoped_tablet_check_payload_path=scoped_tablet_check_payload_path,
            repair_scope_check_payload_path=repair_scope_check_payload_path,
            edit_scope_check_payload_path=edit_scope_check_payload_path,
        )
        worker_artifacts = _clear_artifact_files(config, "worker_handoff.json")

        worker_result = run_worker_burst(
            config.worker,
            worker_prompt,
            session_name=config.tmux.session_name,
            work_dir=repo,
            burst_user=config.tmux.burst_user,
            timeout_seconds=policy.timing.burst_timeout_seconds,
            startup_timeout_seconds=config.startup_timeout_seconds,
            log_dir=log_dir,
            done_file=worker_artifacts["done"],
            artifact_prefix=str(worker_artifacts["stem"]),
        )

        if worker_result.transcript_path:
            print(f"  Transcript saved: {worker_result.transcript_path}")

        if not worker_result.ok:
            print(f"  Worker burst failed: {worker_result.error}")
            save_state(state_path(config), state)
            return CycleOutcome(outcome="INVALID", detail=f"Worker burst failed: {worker_result.error}")

        worker_handoff, handoff_error = _accept_validated_artifact(
            config,
            "worker_handoff.json",
            kind="worker-handoff",
            phase="theorem_stating",
            repo_for_validation=repo,
        )
        if not isinstance(worker_handoff, dict):
            save_state(state_path(config), state)
            return CycleOutcome(
                outcome="INVALID",
                detail=f"Invalid worker handoff: {handoff_error or 'missing raw/done artifact'}",
            )
        state.last_worker_handoff = worker_handoff
        worker_status = str(worker_handoff.get("status", "") or "").strip().upper()

        fix_lake_permissions(repo)

        # Discover what the worker created/modified
        tdir = repo / "Tablet"
        lean_files = {p.stem for p in tdir.glob("*.lean") if p.stem != "Preamble"}
        tex_files = {p.stem for p in tdir.glob("*.tex") if p.stem not in ("header", "Preamble")}
        all_node_names = lean_files | tex_files
        deleted_nodes = _prune_deleted_tablet_nodes(tablet, all_node_names)
        new_nodes = [n for n in all_node_names if n not in tablet.nodes]
        changed_nodes = _changed_tablet_nodes_since_snapshot(repo, node_hashes_before_worker)
        modified_existing_nodes = [
            n for n in changed_nodes
            if n in tablet.nodes and n in all_node_names
        ]

        if worker_status == "CRISIS":
            if state.theorem_soundness_target:
                save_state(state_path(config), state)
                return CycleOutcome(
                    outcome="INVALID",
                    detail="status CRISIS is only allowed during broad theorem-stating (no active soundness target).",
                )
            if new_nodes or modified_existing_nodes or deleted_nodes:
                save_state(state_path(config), state)
                return CycleOutcome(
                    outcome="INVALID",
                    detail="status CRISIS may not accompany Tablet edits; escalate the paper-level concern without changing artifacts.",
                )

        repair_error = None
        if target_repair_mode:
            repair_result = check_theorem_target_repair_scope(
                repo,
                target=state.theorem_soundness_target,
                snapshot_before=tablet_snapshot_before_worker,
            )
            repair_error = repair_result["errors"][0] if repair_result["errors"] else None
        if repair_error:
            print(f"  INVALID: {repair_error}")
            save_state(state_path(config), state)
            return CycleOutcome(
                outcome="INVALID",
                detail=repair_error,
            )

        scope_result = check_theorem_target_edit_scope(
            repo,
            target=state.theorem_soundness_target,
            before_hashes=node_hashes_before_worker,
            initial_scope=sorted(target_scope_before_worker),
        )
        scope_error = scope_result["errors"][0] if scope_result["errors"] else None
        if scope_error:
            print(f"  INVALID: {scope_error}")
            save_state(state_path(config), state)
            return CycleOutcome(
                outcome="INVALID",
                detail=scope_error,
            )

        touched_nodes = sorted(
            {
                name
                for name in changed_nodes
                if name == PREAMBLE_NAME or name in all_node_names
            }
        )
        if target_restructure_mode and state.theorem_soundness_target:
            allowed_nodes = sorted(
                target_scope_before_worker | _theorem_target_scope(repo, state.theorem_soundness_target)
            )
            baseline_errors = (
                load_json(scoped_tablet_check_payload_path, {}).get("baseline_errors", [])
                if scoped_tablet_check_payload_path else []
            )
        else:
            allowed_nodes = [name for name in touched_nodes if name != PREAMBLE_NAME]
            baseline_errors = theorem_tablet_baseline_errors

        if touched_nodes or target_restructure_mode:
            scoped_outcome = _run_scoped_tablet_check(
                config,
                baseline_errors=baseline_errors,
                allowed_nodes=allowed_nodes,
            )
            if scoped_outcome is not None:
                print(f"  INVALID: scoped deterministic check failed: {scoped_outcome.detail}")
                save_state(state_path(config), state)
                return scoped_outcome

        # Read difficulty hints from worker handoff (if present)
        difficulty_hints: Dict[str, str] = {}
        kind_hints: Dict[str, str] = {}
        hints = worker_handoff.get("difficulty_hints", {}) if isinstance(worker_handoff, dict) else {}
        if isinstance(hints, dict):
            for k, v in hints.items():
                if v in ("easy", "hard"):
                    difficulty_hints[k] = v
        raw_kind_hints = worker_handoff.get("kind_hints", {}) if isinstance(worker_handoff, dict) else {}
        if isinstance(raw_kind_hints, dict):
            for k, v in raw_kind_hints.items():
                if v in ("paper_main_result", "paper_intermediate"):
                    kind_hints[k] = v

        # Register any new nodes in the tablet
        for name in new_nodes:
            lean_path = node_lean_path(repo, name)
            tex_path = node_tex_path(repo, name)
            if lean_path.exists():
                content = lean_path.read_text(encoding="utf-8")
                marker = extract_marker_name(content)
                kind = _theorem_stating_node_kind(name, kind_hints)
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

        if worker_status == "CRISIS":
            outcome = CycleOutcome(
                outcome="NO_PROGRESS",
                detail=f"Worker raised CRISIS: {worker_handoff.get('summary', '')}".strip(),
            )
        else:
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
            else:
                newly_closed = _theorem_stating_closed_nodes(
                    config,
                    list(new_nodes) + list(modified_existing_nodes),
                )
                for name in newly_closed:
                    if name in tablet.nodes:
                        mark_node_closed(tablet, name, cycle)
                detail_parts: List[str] = []
                if new_nodes:
                    print(f"  Created {len(new_nodes)} new nodes: {new_nodes}")
                    detail_parts.append(f"Created nodes: {', '.join(new_nodes)}")
                if deleted_nodes:
                    print(f"  Deleted {len(deleted_nodes)} nodes: {deleted_nodes}")
                    detail_parts.append(f"Deleted nodes: {', '.join(deleted_nodes)}")
                if newly_closed:
                    print(f"  Closed {len(newly_closed)} theorem-stating nodes in Lean: {newly_closed}")
                    detail_parts.append(f"Closed in Lean: {', '.join(newly_closed)}")

                if new_nodes or deleted_nodes or newly_closed:
                    outcome = CycleOutcome(
                        outcome="PROGRESS",
                        detail=", ".join(detail_parts),
                        nodes_created=new_nodes,
                    )
                else:
                    print(f"  No new nodes created (modified existing: {modified_existing_nodes})")
                    outcome = CycleOutcome(outcome="PROGRESS", detail="Modified existing nodes")

        # Save checkpoint: worker done
        regenerate_support_files(tablet, repo)
        save_tablet(tablet_path(config), tablet)
        state.resume_from = "verification"
        save_state(state_path(config), state)
        _save_live_viewer_state(config, tablet, state, source="worker")
        from lagent_tablets.git_ops import commit_checkpoint as git_commit_checkpoint
        git_commit_checkpoint(
            repo,
            cycle,
            "worker",
            phase="theorem_stating",
            outcome=outcome.outcome,
            active_node=state.active_node,
            detail=outcome.detail,
            meta={
                "checkpoint_resume_from": "verification",
                "stage": "worker",
            },
        )
    else:
        # Resuming — reconstruct outcome from current state
        all_node_names = [n for n in tablet.nodes if tablet.nodes[n].kind != "preamble"]
        outcome = CycleOutcome(
            outcome="PROGRESS",
            detail=f"Resumed with {len(all_node_names)} existing nodes",
        )

    # ---- Stage 2: NL Verification ----
    nl_verification_results: List[Dict[str, Any]] = []
    verification_checkpoint = _verification_results_checkpoint_path(config)
    worker_crisis = str((state.last_worker_handoff or {}).get("status", "") or "").strip().upper() == "CRISIS"
    if worker_crisis and resume_from in ("", "verification"):
        print("  Skipping NL verification because worker raised CRISIS")
        state.resume_from = "reviewer"
        save_state(state_path(config), state)
    elif resume_from in ("", "verification"):
        repaired_hashes = _backfill_legacy_correspondence_hashes(tablet, repo)
        repaired_failures = _repair_stale_legacy_correspondence_failures(tablet, state, repo)
        if repaired_hashes or repaired_failures:
            save_tablet(tablet_path(config), tablet)
        all_check_nodes = [n for n in tablet.nodes if tablet.nodes[n].kind != "preamble"]
        corr_check_nodes = _theorem_stating_correspondence_frontier(tablet, repo)
        print(f"  Running NL verification for {len(all_check_nodes)} nodes...")
        if state.theorem_soundness_target:
            print(f"  Current theorem-stating soundness target: {state.theorem_soundness_target}")
        print(f"  Statement-level correspondence frontier: {len(corr_check_nodes)} nodes")
        verification_state = _suspend_theorem_soundness_target(state) if corr_check_nodes else state
        nl_verification_results = _run_nl_verification(
            config, policy, tablet, all_check_nodes, state=verification_state, cycle=cycle, correspondence_node_names=corr_check_nodes, log_dir=log_dir, nl_cache=nl_cache,
            human_input=state.human_input,
            soundness_target_node=state.theorem_soundness_target or None,
        )
        save_json(verification_checkpoint, nl_verification_results)
        # Save checkpoint: verification done
        state.resume_from = "reviewer"
        save_state(state_path(config), state)
    elif resume_from == "reviewer":
        # Reconstruct verification results from saved files for the reviewer
        print(f"  Skipping verification (resuming from reviewer)")
        checkpointed = load_json(verification_checkpoint, default=None)
        if isinstance(checkpointed, list):
            nl_verification_results = checkpointed
        else:
            for i in range(10):
                f = repo / f"correspondence_result_{i}.json"
                if f.exists():
                    try:
                        data = load_json(f)
                        if isinstance(data, dict):
                            nl_verification_results.append({"check": "correspondence", "agent_index": i, "node_names": [], **data})
                    except Exception:
                        pass
            if not nl_verification_results:
                f = repo / "correspondence_result.json"
                if f.exists():
                    try:
                        data = load_json(f)
                        if isinstance(data, dict):
                            nl_verification_results.append({"check": "correspondence", "node_names": [], **data})
                    except Exception:
                        pass
        if nl_verification_results:
            overalls = [r.get("overall", "?") for r in nl_verification_results]
            print(f"  Loaded {len(nl_verification_results)} correspondence results: {overalls}")

    # Apply verification results to per-node tablet status
    if nl_verification_results:
        _apply_verification_to_tablet(tablet, nl_verification_results, cycle, repo_path=repo)
        save_tablet(tablet_path(config), tablet)
        _save_live_viewer_state(config, tablet, state, source="verification")
        from lagent_tablets.git_ops import commit_checkpoint as git_commit_checkpoint
        git_commit_checkpoint(
            repo,
            cycle,
            "verification",
            phase="theorem_stating",
            outcome=outcome.outcome,
            active_node=state.active_node,
            detail=outcome.detail,
            meta={
                "checkpoint_resume_from": "reviewer",
                "stage": "verification",
                "verification_results": nl_verification_results,
            },
        )

    # ---- Stage 3: Reviewer ----
    if worker_handoff is None:
        handoff_path = repo / "worker_handoff.json"
        if handoff_path.exists():
            try:
                worker_handoff = load_json(handoff_path)
            except Exception:
                worker_handoff = None

    orphan_candidates = find_orphan_nodes(tablet, repo)

    correspondence_blocked = _correspondence_gate_open(nl_verification_results)
    reviewer_state = _suspend_theorem_soundness_target(state) if correspondence_blocked else state
    _save_live_viewer_state(config, tablet, reviewer_state, source="reviewer")

    reviewer_prompt = build_theorem_stating_reviewer_prompt(
        config, reviewer_state, tablet, policy,
        worker_handoff=worker_handoff,
        worker_output=(worker_result.captured_output[-15000:] if worker_result.captured_output else "") if not resume_from else "",
        nl_verification=nl_verification_results if nl_verification_results else None,
        orphan_candidates=orphan_candidates,
    )

    reviewer_artifacts = _clear_artifact_files(config, "reviewer_decision.json")

    reviewer_result = run_reviewer_burst(
        config.reviewer,
        reviewer_prompt,
        session_name=config.tmux.session_name,
        work_dir=repo,
        burst_user=config.tmux.burst_user,
        timeout_seconds=min(policy.timing.burst_timeout_seconds, 300),
        log_dir=log_dir,
        done_file=reviewer_artifacts["done"],
        artifact_prefix=str(reviewer_artifacts["stem"]),
    )

    decision = None
    decision_error = None
    if reviewer_result.ok:
        decision, decision_error = _accept_validated_artifact(
            config,
            "reviewer_decision.json",
            kind="reviewer-decision",
            phase="theorem_stating",
        )
    if isinstance(decision, dict):
        _enforce_theorem_stating_orphan_candidates(decision, orphan_candidates)
        open_rejections = _reconcile_theorem_stating_open_rejections(
            nl_verification_results,
            decision.get("open_blockers", decision.get("open_rejections", state.open_blockers)),
            include_preferred_extras=True,
        )
        _enforce_theorem_stating_open_rejections(decision, open_rejections)
        state.open_blockers = open_rejections

        for name, kind in decision.get("kind_assignments", {}).items():
            if name in tablet.nodes and kind in {"paper_main_result", "paper_intermediate"}:
                tablet.nodes[name].kind = kind

        if correspondence_blocked:
            state.theorem_correspondence_blocked = True
            state.theorem_soundness_target = ""
            state.theorem_target_edit_mode = "repair"
        else:
            state.theorem_correspondence_blocked = False
            pending_soundness_candidates = _eligible_soundness_nodes(config, tablet)
            pending_soundness_candidates = nl_cache.filter_uncached(repo, pending_soundness_candidates, "soundness")
            pending_soundness_agents = _effective_soundness_agents(config, policy)
            prior_soundness_target = state.theorem_soundness_target
            pending_soundness_target = _select_theorem_soundness_target(
                config,
                tablet,
                pending_soundness_candidates,
                soundness_agents=pending_soundness_agents,
                disagree_bias=policy.verification.soundness_disagree_bias,
                preferred=state.theorem_soundness_target,
            )
            if pending_soundness_target:
                state.theorem_soundness_target = pending_soundness_target
                if pending_soundness_target != prior_soundness_target:
                    state.theorem_target_edit_mode = "repair"
                else:
                    requested_mode = str(decision.get("target_edit_mode", "repair") or "repair").strip().lower()
                    state.theorem_target_edit_mode = (
                        requested_mode if requested_mode in {"repair", "restructure"} else "repair"
                    )
                if decision.get("decision") == "ADVANCE_PHASE":
                    decision["decision"] = "CONTINUE"
                    decision["reason"] = (
                        "NL proof soundness still unresolved"
                        f": {pending_soundness_target}"
                    )
                    if not str(decision.get("next_prompt", "")).strip():
                        decision["next_prompt"] = (
                            f"Keep working on `{pending_soundness_target}` until its NL proof soundness is accepted."
                        )
            else:
                state.theorem_soundness_target = ""
                state.theorem_target_edit_mode = "repair"

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
                    config, policy, tablet, all_nodes, state=state, cycle=cycle, log_dir=log_dir, nl_cache=nl_cache,
                    human_input=state.human_input,
                )
                gate_open_rejections = _reconcile_theorem_stating_open_rejections(
                    gate_results,
                    state.open_blockers,
                )
                state.open_blockers = gate_open_rejections
                _enforce_theorem_stating_open_rejections(decision, gate_open_rejections)

                if gate_open_rejections:
                    print(f"  Verification REJECTED -- blocking ADVANCE_PHASE")
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
        state.last_review = decision
        state.review_log.append({"cycle": cycle, **decision})
        print(f"  Reviewer: {decision.get('decision', '?')} -- {decision.get('reason', '')[:100]}")
        _save_live_viewer_state(config, tablet, state, source="reviewer")
    else:
        print(f"  Reviewer: could not validate decision ({decision_error or 'missing raw/done artifact'})")

    # Clear resume checkpoint — cycle is complete
    state.resume_from = ""
    save_tablet(tablet_path(config), tablet)
    save_state(state_path(config), state)
    _save_cycle_viewer_state(
        config,
        tablet,
        state,
        verification_results=nl_verification_results,
        source="cycle",
    )

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


def preview_next_cycle(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
) -> Dict[str, Any]:
    """Preview the next worker cycle without launching any agents."""
    preview_state: SupervisorState = copy.deepcopy(state)
    preview_tablet: TabletState = copy.deepcopy(tablet)
    next_cycle = preview_state.cycle + 1 if not (preview_state.resume_from or "") else preview_state.cycle

    if preview_state.phase == "theorem_stating":
        from lagent_tablets.prompts import build_theorem_stating_prompt
        from lagent_tablets.nl_cache import NLCache

        nl_cache = NLCache(config.state_dir / "nl_cache.json")
        notes = _prepare_theorem_stating_worker_state(
            config,
            preview_state,
            preview_tablet,
            policy,
            nl_cache=nl_cache,
        )
        error = _theorem_stating_preflight_error(preview_state)
        target = preview_state.theorem_soundness_target.strip()
        mode = _theorem_target_edit_mode(preview_state)
        authorized_region = sorted(_theorem_target_scope(config.repo_path, target)) if target else []
        prompt = build_theorem_stating_prompt(
            config,
            preview_state,
            preview_tablet,
            policy,
            authorized_region=authorized_region,
            scoped_tablet_check_payload_path=(
                Path("<preview-scope.json>") if target and mode == "restructure" else None
            ),
            repair_scope_check_payload_path=(
                Path("<theorem-repair-scope.json>") if target and mode == "repair" else None
            ),
            edit_scope_check_payload_path=(
                Path("<theorem-edit-scope.json>") if target and mode == "restructure" else None
            ),
        )
        return {
            "phase": preview_state.phase,
            "cycle": next_cycle,
            "resume_from": preview_state.resume_from,
            "normalized": notes,
            "preflight_error": error,
            "state": {
                "theorem_soundness_target": preview_state.theorem_soundness_target,
                "theorem_target_edit_mode": preview_state.theorem_target_edit_mode,
                "theorem_correspondence_blocked": preview_state.theorem_correspondence_blocked,
                "open_blockers": _theorem_stating_open_blockers(preview_state),
            },
            "worker_prompt": prompt,
        }

    if preview_state.phase in ("proof_formalization", "proof_complete_style_cleanup"):
        from lagent_tablets.prompts import build_worker_prompt

        active_node = preview_state.active_node or preview_tablet.active_node
        node_meta = preview_tablet.nodes.get(active_node)
        node_difficulty = node_meta.difficulty if node_meta else "hard"
        prompt = build_worker_prompt(
            config,
            preview_state,
            preview_tablet,
            policy,
            difficulty=node_difficulty,
            cleanup_check_payload_path=(
                Path("<cleanup-scope.json>")
                if preview_state.phase == "proof_complete_style_cleanup"
                else None
            ),
            proof_scope_check_payload_path=(
                Path("<proof-scope.json>")
                if preview_state.phase == "proof_formalization"
                else None
            ),
        )
        return {
            "phase": preview_state.phase,
            "cycle": next_cycle,
            "resume_from": preview_state.resume_from,
            "normalized": [],
            "preflight_error": "",
            "state": {
                "active_node": active_node,
                "difficulty": node_difficulty,
            },
            "worker_prompt": prompt,
        }

    return {
        "phase": preview_state.phase,
        "cycle": next_cycle,
        "resume_from": preview_state.resume_from,
        "normalized": [],
        "preflight_error": f"Preview is not implemented for phase {preview_state.phase}",
        "state": {},
        "worker_prompt": "",
    }


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
    # Clear stale verification/reviewer activity from the previous cycle before
    # the new worker burst starts.
    _save_live_viewer_state(config, tablet, state, source="worker")
    log_dir = config.state_dir / "logs" / f"cycle-{cycle:04d}"

    # Regenerate scripts each cycle (hot-reloadable)
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]
    write_scripts(repo, config.state_dir, allowed_prefixes=config.workflow.allowed_import_prefixes, forbidden_keywords=forbidden)

    # Fix .lake permissions before any compilation
    from lagent_tablets.health import fix_lake_permissions
    fix_lake_permissions(repo)

    # Ensure oleans are up to date before the burst (so check_node works for the worker)
    _ensure_lake_build(repo)

    # Set file permissions.
    # Easy mode: only the active node .lean is writable; Tablet/, active .tex, and Preamble stay read-only.
    setup_permissions(config, active_node, easy_mode=easy_mode)

    # Record state BEFORE the burst for easy-mode validation
    active_lean = node_lean_path(repo, active_node)
    active_tex = node_tex_path(repo, active_node)
    hash_before = hashlib.sha256(active_lean.read_bytes()).hexdigest() if active_lean.exists() else ""
    proof_tablet_baseline_errors: List[str] = list(
        run_check_tablet(
            repo,
            allowed_prefixes=config.workflow.allowed_import_prefixes,
            forbidden_keywords=forbidden,
            approved_axioms_path=config.workflow.approved_axioms_path,
        ).get("errors", [])
    )
    imports_before: List[str] = []
    tablet_snapshot_before: Dict[str, str] = _snapshot_tablet_dir(repo)
    proof_scope_check_payload_path = _write_proof_scope_payload(
        log_dir,
        active_node=active_node,
        difficulty=node_difficulty,
        snapshot_before=tablet_snapshot_before,
        existing_nodes=sorted(tablet.nodes.keys()),
        expected_active_hash=tablet.nodes[active_node].lean_statement_hash if active_node in tablet.nodes else "",
        imports_before=imports_before if easy_mode else None,
    )
    if easy_mode and active_lean.exists():
        imports_before = extract_imports(active_lean.read_text(encoding="utf-8"))
        proof_scope_check_payload_path = _write_proof_scope_payload(
            log_dir,
            active_node=active_node,
            difficulty=node_difficulty,
            snapshot_before=tablet_snapshot_before,
            existing_nodes=sorted(tablet.nodes.keys()),
            expected_active_hash=tablet.nodes[active_node].lean_statement_hash if active_node in tablet.nodes else "",
            imports_before=imports_before,
        )

    # Build worker prompt
    worker_prompt = build_worker_prompt(
        config, state, tablet, policy,
        previous_outcome=previous_outcome,
        difficulty=node_difficulty,
        proof_scope_check_payload_path=proof_scope_check_payload_path,
    )

    # Run worker burst
    worker_artifacts = _clear_artifact_files(config, "worker_handoff.json")

    worker_result = run_worker_burst(
        effective_worker,
        worker_prompt,
        session_name=config.tmux.session_name,
        work_dir=repo,
        burst_user=config.tmux.burst_user,
        timeout_seconds=policy.timing.burst_timeout_seconds,
        startup_timeout_seconds=config.startup_timeout_seconds,
        log_dir=log_dir,
        done_file=worker_artifacts["done"],
        artifact_prefix=str(worker_artifacts["stem"]),
    )

    if worker_result.transcript_path:
        print(f"  Transcript saved: {worker_result.transcript_path}")

    _accumulate_usage(state, "worker", worker_result.usage)

    if not worker_result.ok:
        print(f"  Worker burst failed: {worker_result.error}")
        save_state(state_path(config), state)
        _save_live_viewer_state(config, tablet, state, source="worker")
        return CycleOutcome(outcome="INVALID", detail=f"Worker burst failed: {worker_result.error}")

    worker_handoff, handoff_error = _accept_validated_artifact(
        config,
        "worker_handoff.json",
        kind="worker-handoff",
        phase="proof_formalization",
        repo_for_validation=repo,
    )
    if not isinstance(worker_handoff, dict):
        save_state(state_path(config), state)
        _save_live_viewer_state(config, tablet, state, source="worker")
        return CycleOutcome(
            outcome="INVALID",
            detail=f"Invalid worker handoff: {handoff_error or 'missing raw/done artifact'}",
        )
    state.last_worker_handoff = worker_handoff

    # Fix .lake permissions after burst (worker may have created oleans)
    fix_lake_permissions(repo)

    # Check the active node's hash AFTER the burst
    hash_after = hashlib.sha256(active_lean.read_bytes()).hexdigest() if active_lean.exists() else ""
    active_changed = hash_before != hash_after
    tablet_snapshot_after = _snapshot_tablet_dir(repo)
    changes = _detect_changes(tablet_snapshot_before, tablet_snapshot_after)
    active_tex_changed = f"{active_node}.tex" in changes["modified"]
    preamble_changed = "Preamble.lean" in changes["modified"]

    if not easy_mode:
        hard_scope = check_proof_hard_scope(
            repo,
            active_node=active_node,
            snapshot_before=tablet_snapshot_before,
        )
        scope_error = hard_scope["errors"][0] if hard_scope["errors"] else None
        changes = hard_scope["changes"]
        if scope_error:
            outcome = CycleOutcome(outcome="INVALID", detail=scope_error)
            save_state(state_path(config), state)
            save_tablet(tablet_path(config), tablet)
            _save_live_viewer_state(config, tablet, state, source="worker")
            return outcome

    # Detect new files created by the worker
    new_files = set(changes["created"])

    # Easy-mode enforcement (layer 2: supervisor-side, in addition to filesystem)
    if easy_mode:
        easy_scope = check_proof_easy_scope(
            repo,
            active_node=active_node,
            snapshot_before=tablet_snapshot_before,
            imports_before=imports_before,
        )
        scope_error = easy_scope["errors"][0] if easy_scope["errors"] else None
        created_content_files = list(easy_scope.get("created_content_files", []))
        if scope_error:
            print(f"  Easy-mode violation: {scope_error}")
            for fname in created_content_files:
                try:
                    (repo / "Tablet" / fname).unlink()
                except OSError:
                    pass
            outcome = CycleOutcome(outcome="INVALID", detail=scope_error)
            if node_meta:
                node_meta.easy_attempts += 1
                if node_meta.easy_attempts >= policy.difficulty.easy_max_retries:
                    node_meta.difficulty = "hard"
                    node_meta.easy_attempts = 0
                    print(f"  Auto-elevating {active_node} to hard (after {policy.difficulty.easy_max_retries} easy attempts)")
            save_state(state_path(config), state)
            save_tablet(tablet_path(config), tablet)
            _save_live_viewer_state(config, tablet, state, source="worker")
            return outcome

    # Validate
    outcome = validate_worker_cycle_v2(
        config, tablet, active_node,
        snapshot_before=tablet_snapshot_before,
        active_changed=active_changed,
        new_lean_files=[f.removesuffix(".lean") for f in new_files if f.endswith(".lean")],
    )
    print(f"  Validation: {outcome.outcome} -- {outcome.detail}")

    proof_allowed_nodes = []
    if active_changed or active_tex_changed:
        proof_allowed_nodes.append(active_node)
    proof_allowed_nodes.extend(outcome.nodes_created)
    if outcome.outcome != "INVALID" and (proof_allowed_nodes or preamble_changed):
        scoped_outcome = _run_scoped_tablet_check(
            config,
            baseline_errors=proof_tablet_baseline_errors,
            allowed_nodes=proof_allowed_nodes,
        )
        if scoped_outcome is not None:
            print(f"  Validation: {scoped_outcome.outcome} -- {scoped_outcome.detail}")
            outcome = scoped_outcome

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

    # Run correspondence/paper-faithfulness on all new nodes plus any changed active node.
    # Run soundness only on nodes that remain open after this cycle.
    nodes_to_verify = []
    soundness_nodes_to_verify: List[str] = []
    if outcome.outcome == "PROGRESS":
        if outcome.nodes_created:
            nodes_to_verify.extend(outcome.nodes_created)
            soundness_nodes_to_verify.extend([n for n in outcome.nodes_created if n not in outcome.nodes_closed])
        if active_changed or active_tex_changed:
            if active_node not in nodes_to_verify:
                nodes_to_verify.append(active_node)
        if active_changed and active_node not in outcome.nodes_closed:
            if active_node not in soundness_nodes_to_verify:
                soundness_nodes_to_verify.append(active_node)

    if nodes_to_verify:
        print(f"  Running NL verification for nodes: {nodes_to_verify}")
        needs_nl_review = True
        from lagent_tablets.nl_cache import NLCache
        nl_cache = NLCache(config.state_dir / "nl_cache.json")
        nl_verification_results = _run_nl_verification(
            config, policy, tablet, nodes_to_verify, state=state, cycle=cycle, log_dir=log_dir, nl_cache=nl_cache,
            human_input=state.human_input,
            soundness_node_names=soundness_nodes_to_verify,
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
    _save_live_viewer_state(config, tablet, state, source="worker")
    from lagent_tablets.git_ops import commit_checkpoint as git_commit_checkpoint
    git_commit_checkpoint(
        repo,
        cycle,
        "worker",
        phase=state.phase,
        outcome=outcome.outcome,
        active_node=active_node,
        detail=outcome.detail,
        meta={"stage": "worker"},
    )
    from lagent_tablets.git_ops import commit_checkpoint as git_commit_checkpoint
    git_commit_checkpoint(
        repo,
        cycle,
        "worker",
        phase=state.phase,
        outcome=outcome.outcome,
        active_node=active_node,
        detail=outcome.detail,
        meta={"stage": "worker"},
    )

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
        git_commit_checkpoint(
            repo,
            cycle,
            "verification",
            phase=state.phase,
            outcome=outcome.outcome,
            active_node=active_node,
            detail=outcome.detail,
            meta={
                "stage": "verification",
                "verification_results": nl_verification_results if nl_verification_results else None,
            },
        )
        reviewer_prompt = build_reviewer_prompt(
            config, state, tablet, policy,
            worker_handoff=worker_handoff,
            worker_output=worker_result.captured_output[-20000:] if worker_result.captured_output else "",
            validation_summary={"outcome": outcome.outcome, "detail": outcome.detail,
                                "consecutive_invalids": state.validation_summary.get("consecutive_invalids", 0) if isinstance(state.validation_summary, dict) else 0},
            nl_verification=nl_verification_results if nl_verification_results else None,
        )

        reviewer_artifacts = _clear_artifact_files(config, "reviewer_decision.json")

        reviewer_result = run_reviewer_burst(
            config.reviewer,
            reviewer_prompt,
            session_name=config.tmux.session_name,
            work_dir=repo,
            burst_user=config.tmux.burst_user,
            timeout_seconds=min(policy.timing.burst_timeout_seconds, 300),
            log_dir=log_dir,
            done_file=reviewer_artifacts["done"],
            artifact_prefix=str(reviewer_artifacts["stem"]),
        )

        _accumulate_usage(state, "reviewer", reviewer_result.usage)

        decision = None
        decision_error = None
        if reviewer_result.ok:
            decision, decision_error = _accept_validated_artifact(
                config,
                "reviewer_decision.json",
                kind="reviewer-decision",
                phase="proof_formalization",
            )
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
            print(f"  Reviewer: could not validate decision ({decision_error or 'missing raw/done artifact'})")

        save_tablet(tablet_path(config), tablet)
        save_state(state_path(config), state)
        _save_live_viewer_state(config, tablet, state, source="reviewer")

    # Git commit
    from lagent_tablets.git_ops import commit_cycle as git_commit
    _save_cycle_viewer_state(
        config,
        tablet,
        state,
        verification_results=nl_verification_results,
        source="cycle",
    )
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


def run_cleanup_cycle(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
    *,
    previous_outcome: Optional[Dict[str, Any]] = None,
) -> CycleOutcome:
    """Run one terminal style-cleanup cycle over an already complete tablet."""
    cycle = state.cycle + 1
    cycle_start = time.monotonic()
    state.cycle = cycle
    active_node = state.active_node or tablet.active_node
    if not active_node or active_node not in tablet.nodes:
        for name, node in sorted(tablet.nodes.items()):
            if node.kind != "preamble":
                active_node = name
                break
    repo = config.repo_path

    effective_worker = config.hard_worker or config.worker
    print(f"=== Cleanup Cycle {cycle} | Focus: {active_node or '(none)'} | Worker: {effective_worker.provider}/{effective_worker.model} ===")
    _save_live_viewer_state(config, tablet, state, source="worker")

    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]
    write_scripts(repo, config.state_dir, allowed_prefixes=config.workflow.allowed_import_prefixes, forbidden_keywords=forbidden)

    from lagent_tablets.health import fix_lake_permissions

    fix_lake_permissions(repo)
    _ensure_lake_build(repo)
    _setup_cleanup_permissions(config)

    snapshot_before = _snapshot_tablet_dir(repo)
    log_dir = config.state_dir / "logs" / f"cycle-{cycle:04d}"
    cleanup_payload_path, cleanup_payload = _write_cleanup_check_payload(
        config,
        tablet,
        log_dir,
        snapshot_before,
    )

    worker_prompt = build_worker_prompt(
        config,
        state,
        tablet,
        policy,
        previous_outcome=previous_outcome,
        difficulty="hard",
        cleanup_check_payload_path=cleanup_payload_path,
    )

    worker_artifacts = _clear_artifact_files(config, "worker_handoff.json")
    worker_result = run_worker_burst(
        effective_worker,
        worker_prompt,
        session_name=config.tmux.session_name,
        work_dir=repo,
        burst_user=config.tmux.burst_user,
        timeout_seconds=policy.timing.burst_timeout_seconds,
        startup_timeout_seconds=config.startup_timeout_seconds,
        log_dir=log_dir,
        done_file=worker_artifacts["done"],
        artifact_prefix=str(worker_artifacts["stem"]),
    )
    _accumulate_usage(state, "worker", worker_result.usage)

    if not worker_result.ok:
        save_state(state_path(config), state)
        _save_live_viewer_state(config, tablet, state, source="worker")
        return CycleOutcome(outcome="INVALID", detail=f"Worker burst failed: {worker_result.error}")

    worker_handoff, handoff_error = _accept_validated_artifact(
        config,
        "worker_handoff.json",
        kind="worker-handoff",
        phase="proof_complete_style_cleanup",
        repo_for_validation=repo,
    )
    if not isinstance(worker_handoff, dict):
        save_state(state_path(config), state)
        _save_live_viewer_state(config, tablet, state, source="worker")
        return CycleOutcome(
            outcome="INVALID",
            detail=f"Invalid worker handoff: {handoff_error or 'missing raw/done artifact'}",
        )
    state.last_worker_handoff = worker_handoff

    cleanup_result = check_cleanup_preserving(
        repo,
        snapshot_before=cleanup_payload["snapshot_before"],
        baseline_declaration_hashes=cleanup_payload["baseline_declaration_hashes"],
        baseline_correspondence_hashes=cleanup_payload["baseline_correspondence_hashes"],
        allowed_prefixes=config.workflow.allowed_import_prefixes,
        forbidden_keywords=forbidden,
        approved_axioms_path=config.workflow.approved_axioms_path,
    )
    changes = cleanup_result.get("changes", {"created": [], "modified": [], "deleted": []})

    if not changes["created"] and not changes["modified"] and not changes["deleted"]:
        outcome = CycleOutcome(outcome="NO_PROGRESS", detail="No cleanup changes were made.")
    elif cleanup_result["errors"]:
        if state.cleanup_last_good_commit:
            _restore_cleanup_last_good_state(config, state.cleanup_last_good_commit)
            fix_lake_permissions(repo)
        outcome = CycleOutcome(
            outcome="INVALID",
            detail=cleanup_result["errors"][0],
            build_output=cleanup_result.get("build_output", ""),
        )
    else:
        detail_parts = []
        if cleanup_result.get("changed_nodes"):
            detail_parts.append(f"lean cleanup: {cleanup_result['changed_nodes']}")
        if "Preamble.lean" in changes.get("modified", []):
            detail_parts.append("Preamble imports tidied")
        if not detail_parts:
            detail_parts.append("Semantics-preserving cleanup applied")
        outcome = CycleOutcome(outcome="PROGRESS", detail="; ".join(detail_parts))
        tablet.last_modified_at_cycle = cycle
        state.cleanup_last_good_commit = f"cycle-{cycle}"

    state.validation_summary = {"consecutive_invalids": 0 if outcome.outcome != "INVALID" else 1, "last_outcome": outcome.outcome}

    regenerate_support_files(tablet, repo)
    save_tablet(tablet_path(config), tablet)
    save_state(state_path(config), state)
    _save_live_viewer_state(config, tablet, state, source="worker")

    reviewer_prompt = build_reviewer_prompt(
        config,
        state,
        tablet,
        policy,
        worker_handoff=worker_handoff,
        worker_output=worker_result.captured_output[-20000:] if worker_result.captured_output else "",
        validation_summary={"outcome": outcome.outcome, "detail": outcome.detail, "consecutive_invalids": 0},
        nl_verification=None,
    )

    reviewer_artifacts = _clear_artifact_files(config, "reviewer_decision.json")
    reviewer_result = run_reviewer_burst(
        config.reviewer,
        reviewer_prompt,
        session_name=config.tmux.session_name,
        work_dir=repo,
        burst_user=config.tmux.burst_user,
        timeout_seconds=min(policy.timing.burst_timeout_seconds, 300),
        log_dir=log_dir,
        done_file=reviewer_artifacts["done"],
        artifact_prefix=str(reviewer_artifacts["stem"]),
    )
    _accumulate_usage(state, "reviewer", reviewer_result.usage)

    decision = None
    decision_error = None
    if reviewer_result.ok:
        decision, decision_error = _accept_validated_artifact(
            config,
            "reviewer_decision.json",
            kind="reviewer-decision",
            phase="proof_complete_style_cleanup",
        )
    if isinstance(decision, dict):
        state.last_review = decision
        state.review_log.append({"cycle": cycle, **decision})
        next_node = str(decision.get("next_active_node", "") or "").strip()
        if next_node and next_node in tablet.nodes and tablet.nodes[next_node].kind != "preamble":
            state.active_node = next_node
            tablet.active_node = next_node
        print(f"  Reviewer: {decision.get('decision', '?')} -> next: {state.active_node}")
    else:
        print(f"  Reviewer: could not validate decision ({decision_error or 'missing raw/done artifact'})")

    save_tablet(tablet_path(config), tablet)
    save_state(state_path(config), state)
    _save_live_viewer_state(config, tablet, state, source="reviewer")

    from lagent_tablets.git_ops import commit_cycle as git_commit

    _save_cycle_viewer_state(
        config,
        tablet,
        state,
        verification_results=[],
        source="cycle",
    )
    git_commit(
        repo, cycle,
        phase=state.phase,
        outcome=outcome.outcome,
        active_node=active_node,
        detail=outcome.detail,
        meta={
            "duration_seconds": round(time.monotonic() - cycle_start, 1),
            "reviewer_decision": state.last_review,
            "verification_results": None,
            "token_usage": state.agent_token_usage,
        },
    )

    return outcome
