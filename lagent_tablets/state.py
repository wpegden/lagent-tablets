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
            if mode is not None:
                os.chmod(tmp_name, mode)
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
    closed_at_cycle: Optional[int] = None
    invalidated_at_cycle: Optional[int] = None
    easy_attempts: int = 0
    correspondence_status: str = "?"  # "?", "pass", "fail"
    soundness_status: str = "?"       # "?", "pass", "fail", "structural"
    verification_at_cycle: Optional[int] = None  # when status was last set
    verification_content_hash: str = ""  # hash of .lean+.tex when status was set

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
        if self.verification_content_hash:
            d["verification_content_hash"] = self.verification_content_hash
        return d

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any]) -> TabletNode:
        diff = str(raw.get("difficulty", "hard"))
        if diff not in ("easy", "hard"):
            diff = "hard"
        return cls(
            name=name,
            kind=str(raw.get("kind", "helper_lemma")),
            status=str(raw.get("status", "open")),
            difficulty=diff,
            title=str(raw.get("title", "")),
            paper_provenance=str(raw.get("paper_provenance", "")),
            lean_statement_hash=str(raw.get("lean_statement_hash", "")),
            closed_at_cycle=raw.get("closed_at_cycle"),
            invalidated_at_cycle=raw.get("invalidated_at_cycle"),
            easy_attempts=int(raw.get("easy_attempts", 0)),
            correspondence_status=str(raw.get("correspondence_status", "?")),
            soundness_status=str(raw.get("soundness_status", "?")),
            verification_at_cycle=raw.get("verification_at_cycle"),
            verification_content_hash=str(raw.get("verification_content_hash", "")),
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
    last_worker_handoff: Optional[Dict[str, Any]] = None
    last_review: Optional[Dict[str, Any]] = None
    review_log: List[Dict[str, Any]] = field(default_factory=list)
    validation_summary: Optional[Dict[str, Any]] = None
    stuck_recovery_attempts: List[Dict[str, Any]] = field(default_factory=list)
    human_input: str = ""
    human_input_at_cycle: int = 0
    awaiting_human_input: bool = False
    cleanup_last_good_commit: str = ""
    agent_token_usage: Dict[str, Any] = field(default_factory=dict)
    resume_from: str = ""  # mid-cycle checkpoint: "", "verification", "reviewer"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle": self.cycle,
            "phase": self.phase,
            "active_node": self.active_node,
            "last_worker_handoff": self.last_worker_handoff,
            "last_review": self.last_review,
            "review_log": self.review_log,
            "validation_summary": self.validation_summary,
            "stuck_recovery_attempts": self.stuck_recovery_attempts,
            "human_input": self.human_input,
            "human_input_at_cycle": self.human_input_at_cycle,
            "awaiting_human_input": self.awaiting_human_input,
            "cleanup_last_good_commit": self.cleanup_last_good_commit,
            "agent_token_usage": self.agent_token_usage,
            "resume_from": self.resume_from,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SupervisorState:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            cycle=int(raw.get("cycle", 0)),
            phase=str(raw.get("phase", "paper_check")),
            active_node=str(raw.get("active_node", "")),
            last_worker_handoff=raw.get("last_worker_handoff"),
            last_review=raw.get("last_review"),
            review_log=list(raw.get("review_log", [])),
            validation_summary=raw.get("validation_summary"),
            stuck_recovery_attempts=list(raw.get("stuck_recovery_attempts", [])),
            human_input=str(raw.get("human_input", "")),
            human_input_at_cycle=int(raw.get("human_input_at_cycle", 0)),
            awaiting_human_input=bool(raw.get("awaiting_human_input", False)),
            cleanup_last_good_commit=str(raw.get("cleanup_last_good_commit", "")),
            agent_token_usage=dict(raw.get("agent_token_usage", {})),
            resume_from=str(raw.get("resume_from", "")),
        )


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
