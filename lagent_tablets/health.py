"""Health monitoring, logging, and recovery for agent interactions.

Every burst, validation, and cycle is logged to a structured JSONL file.
The health monitor tracks success rates and detects systematic failures.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.state import append_jsonl, timestamp_now


SHARED_FILE_MODE = 0o664
SHARED_DIR_MODE = 0o2775


# ---------------------------------------------------------------------------
# Structured event logging
# ---------------------------------------------------------------------------

def log_event(
    log_path: Path,
    *,
    event: str,
    cycle: int = 0,
    provider: str = "",
    role: str = "",
    duration_seconds: float = 0,
    outcome: str = "",
    detail: str = "",
    error: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a structured event to the supervisor log."""
    record: Dict[str, Any] = {
        "timestamp": timestamp_now(),
        "event": event,
        "cycle": cycle,
    }
    if provider:
        record["provider"] = provider
    if role:
        record["role"] = role
    if duration_seconds:
        record["duration_seconds"] = round(duration_seconds, 1)
    if outcome:
        record["outcome"] = outcome
    if detail:
        record["detail"] = detail[:500]
    if error:
        record["error"] = error[:500]
    if extra:
        record["extra"] = extra

    try:
        append_jsonl(log_path, record)
    except (OSError, TypeError) as exc:
        print(f"WARNING: Could not write log event: {exc}")


# ---------------------------------------------------------------------------
# Health tracking
# ---------------------------------------------------------------------------

@dataclass
class HealthStats:
    """Aggregated health statistics."""
    total_bursts: int = 0
    successful_bursts: int = 0
    failed_bursts: int = 0
    stall_recoveries: int = 0
    rate_limit_retries: int = 0
    no_progress_cycles: int = 0
    invalid_cycles: int = 0
    progress_cycles: int = 0
    consecutive_failures: int = 0
    last_success_time: float = 0
    last_failure_time: float = 0
    last_failure_error: str = ""


class HealthMonitor:
    """Tracks burst and cycle health, detects systematic failures."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.stats = HealthStats()
        self._cycle_start_time: float = 0

    def on_cycle_start(self, cycle: int) -> None:
        self._cycle_start_time = time.monotonic()
        log_event(self.log_path, event="cycle_start", cycle=cycle)

    def on_burst_complete(
        self, cycle: int, provider: str, role: str,
        ok: bool, duration: float, error: str = "",
        stall_recoveries: int = 0,
    ) -> None:
        self.stats.total_bursts += 1
        if ok:
            self.stats.successful_bursts += 1
            self.stats.consecutive_failures = 0
            self.stats.last_success_time = time.monotonic()
        else:
            self.stats.failed_bursts += 1
            self.stats.consecutive_failures += 1
            self.stats.last_failure_time = time.monotonic()
            self.stats.last_failure_error = error
        self.stats.stall_recoveries += stall_recoveries

        log_event(
            self.log_path,
            event="burst_complete",
            cycle=cycle,
            provider=provider,
            role=role,
            duration_seconds=duration,
            outcome="ok" if ok else "failed",
            error=error,
            extra={"stall_recoveries": stall_recoveries} if stall_recoveries else None,
        )

    def on_cycle_outcome(self, cycle: int, outcome: str, detail: str = "") -> None:
        if outcome == "PROGRESS":
            self.stats.progress_cycles += 1
        elif outcome == "NO_PROGRESS":
            self.stats.no_progress_cycles += 1
        elif outcome == "INVALID":
            self.stats.invalid_cycles += 1

        duration = time.monotonic() - self._cycle_start_time if self._cycle_start_time else 0
        log_event(
            self.log_path,
            event="cycle_complete",
            cycle=cycle,
            outcome=outcome,
            detail=detail,
            duration_seconds=duration,
        )

    def on_validation(
        self, cycle: int, node: str,
        compiles: bool, sorry_free: bool, error: str = "",
    ) -> None:
        log_event(
            self.log_path,
            event="validation",
            cycle=cycle,
            detail=f"node={node} compiles={compiles} sorry_free={sorry_free}",
            error=error,
        )

    def on_reviewer(self, cycle: int, decision: str, next_node: str = "") -> None:
        log_event(
            self.log_path,
            event="reviewer_decision",
            cycle=cycle,
            outcome=decision,
            detail=f"next_node={next_node}",
        )

    def on_reconcile(self, nodes: List[str]) -> None:
        if nodes:
            log_event(
                self.log_path,
                event="reconcile",
                detail=f"closed={nodes}",
            )

    def on_permission_setup(self, active_node: str) -> None:
        log_event(self.log_path, event="permission_setup", detail=f"active={active_node}")

    def on_lake_error(self, error: str) -> None:
        log_event(self.log_path, event="lake_error", error=error)

    def should_restart_agent(self, stall_threshold_minutes: float = 30) -> bool:
        """Check if the agent seems wedged and needs a fresh restart.

        Returns True if:
        - 3+ consecutive burst failures
        - No successful burst in stall_threshold_minutes
        """
        if self.stats.consecutive_failures >= 3:
            return True
        if self.stats.last_success_time > 0:
            minutes_since_success = (time.monotonic() - self.stats.last_success_time) / 60
            if minutes_since_success > stall_threshold_minutes and self.stats.total_bursts > 0:
                return True
        return False

    def summary(self) -> Dict[str, Any]:
        return {
            "total_bursts": self.stats.total_bursts,
            "successful_bursts": self.stats.successful_bursts,
            "failed_bursts": self.stats.failed_bursts,
            "success_rate": round(self.stats.successful_bursts / max(1, self.stats.total_bursts), 2),
            "stall_recoveries": self.stats.stall_recoveries,
            "progress_cycles": self.stats.progress_cycles,
            "no_progress_cycles": self.stats.no_progress_cycles,
            "invalid_cycles": self.stats.invalid_cycles,
            "consecutive_failures": self.stats.consecutive_failures,
            "last_failure_error": self.stats.last_failure_error,
        }


def _lake_build_roots(repo_path: Path, *, include_package_builds: bool = False) -> List[Path]:
    roots: List[Path] = []
    main_build = repo_path / ".lake" / "build"
    if main_build.exists():
        roots.append(main_build)
    if include_package_builds:
        packages_root = repo_path / ".lake" / "packages"
        if packages_root.exists():
            for package_dir in sorted(packages_root.iterdir()):
                build_root = package_dir / ".lake" / "build"
                if build_root.exists():
                    roots.append(build_root)
    return roots


def _shared_repo_roots(
    repo_path: Path,
    state_dir: Path,
    *,
    include_tablet: bool = True,
    include_staging: bool = True,
) -> List[Path]:
    roots: List[Path] = []
    if include_tablet:
        roots.append(repo_path / "Tablet")
        roots.append(repo_path / "Tablet.lean")
    if include_staging:
        roots.append(state_dir / "staging")
    return roots


def _normalize_root(root: Path, *, gid: int) -> None:
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return
    except OSError:
        return

    if stat.S_ISLNK(st.st_mode):
        return
    if stat.S_ISREG(st.st_mode):
        try:
            os.chown(str(root), -1, gid)
        except (PermissionError, OSError):
            pass
        try:
            os.chmod(str(root), SHARED_FILE_MODE)
        except (PermissionError, OSError):
            pass
        return
    if not stat.S_ISDIR(st.st_mode):
        return

    try:
        os.chown(str(root), -1, gid)
    except (PermissionError, OSError):
        pass
    try:
        os.chmod(str(root), SHARED_DIR_MODE)
    except (PermissionError, OSError):
        pass

    for current_root, _, files in os.walk(str(root), followlinks=False):
        current_path = Path(current_root)
        try:
            os.chown(str(current_path), -1, gid)
        except (PermissionError, OSError):
            pass
        try:
            os.chmod(str(current_path), SHARED_DIR_MODE)
        except (PermissionError, OSError):
            pass
        for name in files:
            file_path = current_path / name
            try:
                fst = os.lstat(file_path)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if not stat.S_ISREG(fst.st_mode):
                continue
            try:
                os.chown(str(file_path), -1, gid)
            except (PermissionError, OSError):
                pass
            try:
                os.chmod(str(file_path), SHARED_FILE_MODE)
            except (PermissionError, OSError):
                pass


def _normalize_roots_as_burst_user(
    roots: List[Path],
    *,
    burst_user: str,
    group: str,
) -> None:
    if not roots:
        return
    payload = json.dumps([str(root) for root in roots])
    script = r"""
import grp
import json
import os
import stat
import sys
from pathlib import Path

roots = [Path(p) for p in json.loads(sys.argv[1])]
gid = grp.getgrnam(sys.argv[2]).gr_gid
file_mode = 0o664
dir_mode = 0o2775

for root in roots:
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        continue
    except OSError:
        continue
    if stat.S_ISLNK(st.st_mode):
        continue
    if stat.S_ISREG(st.st_mode):
        try:
            os.chown(str(root), -1, gid)
        except (PermissionError, OSError):
            pass
        try:
            os.chmod(str(root), file_mode)
        except (PermissionError, OSError):
            pass
        continue
    if not stat.S_ISDIR(st.st_mode):
        continue
    try:
        os.chown(str(root), -1, gid)
    except (PermissionError, OSError):
        pass
    try:
        os.chmod(str(root), dir_mode)
    except (PermissionError, OSError):
        pass
    for current_root, _, files in os.walk(str(root), followlinks=False):
        current_path = Path(current_root)
        try:
            os.chown(str(current_path), -1, gid)
        except (PermissionError, OSError):
            pass
        try:
            os.chmod(str(current_path), dir_mode)
        except (PermissionError, OSError):
            pass
        for name in files:
            file_path = current_path / name
            try:
                fst = os.lstat(file_path)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if not stat.S_ISREG(fst.st_mode):
                continue
            try:
                os.chown(str(file_path), -1, gid)
            except (PermissionError, OSError):
                pass
            try:
                os.chmod(str(file_path), file_mode)
            except (PermissionError, OSError):
                pass
"""
    try:
        subprocess.run(
            ["sudo", "-n", "-u", burst_user, "python3", "-c", script, payload, group],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def fix_shared_repo_permissions(
    repo_path: Path,
    state_dir: Path,
    group: str = "leanagent",
    *,
    burst_user: Optional[str] = None,
    include_tablet: bool = True,
    include_staging: bool = True,
) -> None:
    """Normalize shared repo surfaces that the supervisor reads after bursts."""
    import grp

    roots = _shared_repo_roots(
        repo_path,
        state_dir,
        include_tablet=include_tablet,
        include_staging=include_staging,
    )
    if not roots:
        return

    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return

    for root in roots:
        _normalize_root(root, gid=gid)
    if burst_user:
        _normalize_roots_as_burst_user(roots, burst_user=burst_user, group=group)


def _validate_regular_file(path: Path, *, label: str) -> List[str]:
    errors: List[str] = []
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return errors
    except OSError as exc:
        return [f"{label}: lstat failed ({exc})"]
    if stat.S_ISLNK(st.st_mode):
        return [f"{label}: symlinks are not allowed"]
    if not stat.S_ISREG(st.st_mode):
        return [f"{label}: expected a regular file"]
    if not os.access(path, os.R_OK):
        return [f"{label}: supervisor cannot read file"]
    return errors


def _validate_directory_entries(path: Path, *, label: str, allow_nested_dirs: bool) -> List[str]:
    errors: List[str] = []
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return errors
    except OSError as exc:
        return [f"{label}: lstat failed ({exc})"]
    if stat.S_ISLNK(st.st_mode):
        return [f"{label}: symlinks are not allowed"]
    if not stat.S_ISDIR(st.st_mode):
        return [f"{label}: expected a directory"]
    if not os.access(path, os.R_OK | os.X_OK):
        errors.append(f"{label}: supervisor cannot traverse directory")
        return errors
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        return [f"{label}: could not list directory ({exc})"]
    for entry in entries:
        entry_label = f"{label}/{entry.name}"
        try:
            st = os.lstat(entry)
        except OSError as exc:
            errors.append(f"{entry_label}: lstat failed ({exc})")
            continue
        if stat.S_ISLNK(st.st_mode):
            errors.append(f"{entry_label}: symlinks are not allowed")
            continue
        if stat.S_ISDIR(st.st_mode):
            if allow_nested_dirs:
                if not os.access(entry, os.R_OK | os.X_OK):
                    errors.append(f"{entry_label}: supervisor cannot traverse directory")
            else:
                errors.append(f"{entry_label}: unexpected nested directory")
            continue
        if not stat.S_ISREG(st.st_mode):
            errors.append(f"{entry_label}: expected a regular file")
            continue
        if not os.access(entry, os.R_OK):
            errors.append(f"{entry_label}: supervisor cannot read file")
    return errors


def validate_supervisor_read_surfaces(
    repo_path: Path,
    state_dir: Path,
    *,
    include_tablet: bool = True,
    include_staging: bool = True,
) -> List[str]:
    """Validate that the supervisor-readable repo surfaces are sane and readable."""
    errors: List[str] = []
    if include_tablet:
        errors.extend(_validate_directory_entries(repo_path / "Tablet", label="Tablet", allow_nested_dirs=False))
        errors.extend(_validate_regular_file(repo_path / "Tablet.lean", label="Tablet.lean"))
    if include_staging:
        errors.extend(
            _validate_directory_entries(
                state_dir / "staging",
                label=".agent-supervisor/staging",
                allow_nested_dirs=False,
            )
        )
    return errors


def prepare_supervisor_read_surfaces(
    repo_path: Path,
    state_dir: Path,
    group: str = "leanagent",
    *,
    burst_user: Optional[str] = None,
    include_tablet: bool = True,
    include_staging: bool = True,
    include_package_builds: bool = False,
) -> List[str]:
    """Normalize and validate the repo surfaces the supervisor must read."""
    fix_shared_repo_permissions(
        repo_path,
        state_dir,
        group=group,
        burst_user=burst_user,
        include_tablet=include_tablet,
        include_staging=include_staging,
    )
    if include_package_builds:
        fix_lake_permissions(
            repo_path,
            group=group,
            burst_user=burst_user,
            include_package_builds=include_package_builds,
        )
    return validate_supervisor_read_surfaces(
        repo_path,
        state_dir,
        include_tablet=include_tablet,
        include_staging=include_staging,
    )


def _normalize_build_tree(root: Path, *, gid: int) -> None:
    try:
        os.chown(str(root), -1, gid)
    except (PermissionError, OSError):
        pass
    try:
        os.chmod(str(root), 0o2775)
    except (PermissionError, OSError):
        pass
    for current_root, dirs, files in os.walk(str(root)):
        current_path = Path(current_root)
        try:
            os.chown(str(current_path), -1, gid)
        except (PermissionError, OSError):
            pass
        try:
            os.chmod(str(current_path), 0o2775)
        except (PermissionError, OSError):
            pass
        for name in files:
            file_path = current_path / name
            try:
                os.chown(str(file_path), -1, gid)
            except (PermissionError, OSError):
                pass
            try:
                os.chmod(str(file_path), 0o664)
            except (PermissionError, OSError):
                pass


def _normalize_build_roots_as_burst_user(
    roots: List[Path],
    *,
    burst_user: str,
    group: str,
) -> None:
    if not roots:
        return
    payload = json.dumps([str(root) for root in roots])
    script = r"""
import grp
import json
import os
import sys
from pathlib import Path

roots = [Path(p) for p in json.loads(sys.argv[1])]
gid = grp.getgrnam(sys.argv[2]).gr_gid

for root in roots:
    if not root.exists():
        continue
    try:
        os.chown(str(root), -1, gid)
    except (PermissionError, OSError):
        pass
    try:
        os.chmod(str(root), 0o2775)
    except (PermissionError, OSError):
        pass
    for current_root, dirs, files in os.walk(str(root)):
        current_path = Path(current_root)
        try:
            os.chown(str(current_path), -1, gid)
        except (PermissionError, OSError):
            pass
        try:
            os.chmod(str(current_path), 0o2775)
        except (PermissionError, OSError):
            pass
        for name in files:
            file_path = current_path / name
            try:
                os.chown(str(file_path), -1, gid)
            except (PermissionError, OSError):
                pass
            try:
                os.chmod(str(file_path), 0o664)
            except (PermissionError, OSError):
                pass
"""
    try:
        subprocess.run(
            ["sudo", "-n", "-u", burst_user, "python3", "-c", script, payload, group],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def fix_lake_permissions(
    repo_path: Path,
    group: str = "leanagent",
    *,
    burst_user: Optional[str] = None,
    include_package_builds: bool = False,
) -> None:
    """Ensure Lean build artifacts are group-readable/writable for shared access.

    We normalize:
    - repo/.lake/build/**
    - optionally repo/.lake/packages/*/.lake/build/**

    We intentionally do NOT touch package source checkouts outside those build
    directories, because those are real git working trees.
    """
    import grp

    roots = _lake_build_roots(repo_path, include_package_builds=include_package_builds)
    if not roots:
        return

    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return

    for root in roots:
        _normalize_build_tree(root, gid=gid)
    if burst_user:
        _normalize_build_roots_as_burst_user(roots, burst_user=burst_user, group=group)
