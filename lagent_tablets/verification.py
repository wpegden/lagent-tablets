"""Lean verification: sorry scan, forbidden keywords, import validation, lake env lean.

Also generates check_node.sh and check_tablet.sh scripts.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from lagent_tablets.config import FORBIDDEN_KEYWORDS_DEFAULT
from lagent_tablets.tablet import (
    AXIOMS_NAME,
    PREAMBLE_NAME,
    TABLET_DIR,
    declaration_hash,
    extract_imports,
    has_sorry,
    mask_comments_and_strings,
    node_lean_path,
    scan_forbidden_keywords,
    validate_imports,
)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class NodeCheckResult:
    """Result of checking a single tablet node."""
    name: str
    exists: bool = True
    compiles: bool = False
    sorry_free: bool = False
    keyword_clean: bool = False
    imports_valid: bool = False
    declaration_intact: bool = True
    returncode: Optional[int] = None
    build_output: str = ""
    sorry_warnings: List[str] = field(default_factory=list)
    forbidden_hits: List[Dict[str, Any]] = field(default_factory=list)
    import_violations: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def closed(self) -> bool:
        return self.compiles and self.sorry_free and self.keyword_clean and self.imports_valid and self.declaration_intact


@dataclass
class TabletCheckResult:
    """Result of checking the full tablet."""
    nodes: Dict[str, NodeCheckResult] = field(default_factory=dict)
    build_ok: bool = False
    build_output: str = ""

    @property
    def all_ok(self) -> bool:
        return self.build_ok and all(r.closed for r in self.nodes.values())


# ---------------------------------------------------------------------------
# Single-node verification
# ---------------------------------------------------------------------------

def check_node(
    repo_path: Path,
    name: str,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
    expected_declaration_hash: str = "",
    timeout_seconds: float = 120.0,
    burst_user: Optional[str] = None,
) -> NodeCheckResult:
    """Run all deterministic checks on a single tablet node.

    This is the same logic that check_node.sh runs.
    """
    result = NodeCheckResult(name=name)
    lean_path = node_lean_path(repo_path, name)

    if not lean_path.exists():
        result.exists = False
        result.error = f"File not found: {lean_path}"
        return result

    content = lean_path.read_text(encoding="utf-8")

    # 1. Import validation
    result.import_violations = validate_imports(content, allowed_prefixes)
    result.imports_valid = len(result.import_violations) == 0

    # 2. Forbidden keyword scan
    result.forbidden_hits = scan_forbidden_keywords(content, forbidden_keywords)
    # For "sorry" specifically, we also check build output (more reliable)
    non_sorry_hits = [h for h in result.forbidden_hits if h["keyword"] != "sorry"]
    result.keyword_clean = len(non_sorry_hits) == 0

    # 3. Declaration hash check (using node name from marker)
    if expected_declaration_hash:
        marker_name = None
        import re as _re
        marker_match = _re.search(r"-- \[TABLET NODE: (\w+)\]", content)
        if marker_match:
            marker_name = marker_match.group(1)
        actual_hash = declaration_hash(content, node_name=marker_name)
        result.declaration_intact = actual_hash == expected_declaration_hash

    # 3.5 Fix .lake build permissions before compilation (multi-user)
    from lagent_tablets.health import fix_lake_permissions
    fix_lake_permissions(repo_path)

    # 4. Run lake env lean
    # Note: we do NOT delete the .olean file. In multi-user mode, file permissions
    # prevent the worker from tampering with .olean files in .lake/build/.
    # Deleting oleans causes cascade rebuild failures when Lake can't find dependencies.

    build = _run_lake_env_lean(repo_path, name, timeout_seconds=timeout_seconds, burst_user=burst_user)
    result.returncode = build["returncode"]
    result.build_output = build["output"]

    # Lake sometimes fails with exit 1 due to package management issues (e.g., Mathlib URL
    # change + permission denied) even though the actual Lean code is fine. If the error is
    # only about package management, retry without clearing .olean.
    if not build["ok"] and _is_lake_package_error(build["output"]):
        build2 = _run_lake_env_lean(repo_path, name, timeout_seconds=timeout_seconds, burst_user=burst_user)
        if build2["ok"]:
            build = build2
            result.returncode = build2["returncode"]
            result.build_output = build2["output"]

    result.compiles = build["ok"]

    # 5. Scan build output for sorry warnings
    result.sorry_warnings = _extract_sorry_warnings(build["output"])
    result.sorry_free = len(result.sorry_warnings) == 0 and not has_sorry(content)

    # Override keyword_clean to also account for sorry
    if not result.sorry_free:
        result.keyword_clean = False

    return result


def _olean_path(repo_path: Path, name: str) -> Optional[Path]:
    """Compute the .olean path for a tablet node. May not exist."""
    # Lake stores .olean files under .lake/build/lib/
    # The exact path depends on the lake configuration
    # Try the common locations
    for base in [
        repo_path / ".lake" / "build" / "lib" / TABLET_DIR,
        repo_path / "build" / "lib" / TABLET_DIR,
    ]:
        path = base / f"{name}.olean"
        if path.exists():
            return path
    return None


def _run_lake_env_lean(
    repo_path: Path,
    name: str,
    *,
    timeout_seconds: float = 120.0,
    burst_user: Optional[str] = None,
) -> Dict[str, Any]:
    """Run `lake env lean Tablet/{name}.lean` and capture result."""
    rel_path = f"{TABLET_DIR}/{name}.lean"
    cmd = ["lake", "env", "lean", rel_path]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=timeout_seconds,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "output": output,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "output": f"Timed out after {timeout_seconds}s",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": None,
            "output": "lake command not found",
        }


def _is_lake_package_error(output: str) -> bool:
    """Check if a Lake build failure is only about package management, not code errors."""
    lowered = output.lower()
    # Only package management errors, not actual Lean code errors
    package_indicators = ["url has changed", "permission denied", "cloning again", "deleting"]
    code_indicators = ["type mismatch", "unknown identifier", "unexpected token", "expected", "unsolved goals", "declaration uses"]
    has_package_issue = any(p in lowered for p in package_indicators)
    has_code_issue = any(p in lowered for p in code_indicators)
    return has_package_issue and not has_code_issue


def _extract_sorry_warnings(build_output: str) -> List[str]:
    """Extract sorry-related warnings from lake/lean output."""
    warnings = []
    for line in build_output.splitlines():
        lowered = line.lower()
        if "sorry" in lowered and ("warning" in lowered or "declaration uses" in lowered):
            warnings.append(line.strip())
    return warnings


# ---------------------------------------------------------------------------
# Full tablet verification
# ---------------------------------------------------------------------------

def check_tablet(
    repo_path: Path,
    node_names: Sequence[str],
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
    declaration_hashes: Optional[Dict[str, str]] = None,
    timeout_seconds: float = 120.0,
    burst_user: Optional[str] = None,
    run_full_build: bool = True,
) -> TabletCheckResult:
    """Run verification on all tablet nodes.

    This is the same logic that check_tablet.sh runs.
    """
    if declaration_hashes is None:
        declaration_hashes = {}

    result = TabletCheckResult()

    # Check each node
    for name in node_names:
        if name == PREAMBLE_NAME or name == AXIOMS_NAME:
            continue
        node_result = check_node(
            repo_path, name,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            expected_declaration_hash=declaration_hashes.get(name, ""),
            timeout_seconds=timeout_seconds,
            burst_user=burst_user,
        )
        result.nodes[name] = node_result

    # Check name uniqueness
    # (names are the keys, so uniqueness is inherent in the dict structure)

    # Run lake build Tablet
    if run_full_build:
        build = _run_lake_build_tablet(repo_path, timeout_seconds=timeout_seconds * 3)
        result.build_ok = build["ok"]
        result.build_output = build["output"]
    else:
        result.build_ok = True  # assume ok if skipping

    return result


def _run_lake_build_tablet(
    repo_path: Path,
    *,
    timeout_seconds: float = 360.0,
) -> Dict[str, Any]:
    """Run `lake build Tablet` and capture result."""
    try:
        proc = subprocess.run(
            ["lake", "build", "Tablet"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=timeout_seconds,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "output": output,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "output": f"lake build Tablet timed out after {timeout_seconds}s",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": None,
            "output": "lake command not found",
        }


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------

def generate_check_node_sh(
    repo_path: Path,
    state_dir: Path,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
) -> str:
    """Generate the check_node.sh wrapper.

    The actual logic lives in check.py; this wrapper is only for convenience.
    """
    return f"""#!/bin/bash
# Wrapper for the shared deterministic checker.
exec python3 {shlex.quote(str(state_dir / "scripts" / "check.py"))} node "$@" {shlex.quote(str(repo_path))}
"""


def generate_check_tablet_sh(
    repo_path: Path,
    state_dir: Path,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
) -> str:
    """Generate the check_tablet.sh wrapper."""
    return f"""#!/bin/bash
# Wrapper for the shared deterministic checker.
exec python3 {shlex.quote(str(state_dir / "scripts" / "check.py"))} tablet {shlex.quote(str(repo_path))}
"""


def write_scripts(
    repo_path: Path,
    state_dir: Path,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
) -> None:
    """Install check scripts into state_dir/scripts/.

    The key script is check.py -- the SINGLE SOURCE OF TRUTH for all
    deterministic checks. Both the supervisor and the worker run this
    exact same code. The worker runs it directly:
        python3 .agent-supervisor/scripts/check.py <node_name>
    """
    scripts_dir = state_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    try:
        import grp
        gid = grp.getgrnam("leanagent").gr_gid
        os.chown(str(scripts_dir), -1, gid)
        os.chmod(str(scripts_dir), 0o2755)
    except (KeyError, PermissionError):
        pass

    # Copy check.py (the single source of truth)
    import shutil
    check_src = Path(__file__).parent / "check.py"
    check_dst = scripts_dir / "check.py"
    shutil.copy2(check_src, check_dst)
    check_dst.chmod(0o755)
    try:
        os.chown(str(check_dst), -1, gid)
    except (NameError, PermissionError):
        pass

    # Also generate shell wrappers for convenience
    check_node = scripts_dir / "check_node.sh"
    check_node.write_text(
        generate_check_node_sh(
            repo_path,
            state_dir,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
        ),
        encoding="utf-8",
    )
    check_node.chmod(0o755)
    try:
        os.chown(str(check_node), -1, gid)
    except (NameError, PermissionError):
        pass

    check_tablet = scripts_dir / "check_tablet.sh"
    check_tablet.write_text(
        generate_check_tablet_sh(repo_path, state_dir, allowed_prefixes=allowed_prefixes, forbidden_keywords=forbidden_keywords),
        encoding="utf-8",
    )
    check_tablet.chmod(0o755)
    try:
        os.chown(str(check_tablet), -1, gid)
    except (NameError, PermissionError):
        pass
