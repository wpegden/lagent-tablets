"""Cycle history: diffs and metadata for the web viewer.

Stores:
  history/cycles.jsonl    — one JSON line per cycle (metadata, decisions, outcomes)
  history/diffs/cycle-NNNN.patch  — unified diff of all file changes
  history/baseline/       — full snapshot at cycle 0 (starting point for replay)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


def _snapshot_tablet(repo: Path) -> Dict[str, str]:
    """Read all tablet files into a dict {relative_path: content}."""
    files = {}
    tablet_dir = repo / "Tablet"
    if tablet_dir.is_dir():
        for p in sorted(tablet_dir.iterdir()):
            if p.is_file():
                try:
                    files[f"Tablet/{p.name}"] = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
    # Also capture Tablet.lean root file
    root = repo / "Tablet.lean"
    if root.is_file():
        try:
            files["Tablet.lean"] = root.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return files


def _make_unified_diff(old: Dict[str, str], new: Dict[str, str]) -> str:
    """Generate a unified diff between two file snapshots."""
    import difflib
    all_files = sorted(set(old.keys()) | set(new.keys()))
    patches = []
    for path in all_files:
        old_content = old.get(path, "")
        new_content = new.get(path, "")
        if old_content == new_content:
            continue
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}",
        )
        patches.append("".join(diff))
    return "\n".join(patches)


def save_baseline(repo: Path, history_dir: Path) -> None:
    """Save the full tablet state as the baseline for diff replay."""
    baseline = history_dir / "baseline"
    baseline.mkdir(parents=True, exist_ok=True)
    tablet_dir = repo / "Tablet"
    baseline_tablet = baseline / "Tablet"
    if baseline_tablet.exists():
        shutil.rmtree(baseline_tablet)
    if tablet_dir.is_dir():
        shutil.copytree(tablet_dir, baseline_tablet)
    # Copy state files
    for name in ("tablet.json", "state.json"):
        src = repo / ".agent-supervisor" / name
        if src.exists():
            shutil.copy2(src, baseline / name)


def record_cycle(
    repo: Path,
    history_dir: Path,
    *,
    cycle: int,
    phase: str,
    snapshot_before: Dict[str, str],
    outcome: Dict[str, Any],
    reviewer_decision: Optional[Dict[str, Any]] = None,
    verification_results: Optional[List[Dict[str, Any]]] = None,
    worker_handoff: Optional[Dict[str, Any]] = None,
    duration_seconds: float = 0,
) -> None:
    """Record a cycle's metadata and diffs."""
    history_dir.mkdir(parents=True, exist_ok=True)
    diffs_dir = history_dir / "diffs"
    diffs_dir.mkdir(exist_ok=True)

    # Take snapshot after
    snapshot_after = _snapshot_tablet(repo)

    # Generate and save diff
    diff = _make_unified_diff(snapshot_before, snapshot_after)
    diff_path = diffs_dir / f"cycle-{cycle:04d}.patch"
    diff_path.write_text(diff, encoding="utf-8")

    # List changed files
    changed = [f for f in sorted(set(snapshot_before.keys()) | set(snapshot_after.keys()))
               if snapshot_before.get(f, "") != snapshot_after.get(f, "")]

    # Read current tablet.json for node summary
    tablet_json = {}
    tablet_path = repo / ".agent-supervisor" / "tablet.json"
    if tablet_path.exists():
        try:
            tablet_json = json.loads(tablet_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Build cycle entry
    entry = {
        "cycle": cycle,
        "phase": phase,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": round(duration_seconds, 1),
        "outcome": outcome,
        "changed_files": changed,
        "diff_path": str(diff_path.relative_to(history_dir)),
        "worker_handoff": worker_handoff,
        "reviewer_decision": reviewer_decision,
        "verification_results": verification_results,
        "tablet_summary": {
            "total_nodes": len([n for n, v in tablet_json.get("nodes", {}).items()
                               if v.get("kind") != "preamble"]),
            "nodes": {name: {"kind": v.get("kind"), "status": v.get("status")}
                     for name, v in tablet_json.get("nodes", {}).items()
                     if v.get("kind") != "preamble"},
        },
    }

    # Append to cycles.jsonl
    cycles_path = history_dir / "cycles.jsonl"
    with open(cycles_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
