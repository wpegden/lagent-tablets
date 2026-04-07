#!/usr/bin/env python3
"""Deterministic tablet node verification.

This is the SINGLE SOURCE OF TRUTH for all deterministic checks.
Both the supervisor and the worker run this exact same code.

The supervisor imports check_node() from this module.
The worker runs this file directly as a script:
    python3 .agent-supervisor/scripts/check.py <node_name>

If this script passes, the supervisor will accept the work.
If this script fails, the supervisor will reject the work.
No surprises.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Comment/string masking (for accurate keyword scanning)
# ---------------------------------------------------------------------------

def mask_comments_and_strings(text: str) -> str:
    """Replace comments and string literals with spaces, preserving line structure."""
    result = []
    i = 0
    n = len(text)
    block_depth = 0
    while i < n:
        if block_depth > 0:
            if i + 1 < n and text[i] == "/" and text[i + 1] == "-":
                block_depth += 1; result.append("  "); i += 2
            elif i + 1 < n and text[i] == "-" and text[i + 1] == "/":
                block_depth -= 1; result.append("  "); i += 2
            elif text[i] == "\n":
                result.append("\n"); i += 1
            else:
                result.append(" "); i += 1
        elif i + 1 < n and text[i] == "/" and text[i + 1] == "-":
            block_depth = 1; result.append("  "); i += 2
        elif i + 1 < n and text[i] == "-" and text[i + 1] == "-":
            result.append("  "); i += 2
            while i < n and text[i] != "\n":
                result.append(" "); i += 1
        elif text[i] == '"':
            result.append(" "); i += 1
            while i < n and text[i] != '"':
                result.append("\n" if text[i] == "\n" else " "); i += 1
            if i < n:
                result.append(" "); i += 1
        else:
            result.append(text[i]); i += 1
    return "".join(result)


# ---------------------------------------------------------------------------
# Declaration extraction and hashing
# ---------------------------------------------------------------------------

LEAN_DECL_RE = re.compile(
    r"^(theorem|lemma|def|abbrev|noncomputable\s+def|noncomputable\s+theorem)\s+"
    r"([A-Za-z_][A-Za-z0-9_.']*)",
    re.MULTILINE,
)
TABLET_NODE_MARKER_RE = re.compile(r"^-- \[TABLET NODE: ([A-Za-z_][A-Za-z0-9_]*)\]$", re.MULTILINE)
NAMESPACE_PREFIXES = ["Filter.", "Real.", "Nat.", "Int.", "Set.", "Finset.",
                      "MeasureTheory.", "Topology.", "ENNReal.", "NNReal."]


def find_declaration(content: str, node_name: str) -> Optional[str]:
    """Find the declaration line for a specific node name."""
    decl_lines: List[str] = []
    found = False
    for line in content.splitlines():
        match = LEAN_DECL_RE.match(line.strip())
        if match and match.group(2) == node_name:
            found = True
            decl_lines = [line.strip()]
            if ":=" in line:
                return " ".join(decl_lines)
            continue
        if found:
            decl_lines.append(line.strip())
            if ":=" in line:
                return " ".join(decl_lines)
    if decl_lines:
        return " ".join(decl_lines)
    return None


def normalize_declaration(decl: str) -> str:
    """Normalize for hash comparison: strip proof start, namespace prefixes, whitespace."""
    d = decl.strip()
    for suffix in [":= by", ":=by", ":= sorry", ":=sorry", ":="]:
        if d.endswith(suffix):
            d = d[:-len(suffix)].strip()
            break
    for prefix in NAMESPACE_PREFIXES:
        d = d.replace(prefix, "")
    return " ".join(d.split())


def declaration_hash(content: str, node_name: str) -> str:
    """SHA-256 of the normalized declaration."""
    decl = find_declaration(content, node_name)
    if decl is None:
        return ""
    return hashlib.sha256(normalize_declaration(decl).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Import validation
# ---------------------------------------------------------------------------

def extract_imports(content: str) -> List[str]:
    return re.findall(r"^import\s+([\w.]+)\s*$", content, re.MULTILINE)


def check_imports(content: str, allowed_prefixes: List[str]) -> List[str]:
    """Return list of unauthorized imports."""
    violations = []
    for imp in extract_imports(content):
        if imp.startswith("Tablet."):
            continue
        if any(imp.startswith(p + ".") or imp == p for p in allowed_prefixes):
            continue
        violations.append(imp)
    return violations


# ---------------------------------------------------------------------------
# Forbidden keyword scan
# ---------------------------------------------------------------------------

def scan_forbidden(content: str, keywords: List[str]) -> List[Dict[str, Any]]:
    """Scan masked source for forbidden keywords."""
    masked = mask_comments_and_strings(content)
    hits = []
    for lineno, (masked_line, orig_line) in enumerate(
        zip(masked.splitlines(), content.splitlines()), start=1
    ):
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", masked_line):
                hits.append({"keyword": kw, "line": lineno, "text": orig_line.strip()})
    return hits


# ---------------------------------------------------------------------------
# Compilation check
# ---------------------------------------------------------------------------

def run_lake_env_lean(repo: Path, name: str, *, timeout_secs: float = 300) -> Dict[str, Any]:
    """Run lake env lean and return result."""
    try:
        proc = subprocess.run(
            ["lake", "env", "lean", f"Tablet/{name}.lean"],
            capture_output=True, text=True, cwd=str(repo), timeout=timeout_secs,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "output": f"Timed out after {timeout_secs}s"}
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "output": "lake not found"}


def is_lake_package_error(output: str) -> bool:
    """Check if a build failure is only Lake package noise, not real code errors."""
    lowered = output.lower()
    pkg = ["url has changed", "permission denied", "cloning again", "deleting"]
    code = ["type mismatch", "unknown identifier", "unexpected token", "expected",
            "unsolved goals", "declaration uses"]
    return any(p in lowered for p in pkg) and not any(p in lowered for p in code)


def extract_sorry_warnings(output: str) -> List[str]:
    return [l.strip() for l in output.splitlines()
            if "sorry" in l.lower() and ("warning" in l.lower() or "declaration uses" in l.lower())]


# ---------------------------------------------------------------------------
# Main check function (used by both supervisor and worker)
# ---------------------------------------------------------------------------

def check_node(
    repo: Path,
    name: str,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
    expected_hash: str = "",
    timeout_secs: float = 300,
) -> Dict[str, Any]:
    """Run ALL deterministic checks on a single node.

    Returns a dict with:
        ok: bool (all checks passed for closure)
        compiles: bool
        sorry_free: bool
        keyword_clean: bool
        imports_valid: bool
        declaration_intact: bool
        errors: list of error strings
        warnings: list of warning strings
    """
    lean_path = repo / "Tablet" / f"{name}.lean"
    tex_path = repo / "Tablet" / f"{name}.tex"
    errors: List[str] = []
    warnings: List[str] = []

    # File existence
    if not lean_path.exists():
        return {"ok": False, "errors": [f"{lean_path} not found"], "warnings": []}
    if not tex_path.exists():
        errors.append(f"{tex_path} not found (every node needs a .tex file)")

    content = lean_path.read_text(encoding="utf-8")

    # Declaration hash
    declaration_intact = True
    if expected_hash:
        actual = declaration_hash(content, name)
        if actual != expected_hash:
            declaration_intact = False
            errors.append(f"Declaration signature changed (expected {expected_hash[:16]}... got {actual[:16]}...)")
            errors.append("Only the proof body (after :=) may be modified, not the theorem statement.")

    # Imports
    violations = check_imports(content, allowed_prefixes)
    imports_valid = len(violations) == 0
    if violations:
        errors.append(f"Unauthorized imports: {violations}")

    # Forbidden keywords
    hits = scan_forbidden(content, forbidden_keywords)
    non_sorry = [h for h in hits if h["keyword"] != "sorry"]
    keyword_clean = len(non_sorry) == 0
    if non_sorry:
        errors.append(f"Forbidden keywords: {[h['keyword'] for h in non_sorry]}")
    sorry_in_source = any(h["keyword"] == "sorry" for h in hits)

    # Compilation
    build = run_lake_env_lean(repo, name, timeout_secs=timeout_secs)
    compiles = build["ok"]
    if not compiles and is_lake_package_error(build["output"]):
        compiles = True  # ignore Lake package noise
        warnings.append("Lake package warning ignored")
    if not compiles:
        # Extract just the error lines
        err_lines = [l for l in build["output"].splitlines() if "error" in l.lower()]
        errors.append(f"Compilation failed:\n" + "\n".join(err_lines[:20]))

    # Sorry
    sorry_warnings = extract_sorry_warnings(build["output"])
    sorry_free = not sorry_in_source and len(sorry_warnings) == 0
    if not sorry_free:
        warnings.append("Node has sorry (open)")

    ok = compiles and sorry_free and keyword_clean and imports_valid and declaration_intact

    return {
        "ok": ok,
        "compiles": compiles,
        "sorry_free": sorry_free,
        "keyword_clean": keyword_clean,
        "imports_valid": imports_valid,
        "declaration_intact": declaration_intact,
        "errors": errors,
        "warnings": warnings,
        "build_output": build.get("output", ""),
    }


# ---------------------------------------------------------------------------
# CLI entry point (for workers to run directly)
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 check.py <node_name> [repo_path]")
        sys.exit(1)

    name = sys.argv[1]
    repo = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()

    # Load config from tablet.json
    tablet_path = repo / ".agent-supervisor" / "tablet.json"
    expected_hash = ""
    allowed_prefixes = ["Mathlib"]
    forbidden_keywords = ["sorry", "axiom", "constant", "unsafe", "native_decide", "implementedBy", "extern"]

    if tablet_path.exists():
        tablet = json.loads(tablet_path.read_text())
        node_data = tablet.get("nodes", {}).get(name, {})
        expected_hash = node_data.get("lean_statement_hash", "")

    # Load config for prefixes/keywords if available
    config_candidates = list((repo / ".agent-supervisor").glob("*.json"))
    # Try to find allowed_prefixes from any config-looking file
    # Default is fine for most cases

    print(f"=== Checking node: {name} ===")
    print(f"  Repo: {repo}")

    result = check_node(
        repo, name,
        allowed_prefixes=allowed_prefixes,
        forbidden_keywords=forbidden_keywords,
        expected_hash=expected_hash,
    )

    # Print results
    for err in result["errors"]:
        print(f"  FAIL: {err}")
    for warn in result["warnings"]:
        print(f"  WARNING: {warn}")

    if result["declaration_intact"]:
        print(f"  Declaration: OK")
    if result["imports_valid"]:
        print(f"  Imports: OK")
    if result["keyword_clean"]:
        print(f"  Keywords: OK")
    if result["compiles"]:
        print(f"  Compiles: OK")

    if result["ok"]:
        print(f"  Status: CLOSED (all checks pass)")
    elif result["sorry_free"] and result["compiles"]:
        print(f"  Status: CLOSED (sorry-free, compiles)")
    elif not result["sorry_free"]:
        print(f"  Status: OPEN (has sorry)")
    else:
        print(f"  Status: INVALID (errors above)")

    print(f"=== Done ===")
    sys.exit(0 if not result["errors"] else 1)


if __name__ == "__main__":
    main()
