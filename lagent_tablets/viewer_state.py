"""Canonical viewer snapshot generation.

This module builds the JSON payload consumed by the DAG viewer.

Future cycles commit `.agent-supervisor/viewer_state.json` alongside
`state.json`/`tablet.json`. Historical views then come from git directly.

Legacy cycles can be backfilled once into an external cache using the same
snapshot builder, with era-specific handling for older metadata shapes.
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Set

from lagent_tablets.nl_cache import correspondence_fingerprint, prime_correspondence_fingerprints, soundness_fingerprint
from lagent_tablets.state import (
    SupervisorState,
    TabletNode,
    TabletState,
    load_json,
    save_json,
)
from lagent_tablets.tablet import PREAMBLE_NAME, extract_tablet_imports, has_sorry


_TEX_ENV_RE = re.compile(
    r"\\begin\{(theorem|lemma|definition|proposition|corollary)\}(?:\[(.*?)\])?"
)


class Snapshot(Protocol):
    def read_text(self, rel_path: str) -> str:
        ...


class FsSnapshot:
    """Filesystem-backed snapshot reader."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self._cache: Dict[str, str] = {}

    def read_text(self, rel_path: str) -> str:
        if rel_path in self._cache:
            return self._cache[rel_path]
        path = self.repo_path / rel_path
        try:
            value = path.read_text(encoding="utf-8")
        except Exception:
            value = ""
        self._cache[rel_path] = value
        return value


class GitSnapshot:
    """Git-backed snapshot reader."""

    def __init__(self, repo_path: Path, ref: str) -> None:
        self.repo_path = repo_path
        self.ref = ref
        self._cache: Dict[str, str] = {}

    def read_text(self, rel_path: str) -> str:
        if rel_path in self._cache:
            return self._cache[rel_path]
        try:
            value = subprocess.check_output(
                ["git", "-C", str(self.repo_path), "show", f"{self.ref}:{rel_path}"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            value = ""
        self._cache[rel_path] = value
        return value


def viewer_state_path(state_dir: Path) -> Path:
    return state_dir / "viewer_state.json"


def _static_out_dir() -> Path:
    return Path(os.environ.get("LAGENT_VIEWER_STATIC_OUT", "/home/leanagent/lagent-tablets-web"))


def viewer_project_slug(repo_path: Path) -> str:
    slug = re.sub(r"_tablets?$", "", repo_path.name)
    return slug or repo_path.name


def _write_json_world_readable(path: Path, payload: Any) -> None:
    save_json(path, payload)
    os.chmod(path, 0o644)


def repo_cache_slug(repo_path: Path) -> str:
    digest = hashlib.sha1(str(repo_path).encode("utf-8")).hexdigest()[:10]
    return f"{repo_path.name}-{digest}"


def backfill_cache_dir(static_out: Path, repo_path: Path) -> Path:
    return static_out / "api" / "backfill" / repo_cache_slug(repo_path) / "state-at"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _extract_declaration_preview(lean_content: str) -> str:
    lines = lean_content.splitlines()
    result: List[str] = []
    in_decl = False
    for line in lines:
        stripped = line.strip()
        if not in_decl and re.match(r"^(theorem|lemma|def|noncomputable\s+def)\s", stripped):
            in_decl = True
        if in_decl:
            result.append(line)
            if ":= sorry" in line or ":= by" in line or stripped.endswith(":="):
                break
    return "\n".join(result)


def extract_tex_statement(tex_content: str) -> str:
    proof_start = tex_content.find("\\begin{proof}")
    if proof_start >= 0:
        return tex_content[:proof_start].strip()
    return tex_content.strip()


def _recursive_imports(name: str, snapshot: Snapshot, visited: Optional[Set[str]] = None) -> Set[str]:
    if not name or name == PREAMBLE_NAME:
        return set()
    acc = set() if visited is None else visited
    if name in acc:
        return acc
    acc.add(name)
    lean_content = snapshot.read_text(f"Tablet/{name}.lean")
    for dep in extract_tablet_imports(lean_content):
        if dep == PREAMBLE_NAME:
            continue
        _recursive_imports(dep, snapshot, acc)
    return acc


def _direct_imports(name: str, snapshot: Snapshot) -> List[str]:
    if not name or name == PREAMBLE_NAME:
        return []
    lean_content = snapshot.read_text(f"Tablet/{name}.lean")
    return sorted(extract_tablet_imports(lean_content))


def _build_node_payload(name: str, meta: TabletNode, snapshot: Snapshot) -> Dict[str, Any]:
    lean_content = snapshot.read_text(f"Tablet/{name}.lean")
    tex_content = snapshot.read_text(f"Tablet/{name}.tex")
    imports = _direct_imports(name, snapshot)
    title = ""
    tex_env = ""
    env_match = _TEX_ENV_RE.search(tex_content)
    if env_match:
        tex_env = env_match.group(1) or ""
        title = env_match.group(2) or ""
    return {
        **meta.to_dict(),
        "title": title or meta.title,
        "texEnv": tex_env,
        "imports": imports,
        "declaration": _extract_declaration_preview(lean_content),
        "hasSorry": has_sorry(lean_content) if lean_content else False,
        "leanContent": lean_content,
        "texContent": tex_content,
    }


def _build_nodes(tablet: TabletState, snapshot: Snapshot) -> Dict[str, Dict[str, Any]]:
    nodes: Dict[str, Dict[str, Any]] = {}
    preamble_content = snapshot.read_text("Tablet/Preamble.lean")
    if preamble_content:
        defs = [
            line.strip()
            for line in preamble_content.splitlines()
            if re.match(r"^(noncomputable\s+)?def\s", line.strip())
        ]
        nodes[PREAMBLE_NAME] = {
            "kind": "preamble",
            "status": "closed",
            "title": "Preamble",
            "imports": [],
            "declaration": "\n".join(defs),
            "hasSorry": has_sorry(preamble_content),
            "leanContent": preamble_content,
            "texContent": "",
            "verification": {"correspondence": "pass", "nl_proof": "pass"},
            "activity": {
                "worker": False,
                "correspondence": False,
                "soundness": False,
                "reviewer": False,
            },
        }

    for name, meta in tablet.nodes.items():
        if name == PREAMBLE_NAME or meta.kind == "preamble":
            continue
        nodes[name] = _build_node_payload(name, meta, snapshot)
    return nodes


def _live_verification_statuses(repo_path: Path, tablet: TabletState) -> Dict[str, Dict[str, str]]:
    statuses: Dict[str, Dict[str, str]] = {}
    prime_correspondence_fingerprints(
        repo_path,
        [
            name
            for name, node in tablet.nodes.items()
            if name != PREAMBLE_NAME and node.kind != "preamble"
        ],
    )
    for name, node in tablet.nodes.items():
        if name == PREAMBLE_NAME or node.kind == "preamble":
            continue
        corr = node.correspondence_status or "?"
        sound = node.soundness_status or "?"
        saved_corr_hash = node.correspondence_content_hash or node.verification_content_hash
        saved_sound_hash = node.soundness_content_hash or node.verification_content_hash
        current_corr_hash = correspondence_fingerprint(repo_path, name) or ""
        current_sound_hash = soundness_fingerprint(repo_path, name) or ""
        if saved_corr_hash and current_corr_hash and saved_corr_hash != current_corr_hash:
            corr = "?"
        if saved_sound_hash and current_sound_hash and saved_sound_hash != current_sound_hash:
            sound = "?"
        if node.status == "closed":
            sound = "pass"
        statuses[name] = {"correspondence": corr, "nl_proof": sound}
    return statuses


def _stored_verification_statuses(tablet: TabletState) -> Dict[str, Dict[str, str]]:
    """Cheap status snapshot that trusts persisted tablet state.

    This is used only for startup/live bootstrap so the viewer can refresh
    immediately without blocking on semantic fingerprinting.
    """
    statuses: Dict[str, Dict[str, str]] = {}
    for name, node in tablet.nodes.items():
        if name == PREAMBLE_NAME or node.kind == "preamble":
            continue
        corr = node.correspondence_status or "?"
        sound = node.soundness_status or "?"
        if node.status == "closed":
            sound = "pass"
        statuses[name] = {"correspondence": corr, "nl_proof": sound}
    return statuses


def _aggregate_correspondence_overall(verification_results: List[Dict[str, Any]]) -> Optional[str]:
    corr_results = [r for r in verification_results if r.get("check") == "correspondence"]
    if not corr_results:
        return None
    overalls = [str(r.get("overall", "")).upper() for r in corr_results]
    if overalls and all(o == "APPROVE" for o in overalls):
        return "APPROVE"
    if overalls and all(o == "REJECT" for o in overalls):
        return "REJECT"
    return "DISAGREE"


def _legacy_correspondence_activity(
    tablet: TabletState,
    state: SupervisorState,
    verification_results: List[Dict[str, Any]],
) -> Set[str]:
    checked: Set[str] = set()
    saw_legacy_missing = False
    for result in verification_results:
        if result.get("check") != "correspondence":
            continue
        if "node_names" not in result:
            saw_legacy_missing = True
            continue
        node_names = result.get("node_names")
        if isinstance(node_names, list):
            if node_names:
                checked.update(str(name) for name in node_names if isinstance(name, str))
            continue
        if node_names is None:
            saw_legacy_missing = True
    if checked:
        return checked
    has_correspondence = any(r.get("check") == "correspondence" for r in verification_results)
    if not has_correspondence:
        return set()
    if saw_legacy_missing and state.phase == "theorem_stating":
        return {
            name for name, node in tablet.nodes.items()
            if name != PREAMBLE_NAME and node.kind != "preamble"
        }
    if saw_legacy_missing and state.active_node:
        return {state.active_node}
    return set()


def _node_verdict_status(verdict: Dict[str, Any]) -> str:
    if verdict.get("structural"):
        return "structural"
    if str(verdict.get("overall", "")).upper() == "APPROVE":
        return "pass"
    return "fail"


def _soundness_status_from_snapshot(snapshot: Snapshot, node_name: str) -> Optional[str]:
    results: List[Dict[str, Any]] = []
    for index in range(10):
        raw = snapshot.read_text(f"nl_proof_{node_name}_{index}.json")
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            results.append(data)
    if not results:
        return None
    overalls = [str(r.get("overall", "")).upper() for r in results]
    has_structural = any(
        str(((r.get("soundness") or {}).get("decision", ""))).upper() == "STRUCTURAL"
        for r in results
        if isinstance(r, dict)
    )
    if overalls and all(o == "APPROVE" for o in overalls):
        return "pass"
    if has_structural:
        return "structural"
    return "fail"


def _historical_verification_statuses(
    snapshot: Snapshot,
    tablet: TabletState,
    state: SupervisorState,
    verification_results: List[Dict[str, Any]],
) -> Dict[str, Dict[str, str]]:
    statuses: Dict[str, Dict[str, str]] = {}
    for name, node in tablet.nodes.items():
        if name == PREAMBLE_NAME or node.kind == "preamble":
            continue
        sound = "pass" if node.status == "closed" else (node.soundness_status or "?")
        statuses[name] = {
            "correspondence": node.correspondence_status or "?",
            "nl_proof": sound,
        }

    corr_overall = _aggregate_correspondence_overall(verification_results)
    corr_checked = _legacy_correspondence_activity(tablet, state, verification_results)
    if corr_overall == "APPROVE":
        for name in corr_checked:
            if name in statuses:
                statuses[name]["correspondence"] = "pass"

    for result in verification_results:
        if result.get("check") != "nl_proof":
            continue
        node_verdicts = result.get("node_verdicts")
        if isinstance(node_verdicts, list) and node_verdicts:
            for verdict in node_verdicts:
                node_name = str(verdict.get("node", "")).strip()
                if node_name in statuses:
                    statuses[node_name]["nl_proof"] = _node_verdict_status(verdict)
            continue
        node_names = result.get("node_names")
        if not isinstance(node_names, list):
            node_names = []
        for node_name in node_names:
            if node_name not in statuses:
                continue
            computed = _soundness_status_from_snapshot(snapshot, node_name)
            if computed:
                statuses[node_name]["nl_proof"] = computed
                continue
            if str(result.get("overall", "")).upper() == "APPROVE":
                statuses[node_name]["nl_proof"] = "pass"
            elif str(result.get("overall", "")).upper() == "REJECT":
                statuses[node_name]["nl_proof"] = "fail"
    return statuses


def _normalize_activity(nodes: Iterable[str], activity: Optional[Dict[str, Iterable[str]]] = None) -> Dict[str, Dict[str, bool]]:
    normalized = {
        name: {
            "worker": False,
            "correspondence": False,
            "soundness": False,
            "reviewer": False,
        }
        for name in nodes
        if name != PREAMBLE_NAME
    }
    if not activity:
        return normalized
    for field in ("worker", "correspondence", "soundness", "reviewer"):
        for name in activity.get(field, []) or []:
            if name in normalized:
                normalized[name][field] = True
    return normalized


def _default_live_activity(repo_path: Path, tablet: TabletState, state: SupervisorState) -> Dict[str, Iterable[str]]:
    activity: Dict[str, Iterable[str]] = {}
    if state.phase == "theorem_stating" and state.resume_from == "verification":
        prime_correspondence_fingerprints(
            repo_path,
            [
                name
                for name, node in tablet.nodes.items()
                if name != PREAMBLE_NAME and node.kind != "preamble"
            ],
        )
        corr_frontier: List[str] = []
        for name, node in tablet.nodes.items():
            if name == PREAMBLE_NAME or node.kind == "preamble":
                continue
            current_corr_hash = correspondence_fingerprint(repo_path, name) or ""
            saved_corr_hash = node.correspondence_content_hash or node.verification_content_hash
            if not current_corr_hash or node.correspondence_status == "?" or not saved_corr_hash:
                corr_frontier.append(name)
                continue
            if current_corr_hash != saved_corr_hash:
                corr_frontier.append(name)
        if corr_frontier:
            activity["correspondence"] = sorted(corr_frontier)
        elif state.theorem_soundness_target:
            activity["soundness"] = [state.theorem_soundness_target]
    elif state.phase == "proof_formalization" and state.resume_from == "verification":
        if state.active_node:
            activity["correspondence"] = [state.active_node]
    elif state.resume_from == "reviewer":
        if state.phase == "theorem_stating" and state.theorem_soundness_target:
            activity["reviewer"] = [state.theorem_soundness_target]
        elif state.active_node:
            activity["reviewer"] = [state.active_node]
    return activity


def _historical_activity(
    tablet: TabletState,
    state: SupervisorState,
    verification_results: List[Dict[str, Any]],
) -> Dict[str, Iterable[str]]:
    corr = _legacy_correspondence_activity(tablet, state, verification_results)
    sound: Set[str] = set()
    for result in verification_results:
        if result.get("check") != "nl_proof":
            continue
        node_names = result.get("node_names")
        if isinstance(node_names, list) and node_names:
            sound.update(str(name) for name in node_names if isinstance(name, str))
    if not sound:
        target = state.theorem_soundness_target or state.active_node
        if target:
            sound.add(target)
    return {
        "correspondence": sorted(corr),
        "soundness": sorted(sound),
    }


def build_live_viewer_state(
    repo_path: Path,
    tablet: TabletState,
    state: SupervisorState,
    *,
    activity: Optional[Dict[str, Iterable[str]]] = None,
    in_flight_cycle: Optional[int] = None,
    source: str = "live",
    fast: bool = False,
) -> Dict[str, Any]:
    snapshot = FsSnapshot(repo_path)
    nodes = _build_nodes(tablet, snapshot)
    verification = (
        _stored_verification_statuses(tablet)
        if fast
        else _live_verification_statuses(repo_path, tablet)
    )
    normalized_activity = _normalize_activity(
        nodes.keys(),
        activity or _default_live_activity(repo_path, tablet, state),
    )
    for name, payload in nodes.items():
        if name == PREAMBLE_NAME:
            continue
        payload["verification"] = verification.get(
            name, {"correspondence": "?", "nl_proof": "?"}
        )
        payload["activity"] = normalized_activity.get(
            name,
            {"worker": False, "correspondence": False, "soundness": False, "reviewer": False},
        )
    return {
        "state": state.to_dict(),
        "tablet": tablet.to_dict(),
        "nodes": nodes,
        "meta": {
            "source": source,
            "cycle_checkpoint": state.cycle,
            "in_flight_cycle": in_flight_cycle if in_flight_cycle is not None else state.cycle,
        },
    }


def build_historical_viewer_state(
    repo_path: Path,
    tag: str,
    tablet: TabletState,
    state: SupervisorState,
    *,
    verification_results: Optional[List[Dict[str, Any]]] = None,
    source: str = "git",
) -> Dict[str, Any]:
    snapshot = GitSnapshot(repo_path, tag)
    nodes = _build_nodes(tablet, snapshot)
    results = verification_results or []
    verification = _historical_verification_statuses(snapshot, tablet, state, results)
    activity = _normalize_activity(nodes.keys(), _historical_activity(tablet, state, results))
    for name, payload in nodes.items():
        if name == PREAMBLE_NAME:
            continue
        payload["verification"] = verification.get(
            name, {"correspondence": "?", "nl_proof": "?"}
        )
        payload["activity"] = activity.get(
            name,
            {"worker": False, "correspondence": False, "soundness": False, "reviewer": False},
        )
    return {
        "state": state.to_dict(),
        "tablet": tablet.to_dict(),
        "nodes": nodes,
        "meta": {
            "source": source,
            "tag": tag,
            "cycle_checkpoint": state.cycle,
            "in_flight_cycle": state.cycle,
        },
    }


def write_live_viewer_state(
    path: Path,
    repo_path: Path,
    tablet: TabletState,
    state: SupervisorState,
    *,
    activity: Optional[Dict[str, Iterable[str]]] = None,
    in_flight_cycle: Optional[int] = None,
    source: str = "live",
    fast: bool = False,
) -> Dict[str, Any]:
    payload = build_live_viewer_state(
        repo_path,
        tablet,
        state,
        activity=activity,
        in_flight_cycle=in_flight_cycle,
        source=source,
        fast=fast,
    )
    save_json(path, payload)
    _mirror_static_project_payloads(repo_path, payload)
    return payload


def write_cycle_viewer_state(
    path: Path,
    repo_path: Path,
    tablet: TabletState,
    state: SupervisorState,
    *,
    verification_results: Optional[List[Dict[str, Any]]] = None,
    source: str = "cycle",
) -> Dict[str, Any]:
    # Use the current filesystem contents, but freeze statuses/activity as the
    # finished cycle should display them historically.
    payload = build_live_viewer_state(
        repo_path,
        tablet,
        replace(state),
        activity=_historical_activity(tablet, state, verification_results or []),
        in_flight_cycle=state.cycle,
        source=source,
    )
    results = verification_results or []
    if results:
        for name, status in _historical_verification_statuses(
            FsSnapshot(repo_path), tablet, state, results
        ).items():
            if name in payload["nodes"]:
                payload["nodes"][name]["verification"] = status
    save_json(path, payload)
    _mirror_static_project_payloads(repo_path, payload)
    return payload


def load_tagged_viewer_state(repo_path: Path, tag: str) -> Optional[Dict[str, Any]]:
    snapshot = GitSnapshot(repo_path, tag)
    raw = snapshot.read_text(".agent-supervisor/viewer_state.json")
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _git_text(repo_path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_path), *args],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=10,
    ).strip()


def _project_cycles(repo_path: Path) -> List[Dict[str, Any]]:
    try:
        tags = [
            tag
            for tag in _git_text(repo_path, "tag", "-l", "cycle-*", "--sort=version:refname").splitlines()
            if re.match(r"^cycle-\d+$", tag)
        ]
    except Exception:
        return []

    cycles: List[Dict[str, Any]] = []
    for tag in tags:
        cycle = int(tag.replace("cycle-", ""))
        hash_value = ""
        timestamp = ""
        message = ""
        try:
            parts = _git_text(repo_path, "log", "-1", "--format=%H%n%aI%n%s", tag).splitlines()
            hash_value = parts[0] if len(parts) > 0 else ""
            timestamp = parts[1] if len(parts) > 1 else ""
            message = parts[2] if len(parts) > 2 else ""
        except Exception:
            pass
        meta: Dict[str, Any] = {}
        try:
            meta_raw = _git_text(repo_path, "show", f"{tag}:.agent-supervisor/cycle_meta.json")
            parsed = json.loads(meta_raw)
            if isinstance(parsed, dict):
                meta = parsed
        except Exception:
            pass
        cycles.append({
            "cycle": cycle,
            "hash": hash_value,
            "timestamp": timestamp,
            "message": message,
            **meta,
        })
    return cycles


def _ensure_project_static_shell(static_out: Path, project_slug: str) -> None:
    root_index = static_out / "index.html"
    if not root_index.exists():
        return
    project_root = static_out / project_slug
    project_root.mkdir(parents=True, exist_ok=True)
    target = project_root / "index.html"
    shutil.copyfile(root_index, target)
    os.chmod(target, 0o644)


def _mirror_static_project_payloads(repo_path: Path, live_payload: Dict[str, Any]) -> None:
    static_out = _static_out_dir()
    project_slug = viewer_project_slug(repo_path)
    api_roots = [
        static_out / "api",
        static_out / project_slug / "api",
    ]
    _ensure_project_static_shell(static_out, project_slug)

    cycles = _project_cycles(repo_path)
    for api_root in api_roots:
        _write_json_world_readable(api_root / "viewer-state.json", live_payload)
        _write_json_world_readable(api_root / "cycles.json", cycles)
        state_at_dir = api_root / "state-at"
        state_at_dir.mkdir(parents=True, exist_ok=True)
        valid = set()
        for entry in cycles:
            cycle = entry.get("cycle")
            if not isinstance(cycle, int):
                continue
            valid.add(str(cycle))
            payload = load_tagged_viewer_state(repo_path, f"cycle-{cycle}")
            if payload is None:
                try:
                    payload = build_legacy_backfill_viewer_state(repo_path, cycle)
                except Exception:
                    payload = None
            if payload is not None:
                _write_json_world_readable(state_at_dir / f"{cycle}.json", payload)
        for existing in state_at_dir.iterdir():
            if not existing.is_file() or existing.suffix != ".json":
                continue
            if existing.stem in valid:
                continue
            try:
                existing.unlink()
            except OSError:
                pass


def build_legacy_backfill_viewer_state(repo_path: Path, cycle: int) -> Dict[str, Any]:
    tag = f"cycle-{cycle}"
    tablet_raw = subprocess.check_output(
        ["git", "-C", str(repo_path), "show", f"{tag}:.agent-supervisor/tablet.json"],
        text=True,
        timeout=10,
    )
    state_raw = subprocess.check_output(
        ["git", "-C", str(repo_path), "show", f"{tag}:.agent-supervisor/state.json"],
        text=True,
        timeout=10,
    )
    tablet = TabletState.from_dict(json.loads(tablet_raw))
    state = SupervisorState.from_dict(json.loads(state_raw))
    verification_results: List[Dict[str, Any]] = []
    try:
        meta_raw = subprocess.check_output(
            ["git", "-C", str(repo_path), "show", f"{tag}:.agent-supervisor/cycle_meta.json"],
            text=True,
            timeout=10,
        )
        meta = json.loads(meta_raw)
        raw_results = meta.get("verification_results")
        if isinstance(raw_results, list):
            verification_results = raw_results
    except Exception:
        verification_results = []
    return build_historical_viewer_state(
        repo_path,
        tag,
        tablet,
        state,
        verification_results=verification_results,
        source="backfill",
    )


def write_legacy_backfill_viewer_state(
    repo_path: Path,
    cycle: int,
    *,
    static_out: Path,
) -> Path:
    cache_dir = backfill_cache_dir(static_out, repo_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"{cycle}.json"
    payload = build_legacy_backfill_viewer_state(repo_path, cycle)
    save_json(out_path, payload)
    return out_path


def load_current_viewer_state(state_dir: Path) -> Optional[Dict[str, Any]]:
    data = load_json(viewer_state_path(state_dir), default=None)
    return data if isinstance(data, dict) else None
