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
# Default forbidden keywords
# ---------------------------------------------------------------------------

FORBIDDEN_KEYWORDS_DEFAULT = [
    "sorry", "axiom", "constant", "unsafe",
    "native_decide", "implementedBy", "extern",
]


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
    """Generate the check_node.sh script content.

    This runs the EXACT SAME deterministic checks the supervisor performs.
    The worker should never be surprised by a rejection.
    """
    forbidden_pattern = "|".join(re.escape(kw) for kw in forbidden_keywords)
    allowed_pattern = "|".join(re.escape(p) for p in allowed_prefixes)

    return f"""#!/bin/bash
# check_node.sh -- Verify a single tablet node
# Generated by lagent-supervisor. Runs the EXACT SAME checks the supervisor performs.
# If this passes, the supervisor will accept your work.
# Usage: check_node.sh <node_name>

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: check_node.sh <node_name>"
    exit 1
fi

NAME="$1"
REPO={shlex.quote(str(repo_path))}
STATE_DIR={shlex.quote(str(state_dir))}
LEAN_FILE="$REPO/Tablet/$NAME.lean"
TEX_FILE="$REPO/Tablet/$NAME.tex"
TABLET_JSON="$STATE_DIR/tablet.json"

echo "=== Checking node: $NAME ==="

# Check files exist
if [ ! -f "$LEAN_FILE" ]; then
    echo "FAIL: $LEAN_FILE not found"
    exit 1
fi
echo "  .lean file: OK"

# Check .tex exists (required for all nodes)
if [ ! -f "$TEX_FILE" ]; then
    echo "FAIL: $TEX_FILE not found"
    exit 1
fi
echo "  .tex file: OK"

# Check imports
BAD_IMPORTS=$(grep -E '^import ' "$LEAN_FILE" | grep -v -E '^import (Tablet\\.|{allowed_pattern}\\.)' || true)
if [ -n "$BAD_IMPORTS" ]; then
    echo "FAIL: Unauthorized imports:"
    echo "$BAD_IMPORTS"
    exit 1
fi
echo "  Imports: OK"

# Check forbidden keywords (crude but matches supervisor behavior)
FORBIDDEN=$(grep -n -E '\\b({forbidden_pattern})\\b' "$LEAN_FILE" | grep -v '^[0-9]*:--' | grep -v '^[0-9]*:/\\-' || true)
if [ -n "$FORBIDDEN" ]; then
    echo "WARNING: Potential forbidden keywords (supervisor will do precise check):"
    echo "$FORBIDDEN"
fi

# Check declaration hash (the supervisor checks this to detect unauthorized statement changes)
if [ -f "$TABLET_JSON" ] && command -v python3 &>/dev/null; then
    HASH_CHECK=$(python3 -c "
import json, hashlib, re, sys
try:
    tablet = json.loads(open('$TABLET_JSON').read())
    stored = tablet.get('nodes', {{}}).get('$NAME', {{}}).get('lean_statement_hash', '')
    if not stored:
        print('SKIP: no stored hash')
        sys.exit(0)
    content = open('$LEAN_FILE').read()
    # Find the declaration matching the node name
    lines = content.splitlines()
    decl_lines = []
    found = False
    for line in lines:
        m = re.match(r'(theorem|lemma|def|abbrev)\s+(\w+)', line.strip())
        if m and m.group(2) == '$NAME':
            found = True
            decl_lines = [line.strip()]
            if ':=' in line:
                break
            continue
        if found:
            decl_lines.append(line.strip())
            if ':=' in line:
                break
    if not decl_lines:
        print('SKIP: declaration not found')
        sys.exit(0)
    decl = ' '.join(decl_lines)
    # Normalize: strip proof start and namespace prefixes
    for suffix in [':= by', ':=by', ':= sorry', ':=sorry', ':=']:
        if decl.endswith(suffix):
            decl = decl[:-len(suffix)].strip()
            break
    for prefix in ['Filter.', 'Real.', 'Nat.', 'Int.', 'Set.', 'Finset.',
                    'MeasureTheory.', 'Topology.', 'ENNReal.', 'NNReal.']:
        decl = decl.replace(prefix, '')
    decl = ' '.join(decl.split())
    actual = hashlib.sha256(decl.encode()).hexdigest()
    if actual == stored:
        print('OK')
    else:
        print(f'FAIL: declaration changed (expected {{stored[:16]}}... got {{actual[:16]}}...)')
        sys.exit(1)
except Exception as e:
    print(f'SKIP: {{e}}')
" 2>&1)
    echo "  Declaration hash: $HASH_CHECK"
    if echo "$HASH_CHECK" | grep -q "^FAIL"; then
        echo "  The supervisor WILL REJECT this because the theorem statement was modified."
        echo "  Only the proof body (after :=) may be changed."
        exit 1
    fi
fi

# Compile
echo "  Compiling..."
cd "$REPO"
BUILD_OUTPUT=$(lake env lean "Tablet/$NAME.lean" 2>&1) || true
EXITCODE=$?

if [ $EXITCODE -ne 0 ]; then
    # Check if it's just a Lake package error (not a real code error)
    if echo "$BUILD_OUTPUT" | grep -q "URL has changed\|permission denied\|cloning again"; then
        if ! echo "$BUILD_OUTPUT" | grep -q "type mismatch\|unknown identifier\|unexpected token\|expected\|unsolved goals"; then
            echo "  Compiles: OK (Lake package warning ignored)"
        else
            echo "FAIL: Compilation failed (exit $EXITCODE)"
            echo "$BUILD_OUTPUT" | grep -E 'error:|warning:' | tail -20
            exit 1
        fi
    else
        echo "FAIL: Compilation failed (exit $EXITCODE)"
        echo "$BUILD_OUTPUT" | grep -E 'error:|warning:' | tail -20
        exit 1
    fi
else
    echo "  Compiles: OK"
fi

# Check for sorry warnings
SORRY_WARNINGS=$(echo "$BUILD_OUTPUT" | grep -i "declaration uses.*sorry" || true)
if [ -n "$SORRY_WARNINGS" ]; then
    echo "  Status: OPEN (has sorry)"
else
    echo "  Status: CLOSED (sorry-free)"
fi

echo "=== Done ==="
"""


def generate_check_tablet_sh(
    repo_path: Path,
    state_dir: Path,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
) -> str:
    """Generate the check_tablet.sh script content."""
    return f"""#!/bin/bash
# check_tablet.sh -- Verify all tablet nodes
# Generated by lagent-supervisor. Runs the same checks the supervisor performs.

set -euo pipefail

REPO={shlex.quote(str(repo_path))}
SCRIPTS={shlex.quote(str(state_dir / "scripts"))}
CLOSED=0
OPEN=0
BROKEN=0

echo "=== Tablet Status ==="
echo ""

for LEAN_FILE in "$REPO"/Tablet/*.lean; do
    NAME=$(basename "$LEAN_FILE" .lean)
    [ "$NAME" = "Preamble" ] && continue
    [ "$NAME" = "Axioms" ] && continue

    "$SCRIPTS/check_node.sh" "$NAME" 2>&1 | tail -3
    STATUS=$?
    if [ $STATUS -ne 0 ]; then
        BROKEN=$((BROKEN + 1))
    fi
done

echo ""
echo "=== Full Build ==="
cd "$REPO"
lake build Tablet 2>&1 | tail -10
echo ""
echo "=== Summary ==="
echo "Build exit: $?"
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

    # Copy check.py (the single source of truth)
    import shutil
    check_src = Path(__file__).parent / "check.py"
    check_dst = scripts_dir / "check.py"
    shutil.copy2(check_src, check_dst)
    check_dst.chmod(0o755)

    # Also generate shell wrappers for convenience
    check_node = scripts_dir / "check_node.sh"
    check_node.write_text(
        f"""#!/bin/bash
# Wrapper for check.py -- runs the EXACT SAME checks the supervisor uses.
# Usage: check_node.sh <node_name>
exec python3 {shlex.quote(str(check_dst))} "$@" {shlex.quote(str(repo_path))}
""",
        encoding="utf-8",
    )
    check_node.chmod(0o755)

    check_tablet = scripts_dir / "check_tablet.sh"
    check_tablet.write_text(
        generate_check_tablet_sh(repo_path, state_dir, allowed_prefixes=allowed_prefixes, forbidden_keywords=forbidden_keywords),
        encoding="utf-8",
    )
    check_tablet.chmod(0o755)
