"""Project-local runtime snapshot for sandboxed agent bursts."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from lagent_tablets.project_paths import (
    project_runtime_dir,
    project_runtime_skills_dir,
    project_runtime_src_dir,
)


SOURCE_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_SOURCE_DIR = SOURCE_ROOT / "lagent_tablets"
SKILLS_SOURCE_DIR = SOURCE_ROOT / "skills"
SCRIPT_SOURCES = (
    SOURCE_ROOT / "scripts" / "lean_semantic_fingerprint.lean",
)


def _copytree_filtered(src: Path, dst: Path) -> None:
    def _ignore(_dir: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            if name == "__pycache__" or name.endswith(".pyc"):
                ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=_ignore, dirs_exist_ok=True)


def _replace_dir_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix=f"{dst.name}.tmp.", dir=str(dst.parent)))
    try:
        staged = tmp_root / dst.name
        if src.is_dir():
            _copytree_filtered(src, staged)
        else:
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, staged)
        if dst.exists():
            shutil.rmtree(dst)
        staged.rename(dst)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _normalize_permissions(root: Path) -> None:
    if not root.exists():
        return
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        current_path.chmod(0o755)
        for name in dirs:
            (current_path / name).chmod(0o755)
        for name in files:
            (current_path / name).chmod(0o644)


def materialize_project_runtime(repo_path: Path, state_dir: Path) -> None:
    """Refresh the project-local runtime snapshot used by sandboxed agents."""
    runtime_dir = project_runtime_dir(state_dir)
    runtime_src_dir = project_runtime_src_dir(state_dir)
    runtime_skills_dir = project_runtime_skills_dir(state_dir)

    runtime_dir.mkdir(parents=True, exist_ok=True)

    _replace_dir_atomic(PACKAGE_SOURCE_DIR, runtime_src_dir / "lagent_tablets")
    for script_src in SCRIPT_SOURCES:
        if not script_src.exists():
            continue
        script_dst = runtime_src_dir / "scripts" / script_src.name
        script_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script_src, script_dst)

    _replace_dir_atomic(SKILLS_SOURCE_DIR, runtime_skills_dir)

    _normalize_permissions(runtime_src_dir)
    _normalize_permissions(runtime_skills_dir)
