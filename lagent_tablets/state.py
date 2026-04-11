"""State persistence with atomic writes and file locking.

State is stored as JSON files written atomically (temp + rename).
File-level exclusive locks (fcntl.flock) prevent concurrent writes.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, TypeVar

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

T = TypeVar("T")
OPEN_BLOCKER_PHASES = ("correspondence", "paper_faithfulness", "soundness")
# Backward-compatible alias for older code/tests/state.
OPEN_REJECTION_PHASES = OPEN_BLOCKER_PHASES
ORPHAN_RESOLUTION_ACTIONS = ("remove", "keep_and_add_dependency")
DEFAULT_JSON_FILE_MODE = 0o664


# ---------------------------------------------------------------------------
# Atomic file operations
# ---------------------------------------------------------------------------

def _lock_path(path: Path) -> Path:
    suffix = path.suffix + ".lock" if path.suffix else ".lock"
    return path.with_suffix(suffix)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_json(path: Path, default: Any = None) -> Any:
    """Load a JSON file, returning default if missing."""
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any, *, mode: Optional[int] = None) -> None:
    """Atomically write a JSON file (temp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    target_mode = mode if mode is not None else DEFAULT_JSON_FILE_MODE
    with _exclusive_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            if target_mode is not None:
                os.chmod(tmp_name, target_mode)
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def append_jsonl(path: Path, record: Dict[str, Any], *, mode: Optional[int] = None) -> None:
    """Append a single JSON line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(path):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(path, mode)


def timestamp_now() -> str:
    """ISO 8601 timestamp with timezone."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_open_blockers(raw: Any) -> List[Dict[str, str]]:
    """Normalize stored theorem-stating blocker entries.

    Each entry tracks one currently open theorem-stating blocker that the
    worker must address before the phase can advance.
    """
    if not isinstance(raw, list):
        return []

    normalized: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        node = str(item.get("node", "")).strip() or "(global)"
        phase = str(item.get("phase", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if phase not in OPEN_BLOCKER_PHASES or not reason:
            continue
        key = (node, phase)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "node": node,
            "phase": phase,
            "reason": reason,
        })
    return normalized


def normalize_open_rejections(raw: Any) -> List[Dict[str, str]]:
    """Backward-compatible alias for legacy field naming."""
    return normalize_open_blockers(raw)


def normalize_orphan_resolutions(
    raw: Any,
    *,
    allowed_nodes: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """Normalize structured reviewer decisions for theorem-stating orphan nodes."""
    if not isinstance(raw, list):
        return []

    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        node = str(item.get("node", "")).strip()
        action = str(item.get("action", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not node or node in seen:
            continue
        if allowed_nodes is not None and node not in allowed_nodes:
            continue
        if action not in ORPHAN_RESOLUTION_ACTIONS or not reason:
            continue

        suggested_parents: List[str] = []
        raw_parents = item.get("suggested_parents", [])
        if isinstance(raw_parents, list):
            seen_parents: set[str] = set()
            for parent in raw_parents:
                parent_name = str(parent).strip()
                if not parent_name or parent_name in seen_parents or parent_name == node:
                    continue
                seen_parents.add(parent_name)
                suggested_parents.append(parent_name)

        seen.add(node)
        normalized.append({
            "node": node,
            "action": action,
            "reason": reason,
            "suggested_parents": suggested_parents,
        })
    return normalized


def normalize_paper_focus_ranges(raw: Any) -> List[Dict[str, Any]]:
    """Normalize reviewer-selected paper line ranges for worker prompt excerpts."""
    if not isinstance(raw, list):
        return []

    normalized: List[Dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start_line = int(item.get("start_line", 0))
            end_line = int(item.get("end_line", 0))
        except (TypeError, ValueError):
            continue
        if start_line <= 0 or end_line <= 0:
            continue
        if end_line < start_line:
            start_line, end_line = end_line, start_line
        key = (start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "start_line": start_line,
            "end_line": end_line,
            "reason": str(item.get("reason", "")).strip(),
        })
    return normalized


# ---------------------------------------------------------------------------
# Tablet node dataclass
# ---------------------------------------------------------------------------

@dataclass
class TabletNode:
    """A single node in the proof tablet."""
    name: str
    kind: str  # "preamble", "paper_main_result", "paper_intermediate", "helper_lemma"
    status: str  # "open", "closed"
    difficulty: str = "hard"  # "easy" or "hard"
    title: str = ""
    paper_provenance: str = ""
    lean_statement_hash: str = ""
    closed_content_hash: str = ""
    closed_at_cycle: Optional[int] = None
    invalidated_at_cycle: Optional[int] = None
    easy_attempts: int = 0
    correspondence_status: str = "?"  # "?", "pass", "fail"
    soundness_status: str = "?"       # "?", "pass", "fail", "structural"
    verification_at_cycle: Optional[int] = None  # when status was last set
    correspondence_text_hash: str = ""  # conservative text-level hash for fast correspondence invalidation
    correspondence_content_hash: str = ""  # statement-level hash when correspondence was set
    soundness_content_hash: str = ""       # full NL-proof hash when soundness was set
    verification_content_hash: str = ""    # legacy combined hash for backward compatibility
    coarse: bool = False
    coarse_content_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "kind": self.kind,
            "status": self.status,
            "difficulty": self.difficulty,
            "title": self.title,
        }
        if self.paper_provenance:
            d["paper_provenance"] = self.paper_provenance
        if self.lean_statement_hash:
            d["lean_statement_hash"] = self.lean_statement_hash
        if self.closed_content_hash:
            d["closed_content_hash"] = self.closed_content_hash
        if self.closed_at_cycle is not None:
            d["closed_at_cycle"] = self.closed_at_cycle
        if self.invalidated_at_cycle is not None:
            d["invalidated_at_cycle"] = self.invalidated_at_cycle
        if self.easy_attempts > 0:
            d["easy_attempts"] = self.easy_attempts
        if self.correspondence_status != "?":
            d["correspondence_status"] = self.correspondence_status
        if self.soundness_status != "?":
            d["soundness_status"] = self.soundness_status
        if self.verification_at_cycle is not None:
            d["verification_at_cycle"] = self.verification_at_cycle
        if self.correspondence_text_hash:
            d["correspondence_text_hash"] = self.correspondence_text_hash
        if self.correspondence_content_hash:
            d["correspondence_content_hash"] = self.correspondence_content_hash
        if self.soundness_content_hash:
            d["soundness_content_hash"] = self.soundness_content_hash
        if self.verification_content_hash:
            d["verification_content_hash"] = self.verification_content_hash
        elif self.soundness_content_hash:
            d["verification_content_hash"] = self.soundness_content_hash
        elif self.correspondence_content_hash:
            d["verification_content_hash"] = self.correspondence_content_hash
        if self.coarse:
            d["coarse"] = True
        if self.coarse_content_hash:
            d["coarse_content_hash"] = self.coarse_content_hash
        return d

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any]) -> TabletNode:
        diff = str(raw.get("difficulty", "hard"))
        if diff not in ("easy", "hard"):
            diff = "hard"
        legacy_hash = str(raw.get("verification_content_hash", ""))
        correspondence_text_hash = str(raw.get("correspondence_text_hash", ""))
        correspondence_hash = str(raw.get("correspondence_content_hash", legacy_hash))
        soundness_hash = str(raw.get("soundness_content_hash", legacy_hash))
        return cls(
            name=name,
            kind=str(raw.get("kind", "helper_lemma")),
            status=str(raw.get("status", "open")),
            difficulty=diff,
            title=str(raw.get("title", "")),
            paper_provenance=str(raw.get("paper_provenance", "")),
            lean_statement_hash=str(raw.get("lean_statement_hash", "")),
            closed_content_hash=str(raw.get("closed_content_hash", "")),
            closed_at_cycle=raw.get("closed_at_cycle"),
            invalidated_at_cycle=raw.get("invalidated_at_cycle"),
            easy_attempts=int(raw.get("easy_attempts", 0)),
            correspondence_status=str(raw.get("correspondence_status", "?")),
            soundness_status=str(raw.get("soundness_status", "?")),
            verification_at_cycle=raw.get("verification_at_cycle"),
            correspondence_text_hash=correspondence_text_hash,
            correspondence_content_hash=correspondence_hash,
            soundness_content_hash=soundness_hash,
            verification_content_hash=legacy_hash or soundness_hash or correspondence_hash,
            coarse=bool(raw.get("coarse", False)),
            coarse_content_hash=str(raw.get("coarse_content_hash", "")),
        )


# ---------------------------------------------------------------------------
# Tablet state
# ---------------------------------------------------------------------------

@dataclass
class TabletState:
    """The proof tablet: a DAG of nodes tracked in tablet.json."""
    nodes: Dict[str, TabletNode] = field(default_factory=dict)
    active_node: str = ""
    seeded_at_cycle: Optional[int] = None
    last_modified_at_cycle: Optional[int] = None

    @property
    def total_nodes(self) -> int:
        return sum(1 for n in self.nodes.values() if n.kind != "preamble")

    @property
    def closed_nodes(self) -> int:
        return sum(1 for n in self.nodes.values() if n.kind != "preamble" and n.status == "closed")

    @property
    def open_nodes(self) -> int:
        return sum(1 for n in self.nodes.values() if n.kind != "preamble" and n.status == "open")

    @property
    def easy_open_nodes(self) -> int:
        return sum(1 for n in self.nodes.values() if n.kind != "preamble" and n.status == "open" and n.difficulty == "easy")

    @property
    def hard_open_nodes(self) -> int:
        return sum(1 for n in self.nodes.values() if n.kind != "preamble" and n.status == "open" and n.difficulty == "hard")

    def metrics(self) -> Dict[str, Any]:
        return {
            "total_nodes": self.total_nodes,
            "closed_nodes": self.closed_nodes,
            "open_nodes": self.open_nodes,
            "easy_open": self.easy_open_nodes,
            "hard_open": self.hard_open_nodes,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": {name: node.to_dict() for name, node in self.nodes.items()},
            "active_node": self.active_node,
            "seeded_at_cycle": self.seeded_at_cycle,
            "last_modified_at_cycle": self.last_modified_at_cycle,
            "metrics": self.metrics(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> TabletState:
        if not isinstance(raw, dict):
            return cls()
        nodes: Dict[str, TabletNode] = {}
        raw_nodes = raw.get("nodes", {})
        if isinstance(raw_nodes, dict):
            for name, node_raw in raw_nodes.items():
                if isinstance(node_raw, dict):
                    nodes[name] = TabletNode.from_dict(name, node_raw)
        return cls(
            nodes=nodes,
            active_node=str(raw.get("active_node", "")),
            seeded_at_cycle=raw.get("seeded_at_cycle"),
            last_modified_at_cycle=raw.get("last_modified_at_cycle"),
        )


def load_tablet(path: Path) -> TabletState:
    raw = load_json(path)
    if raw is None:
        return TabletState()
    return TabletState.from_dict(raw)


def save_tablet(path: Path, tablet: TabletState, *, mode: Optional[int] = None) -> None:
    save_json(path, tablet.to_dict(), mode=mode)


# ---------------------------------------------------------------------------
# Supervisor state
# ---------------------------------------------------------------------------

@dataclass
class SupervisorState:
    """The supervisor's runtime state (state.json)."""
    cycle: int = 0
    phase: str = "paper_check"
    active_node: str = ""
    proof_target_edit_mode: str = "local"
    theorem_soundness_target: str = ""
    theorem_target_edit_mode: str = "repair"
    theorem_correspondence_blocked: bool = False
    last_worker_handoff: Optional[Dict[str, Any]] = None
    last_review: Optional[Dict[str, Any]] = None
    open_blockers: List[Dict[str, str]] = field(default_factory=list)
    review_log: List[Dict[str, Any]] = field(default_factory=list)
    validation_summary: Optional[Dict[str, Any]] = None
    stuck_recovery_attempts: List[Dict[str, Any]] = field(default_factory=list)
    human_input: str = ""
    human_input_at_cycle: int = 0
    awaiting_human_input: bool = False
    cleanup_last_good_commit: str = ""
    trusted_main_result_hashes: Dict[str, str] = field(default_factory=dict)
    agent_token_usage: Dict[str, Any] = field(default_factory=dict)
    resume_from: str = ""  # mid-cycle checkpoint: "", "verification", "reviewer"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle": self.cycle,
            "phase": self.phase,
            "active_node": self.active_node,
            "proof_target_edit_mode": self.proof_target_edit_mode,
            "theorem_soundness_target": self.theorem_soundness_target,
            "theorem_target_edit_mode": self.theorem_target_edit_mode,
            "theorem_correspondence_blocked": self.theorem_correspondence_blocked,
            "last_worker_handoff": self.last_worker_handoff,
            "last_review": self.last_review,
            "open_blockers": self.open_blockers,
            "open_rejections": self.open_blockers,
            "review_log": self.review_log,
            "validation_summary": self.validation_summary,
            "stuck_recovery_attempts": self.stuck_recovery_attempts,
            "human_input": self.human_input,
            "human_input_at_cycle": self.human_input_at_cycle,
            "awaiting_human_input": self.awaiting_human_input,
            "cleanup_last_good_commit": self.cleanup_last_good_commit,
            "trusted_main_result_hashes": self.trusted_main_result_hashes,
            "agent_token_usage": self.agent_token_usage,
            "resume_from": self.resume_from,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SupervisorState:
        if not isinstance(raw, dict):
            return cls()
        theorem_target_edit_mode = str(raw.get("theorem_target_edit_mode", "repair") or "repair")
        if theorem_target_edit_mode not in {"repair", "restructure"}:
            theorem_target_edit_mode = "repair"
        proof_target_edit_mode = str(raw.get("proof_target_edit_mode", "local") or "local")
        if proof_target_edit_mode not in {"local", "restructure", "coarse_restructure"}:
            proof_target_edit_mode = "local"
        return cls(
            cycle=int(raw.get("cycle", 0)),
            phase=str(raw.get("phase", "paper_check")),
            active_node=str(raw.get("active_node", "")),
            proof_target_edit_mode=proof_target_edit_mode,
            theorem_soundness_target=str(raw.get("theorem_soundness_target", "")),
            theorem_target_edit_mode=theorem_target_edit_mode,
            theorem_correspondence_blocked=bool(raw.get("theorem_correspondence_blocked", False)),
            last_worker_handoff=raw.get("last_worker_handoff"),
            last_review=raw.get("last_review"),
            open_blockers=normalize_open_blockers(
                raw.get("open_blockers", raw.get("open_rejections", []))
            ),
            review_log=list(raw.get("review_log", [])),
            validation_summary=raw.get("validation_summary"),
            stuck_recovery_attempts=list(raw.get("stuck_recovery_attempts", [])),
            human_input=str(raw.get("human_input", "")),
            human_input_at_cycle=int(raw.get("human_input_at_cycle", 0)),
            awaiting_human_input=bool(raw.get("awaiting_human_input", False)),
            cleanup_last_good_commit=str(raw.get("cleanup_last_good_commit", "")),
            trusted_main_result_hashes=dict(raw.get("trusted_main_result_hashes", {})),
            agent_token_usage=dict(raw.get("agent_token_usage", {})),
            resume_from=str(raw.get("resume_from", "")),
        )

    @property
    def open_rejections(self) -> List[Dict[str, str]]:
        """Backward-compatible alias for legacy callers."""
        return self.open_blockers

    @open_rejections.setter
    def open_rejections(self, value: List[Dict[str, str]]) -> None:
        self.open_blockers = normalize_open_blockers(value)


def state_path(config_or_state_dir: Any) -> Path:
    """Return the path to state.json."""
    if isinstance(config_or_state_dir, Path):
        return config_or_state_dir / "state.json"
    return Path(config_or_state_dir.state_dir) / "state.json"


def tablet_path(config_or_state_dir: Any) -> Path:
    """Return the path to tablet.json."""
    if isinstance(config_or_state_dir, Path):
        return config_or_state_dir / "tablet.json"
    return Path(config_or_state_dir.state_dir) / "tablet.json"


def load_state(path: Path) -> SupervisorState:
    raw = load_json(path)
    if raw is None:
        return SupervisorState()
    return SupervisorState.from_dict(raw)


def save_state(path: Path, state: SupervisorState, *, mode: Optional[int] = None) -> None:
    save_json(path, state.to_dict(), mode=mode)
