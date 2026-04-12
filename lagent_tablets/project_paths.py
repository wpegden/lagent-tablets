"""Canonical project-local paths for mutable lagent state."""

from __future__ import annotations

from pathlib import Path


PROJECT_CONFIG_FILENAME = "lagent.config.json"
PROJECT_POLICY_FILENAME = "lagent.policy.json"


def project_config_path(repo_path: Path) -> Path:
    return repo_path / PROJECT_CONFIG_FILENAME


def project_policy_path(repo_path: Path) -> Path:
    return repo_path / PROJECT_POLICY_FILENAME


def project_chats_dir(state_dir: Path) -> Path:
    return state_dir / "chats"


def project_scratch_dir(state_dir: Path) -> Path:
    return state_dir / "scratch"


def project_feedback_log_path(state_dir: Path) -> Path:
    return state_dir / "agent_feedback.jsonl"


def project_runtime_dir(state_dir: Path) -> Path:
    return state_dir / "runtime"


def project_runtime_src_dir(state_dir: Path) -> Path:
    return project_runtime_dir(state_dir) / "src"


def project_runtime_skills_dir(state_dir: Path) -> Path:
    return project_runtime_dir(state_dir) / "skills"


def project_viewer_dir(state_dir: Path) -> Path:
    return state_dir / "viewer"


def project_viewer_state_path(state_dir: Path) -> Path:
    return project_viewer_dir(state_dir) / "viewer-state.json"


def project_viewer_cycles_path(state_dir: Path) -> Path:
    return project_viewer_dir(state_dir) / "cycles.json"


def project_viewer_chats_path(state_dir: Path) -> Path:
    return project_viewer_dir(state_dir) / "chats.json"


def project_viewer_chats_at_dir(state_dir: Path) -> Path:
    return project_viewer_dir(state_dir) / "chats-at"


def project_viewer_state_at_dir(state_dir: Path) -> Path:
    return project_viewer_dir(state_dir) / "state-at"
