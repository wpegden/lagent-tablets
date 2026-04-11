"""Filesystem sandbox helpers for agent bursts."""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from lagent_tablets.config import SandboxConfig


_SYSTEM_READONLY_DIRS = (
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/etc"),
    Path("/opt"),
)
_HOST_RUNTIME_DIRS = (
    Path("/home/leanagent/.elan"),
    Path("/home/leanagent/.local/bin"),
    Path("/home/leanagent/.local/share"),
    Path("/home/leanagent/.nvm"),
)
_HOST_CONFIG_SYMLINKS = (
    Path("/etc/resolv.conf"),
)


def bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def _ancestor_dirs(paths: Iterable[Path]) -> List[Path]:
    ordered: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        current = path.resolve()
        parents: list[Path] = []
        while True:
            current = current.parent
            if current == Path("/") or str(current) == ".":
                break
            parents.append(current)
        for parent in reversed(parents):
            if parent not in seen:
                ordered.append(parent)
                seen.add(parent)
    return ordered


def _host_extra_readonly_paths() -> List[Path]:
    """Return extra host paths that must be mounted because config symlinks escape /etc."""
    extra: list[Path] = []
    seen: set[Path] = set()
    for path in _HOST_CONFIG_SYMLINKS:
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            continue
        if resolved.is_relative_to(Path("/etc")):
            continue
        if resolved not in seen:
            extra.append(resolved)
            seen.add(resolved)
    return extra


def wrap_command(
    inner_cmd: List[str],
    *,
    sandbox: Optional[SandboxConfig],
    work_dir: Path,
    burst_user: Optional[str],
    burst_home: Optional[Path] = None,
) -> List[str]:
    """Wrap a command in bubblewrap if sandboxing is enabled."""
    if sandbox is None or not sandbox.enabled:
        return inner_cmd
    if sandbox.backend != "bwrap":
        raise ValueError(f"Unsupported sandbox backend: {sandbox.backend}")
    if not bwrap_available():
        raise RuntimeError("bwrap is required for sandboxed bursts but is not installed")

    repo = work_dir.resolve()
    home = (burst_home or (Path(f"/home/{burst_user}") if burst_user else Path.home())).resolve()
    extra_readonly = _host_extra_readonly_paths()

    bind_targets = [
        repo,
        home,
        Path("/tmp"),
        Path("/var/tmp"),
        *[p for p in _SYSTEM_READONLY_DIRS if p.exists()],
        *[p for p in _HOST_RUNTIME_DIRS if p.exists()],
        *extra_readonly,
    ]
    cmd: List[str] = [
        "bwrap",
        "--die-with-parent",
        "--proc", "/proc",
        "--dev-bind", "/dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/var/tmp",
    ]
    for parent in _ancestor_dirs(bind_targets):
        cmd.extend(["--dir", str(parent)])

    for path in _SYSTEM_READONLY_DIRS:
        if path.exists():
            cmd.extend(["--ro-bind", str(path), str(path)])
    for path in _HOST_RUNTIME_DIRS:
        if path.exists():
            cmd.extend(["--ro-bind", str(path), str(path)])
    for path in extra_readonly:
        cmd.extend(["--ro-bind", str(path), str(path)])

    cmd.extend(["--bind", str(home), str(home)])
    cmd.extend(["--bind", str(repo), str(repo)])
    cmd.extend(["--setenv", "HOME", str(home)])
    cmd.extend(["--setenv", "TMPDIR", "/tmp"])
    cmd.extend(["--chdir", str(repo)])
    cmd.extend(inner_cmd)
    return cmd


def probe_sandbox(
    *,
    sandbox: Optional[SandboxConfig],
    work_dir: Path,
    burst_user: Optional[str],
    burst_home: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Return whether the configured sandbox can successfully execute a trivial command."""
    if sandbox is None or not sandbox.enabled:
        return True, ""
    try:
        inner = wrap_command(
            ["/bin/bash", "-lc", "true"],
            sandbox=sandbox,
            work_dir=work_dir,
            burst_user=burst_user,
            burst_home=burst_home,
        )
    except Exception as exc:
        return False, str(exc)

    cmd = inner
    if burst_user:
        cmd = ["sudo", "-n", "-u", burst_user, *inner]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
    return False, detail
