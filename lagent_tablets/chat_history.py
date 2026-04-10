"""Canonical project-local chat history paths and git operations."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.project_paths import project_chats_dir


FINAL_CYCLE_TAG_RE = re.compile(r"^cycle-(\d+)$")
CHECKPOINT_TAG_RE = re.compile(r"^cycle-(\d+)-(worker|verification)$")
CHECKPOINT_STAGE_ORDER = {"worker": 0, "verification": 1}
ARTIFACT_CYCLE_DIR_RE = re.compile(r"^cycle-(\d{4})$")


def chat_repo_path(repo_path: Path) -> Path:
    return project_chats_dir(repo_path / ".agent-supervisor")


def ensure_chat_repo(repo_path: Path) -> Path:
    repo = chat_repo_path(repo_path)
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init")
        _git(repo, "config", "user.name", "lagent-chats")
        _git(repo, "config", "user.email", "lagent-chats@localhost")
        readme = repo / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Project Chat History\n\n"
                "This nested git repo stores canonical per-cycle agent conversations.\n",
                encoding="utf-8",
            )
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "Initialize local chat history repo")
    return repo


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _root_commit(repo: Path) -> Optional[str]:
    result = _git(repo, "rev-list", "--max-parents=0", "HEAD", check=False)
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


def _tag_sort_key(tag: str) -> tuple[int, int]:
    tag = str(tag or "").strip()
    final_match = FINAL_CYCLE_TAG_RE.fullmatch(tag)
    if final_match:
        return (int(final_match.group(1)), 2)
    checkpoint_match = CHECKPOINT_TAG_RE.fullmatch(tag)
    if checkpoint_match:
        return (int(checkpoint_match.group(1)), CHECKPOINT_STAGE_ORDER[checkpoint_match.group(2)])
    return (-1, -1)


def chat_cycle_dir_name(cycle: int) -> str:
    return f"cycle-{int(cycle):04d}"


def _cycle_dir_name_from_log_dir(log_dir: Path) -> str:
    name = log_dir.name
    if ARTIFACT_CYCLE_DIR_RE.fullmatch(name):
        return name
    return "live"


def _artifact_prefix(prefix: Optional[str], role: str) -> str:
    base = prefix or role
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(base)).strip("._-") or role
    return base[:80]


def chat_artifact_dir(
    repo_path: Path,
    *,
    log_dir: Path,
    artifact_prefix: Optional[str],
    role: str,
) -> Path:
    chats = ensure_chat_repo(repo_path)
    cycle_dir = _cycle_dir_name_from_log_dir(log_dir)
    artifact_dir = chats / cycle_dir / _artifact_prefix(artifact_prefix, role)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def ensure_chat_file_link(
    repo_path: Path,
    *,
    log_dir: Path,
    artifact_prefix: Optional[str],
    role: str,
    log_filename: str,
    canonical_name: str,
) -> Path:
    artifact_dir = chat_artifact_dir(repo_path, log_dir=log_dir, artifact_prefix=artifact_prefix, role=role)
    canonical = artifact_dir / canonical_name
    log_path = log_dir / log_filename
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_path.is_symlink() and log_path.resolve() == canonical.resolve():
            return canonical
        if log_path.exists() or log_path.is_symlink():
            log_path.unlink()
    except FileNotFoundError:
        pass
    log_path.symlink_to(canonical)
    return canonical


def copy_chat_artifact(
    repo_path: Path,
    *,
    log_dir: Path,
    artifact_prefix: Optional[str],
    role: str,
    source_path: Path,
    canonical_name: str,
    symlink_name: Optional[str] = None,
) -> Path:
    artifact_dir = chat_artifact_dir(repo_path, log_dir=log_dir, artifact_prefix=artifact_prefix, role=role)
    target = artifact_dir / canonical_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target)
    if symlink_name:
        log_path = log_dir / symlink_name
        try:
            if log_path.exists() or log_path.is_symlink():
                log_path.unlink()
        except FileNotFoundError:
            pass
        log_path.symlink_to(target)
    return target


def commit_chat_checkpoint(repo_path: Path, *, tag: str) -> Optional[str]:
    chats = ensure_chat_repo(repo_path)
    _git(chats, "add", "-A")
    diff = _git(chats, "diff", "--cached", "--quiet", check=False)

    head_exists = _git(chats, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    if diff.returncode != 0:
        _git(chats, "commit", "-m", f"{tag}: chat snapshot")
    elif not head_exists:
        return None

    _git(chats, "tag", "-d", tag, check=False)
    _git(chats, "tag", tag)
    result = _git(chats, "rev-parse", "HEAD")
    return result.stdout.strip() or None


def commit_chat_attempt(
    repo_path: Path,
    *,
    cycle: int,
    attempt: int,
    label: str,
) -> Optional[str]:
    chats = ensure_chat_repo(repo_path)
    _git(chats, "add", "-A")
    diff = _git(chats, "diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        return None
    _git(
        chats,
        "commit",
        "-m",
        f"cycle-{int(cycle)} attempt-{int(attempt)}: {label}",
    )
    result = _git(chats, "rev-parse", "HEAD")
    return result.stdout.strip() or None


def rewind_chat_history(repo_path: Path, *, tag: str) -> None:
    chats = chat_repo_path(repo_path)
    if not (chats / ".git").exists():
        return

    normalized = str(tag or "").strip()
    if normalized == "initial":
        actual_ref = _root_commit(chats)
        target_key = (0, -1)
    else:
        check = _git(chats, "rev-parse", normalized, check=False)
        if check.returncode != 0:
            return
        actual_ref = normalized
        target_key = _tag_sort_key(normalized)

    if not actual_ref:
        return

    _git(chats, "reset", "--hard", actual_ref)
    _git(chats, "clean", "-fdx", timeout=120)

    tags_result = _git(chats, "tag", "-l", "cycle-*", check=False)
    if tags_result.returncode != 0:
        return
    for existing in [t.strip() for t in tags_result.stdout.splitlines() if t.strip()]:
        if _tag_sort_key(existing) > target_key:
            _git(chats, "tag", "-d", existing, check=False)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _normalize_entry(role: str, text: str, kind: str = "message", title: str = "") -> Optional[Dict[str, str]]:
    trimmed = str(text or "").strip()
    if not trimmed:
        return None
    return {
        "role": role or "entry",
        "kind": kind,
        "title": title or "",
        "text": trimmed,
    }


def _collect_text_parts(value: Any, parts: List[str]) -> None:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            parts.append(trimmed)
        return
    if isinstance(value, list):
        for item in value:
            _collect_text_parts(item, parts)
        return
    if not isinstance(value, dict):
        return
    text = value.get("text")
    if isinstance(text, str) and text.strip():
        parts.append(text.strip())
    for key in ("content", "parts", "chunks", "value"):
        if key in value:
            _collect_text_parts(value.get(key), parts)


def _parse_codex_output_entries(text: str) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        item = rec.get("item")
        if rec.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            entry = _normalize_entry("assistant", str(item.get("text") or ""), "message", "Assistant")
            if entry:
                entries.append(entry)
            continue
        if (
            isinstance(item, dict)
            and item.get("type") == "command_execution"
            and rec.get("type") in {"item.started", "item.completed"}
        ):
            command = str(item.get("command") or "").strip()
            output = str(item.get("aggregated_output") or "").strip()
            label = "Command (running)" if rec.get("type") == "item.started" else "Command"
            combined = "\n\n".join(part for part in (command, output) if part)
            entry = _normalize_entry("tool", combined, "command", label)
            if entry:
                entries.append(entry)
    return entries


def _parse_jsonl_transcript_entries(text: str) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
        role = ""
        if isinstance(msg, dict):
            role = str(msg.get("role") or rec.get("role") or rec.get("type") or "")
        parts: List[str] = []
        _collect_text_parts(msg.get("content") if isinstance(msg, dict) else rec.get("content"), parts)
        if not parts:
            _collect_text_parts(msg, parts)
        entry = _normalize_entry(role, "\n\n".join(parts), "message", role or "Entry")
        if entry:
            entries.append(entry)
    return entries


def _parse_json_transcript_entries(text: str) -> List[Dict[str, str]]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    entries: List[Dict[str, str]] = []
    messages = data.get("messages") if isinstance(data, dict) else None
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or msg.get("author") or msg.get("speaker") or "")
            parts: List[str] = []
            _collect_text_parts(msg.get("content"), parts)
            if not parts:
                _collect_text_parts(msg.get("parts"), parts)
            if not parts:
                _collect_text_parts(msg, parts)
            entry = _normalize_entry(role, "\n\n".join(parts), "message", role or "Entry")
            if entry:
                entries.append(entry)
    if entries:
        return entries
    parts: List[str] = []
    _collect_text_parts(data, parts)
    fallback = _normalize_entry("entry", "\n\n".join(parts), "message", "Transcript")
    return [fallback] if fallback else []


def _artifact_title(name: str) -> str:
    if name == "worker_handoff":
        return "Worker"
    if name == "reviewer_decision":
        return "Reviewer"
    m = re.match(r"^correspondence_result_(\d+)$", name)
    if m:
        return f"Correspondence {int(m.group(1)) + 1}"
    m = re.match(r"^nl_proof_(.+)_(\d+)$", name)
    if m:
        return f"Soundness {m.group(1)} ({int(m.group(2)) + 1})"
    return name.replace("_", " ")


def _build_artifact_chat_data(artifact: str, files: Dict[str, str]) -> Dict[str, Any]:
    entries: List[Dict[str, str]] = []
    prompt_entry = _normalize_entry("prompt", files.get("prompt", ""), "prompt", "Prompt")
    if prompt_entry:
        entries.append(prompt_entry)
    entries.extend(_parse_jsonl_transcript_entries(files.get("transcriptJsonl", "")))
    entries.extend(_parse_json_transcript_entries(files.get("transcriptJson", "")))
    if not any(entry.get("role") == "assistant" for entry in entries):
        entries.extend(_parse_codex_output_entries(files.get("output", "")))
    return {
        "id": artifact,
        "title": _artifact_title(artifact),
        "entries": entries,
    }


def _read_working_tree_chat_files(repo_path: Path, cycle: int, artifact: str) -> Dict[str, str]:
    base = ensure_chat_repo(repo_path) / chat_cycle_dir_name(cycle) / artifact
    return {
        "prompt": _read_text(base / "prompt.txt"),
        "output": _read_text(base / "output.log"),
        "transcriptJsonl": _read_text(base / "transcript.jsonl"),
        "transcriptJson": _read_text(base / "transcript.json"),
    }


def _read_git_chat_file(chats_repo: Path, tag: str, rel_path: str) -> str:
    result = _git(chats_repo, "show", f"{tag}:{rel_path}", check=False, timeout=30)
    if result.returncode != 0:
        return ""
    return result.stdout


def _read_git_chat_files(repo_path: Path, cycle: int, artifact: str) -> Dict[str, str]:
    chats_repo = ensure_chat_repo(repo_path)
    base = f"{chat_cycle_dir_name(cycle)}/{artifact}"
    tag = f"cycle-{cycle}"
    return {
        "prompt": _read_git_chat_file(chats_repo, tag, f"{base}/prompt.txt"),
        "output": _read_git_chat_file(chats_repo, tag, f"{base}/output.log"),
        "transcriptJsonl": _read_git_chat_file(chats_repo, tag, f"{base}/transcript.jsonl"),
        "transcriptJson": _read_git_chat_file(chats_repo, tag, f"{base}/transcript.json"),
    }


def _list_working_tree_chat_artifacts(repo_path: Path, cycle: int) -> List[str]:
    root = ensure_chat_repo(repo_path) / chat_cycle_dir_name(cycle)
    if not root.exists():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def _list_git_chat_artifacts(repo_path: Path, cycle: int) -> List[str]:
    chats_repo = ensure_chat_repo(repo_path)
    prefix = f"{chat_cycle_dir_name(cycle)}/"
    result = _git(chats_repo, "ls-tree", "-r", "--name-only", f"cycle-{cycle}", "--", prefix, check=False)
    if result.returncode != 0:
        return []
    artifacts = {
        line[len(prefix):].split("/", 1)[0]
        for line in result.stdout.splitlines()
        if line.startswith(prefix) and line[len(prefix):]
    }
    return sorted(artifacts)


def read_live_chats(repo_path: Path, cycle: int) -> Dict[str, Any]:
    artifacts = _list_working_tree_chat_artifacts(repo_path, cycle)
    return {
        "cycle": cycle,
        "source": "live",
        "artifacts": [_build_artifact_chat_data(artifact, _read_working_tree_chat_files(repo_path, cycle, artifact)) for artifact in artifacts],
    }


def read_historical_chats(repo_path: Path, cycle: int) -> Dict[str, Any]:
    artifacts = _list_git_chat_artifacts(repo_path, cycle)
    return {
        "cycle": cycle,
        "source": f"cycle-{cycle}",
        "artifacts": [_build_artifact_chat_data(artifact, _read_git_chat_files(repo_path, cycle, artifact)) for artifact in artifacts],
    }
