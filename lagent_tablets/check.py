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

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from lagent_tablets.nl_cache import correspondence_fingerprint


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
DEFAULT_APPROVED_AXIOMS: Tuple[str, ...] = (
    "propext",
    "funext",
    "Classical.choice",
    "Quot.sound",
)
AXIOM_AUDIT_TEMP_PREFIX = "axioms_"
WORKER_STATUSES: Tuple[str, ...] = ("NOT_STUCK", "STUCK", "DONE", "NEED_INPUT", "CRISIS")
PROOF_REVIEWER_DECISIONS: Tuple[str, ...] = (
    "CONTINUE",
    "ADVANCE_PHASE",
    "STUCK",
    "NEED_INPUT",
    "DONE",
)
CLEANUP_REVIEWER_DECISIONS: Tuple[str, ...] = (
    "CONTINUE",
    "NEED_INPUT",
    "DONE",
)
PROOF_EDIT_MODES: Tuple[str, ...] = ("local", "restructure", "coarse_restructure")
THEOREM_REVIEWER_DECISIONS: Tuple[str, ...] = (
    "CONTINUE",
    "ADVANCE_PHASE",
    "NEED_INPUT",
)
THEOREM_TARGET_EDIT_MODES: Tuple[str, ...] = ("repair", "restructure")
CORRESPONDENCE_DECISIONS: Tuple[str, ...] = ("PASS", "FAIL")
BATCH_SOUNDNESS_DECISIONS: Tuple[str, ...] = ("PASS", "FAIL")
NODE_SOUNDNESS_DECISIONS: Tuple[str, ...] = ("SOUND", "UNSOUND", "STRUCTURAL")


def _keyword_pattern(keyword: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_']+", keyword):
        return r"\b" + re.escape(keyword) + r"\b"
    return r"(?<![A-Za-z0-9_'])" + re.escape(keyword) + r"(?![A-Za-z0-9_'])"


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


def declaration_kind(content: str, node_name: str) -> str:
    """Return a coarse declaration kind for one node name."""
    for line in content.splitlines():
        match = LEAN_DECL_RE.match(line.strip())
        if match and match.group(2) == node_name:
            raw = match.group(1).strip()
            if "def" in raw or raw == "abbrev":
                return "definition"
            return "theorem_like"
    return ""


def tex_statement_environment(tex_content: str) -> str:
    match = re.search(r"\\begin\{([A-Za-z*]+)\}", tex_content)
    if not match:
        return ""
    return match.group(1).strip().lower()


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
            if re.search(_keyword_pattern(kw), masked_line):
                hits.append({"keyword": kw, "line": lineno, "text": orig_line.strip()})
    return hits


# ---------------------------------------------------------------------------
# Closed-node axiom audit
# ---------------------------------------------------------------------------

def load_approved_axioms(
    approved_axioms_path: Optional[Path],
    node_name: str,
) -> Tuple[set[str], Optional[str]]:
    """Load globally and per-node approved axioms.

    Missing files are tolerated and fall back to a conservative built-in allowlist.
    Existing but malformed files are treated as configuration errors.
    """
    approved = set(DEFAULT_APPROVED_AXIOMS)
    if approved_axioms_path is None or not approved_axioms_path.exists():
        return approved, None

    try:
        raw = json.loads(approved_axioms_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return approved, f"Failed to load approved axioms from {approved_axioms_path}: {exc}"

    if isinstance(raw, list):
        approved.update(str(x).strip() for x in raw if str(x).strip())
        return approved, None

    if not isinstance(raw, dict):
        return approved, (
            f"Approved axioms file must be a JSON list or object: {approved_axioms_path}"
        )

    global_axioms = raw.get("global", [])
    node_axioms = (raw.get("nodes") or {}).get(node_name, [])
    if not isinstance(global_axioms, list):
        return approved, f"approved axioms 'global' must be a list: {approved_axioms_path}"
    if not isinstance(raw.get("nodes", {}), dict):
        return approved, f"approved axioms 'nodes' must be an object: {approved_axioms_path}"
    if not isinstance(node_axioms, list):
        return approved, f"approved axioms nodes.{node_name} must be a list: {approved_axioms_path}"

    approved.update(str(x).strip() for x in global_axioms if str(x).strip())
    approved.update(str(x).strip() for x in node_axioms if str(x).strip())
    return approved, None


def parse_print_axioms_output(output: str) -> Optional[List[str]]:
    normalized = " ".join(output.split())
    if "does not depend on any axioms" in normalized:
        return []
    match = re.search(r"depends on axioms:\s*\[(.*?)\]", normalized)
    if not match:
        return None
    body = match.group(1).strip()
    if not body:
        return []
    return [part.strip() for part in body.split(",") if part.strip()]


def _axiom_audit_temp_dir(repo: Path) -> Path:
    """Prefer a repo-local ignored scratch dir for axiom-audit probes."""
    temp_dir = repo / ".agent-supervisor" / "scratch" / "check"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(temp_dir, 0o2775)
        except OSError:
            pass
        return temp_dir
    except OSError:
        temp_dir = Path(tempfile.gettempdir()) / "lagent-tablets-check"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir


def cleanup_axiom_audit_temp_files(repo: Path) -> int:
    """Remove leftover repo-local axiom-audit temp files."""
    removed = 0
    for directory in (
        repo / ".agent-supervisor" / "scratch" / "check",
        repo / ".agent-supervisor" / "staging",
    ):
        if not directory.exists():
            continue
        for path in directory.glob(f"{AXIOM_AUDIT_TEMP_PREFIX}*"):
            if not path.is_file():
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def run_print_axioms(
    repo: Path,
    name: str,
    *,
    timeout_secs: float = 120.0,
) -> Dict[str, Any]:
    """Run `#print axioms <decl>` against a temporary Lean file."""
    temp_path: Optional[Path] = None
    try:
        temp_dir = _axiom_audit_temp_dir(repo)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lean",
            dir=str(temp_dir),
            prefix=f"{AXIOM_AUDIT_TEMP_PREFIX}{name}_",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(f"import Tablet.{name}\n#print axioms {name}\n")
            temp_path = Path(handle.name)
        try:
            os.chmod(temp_path, 0o664)
        except OSError:
            pass
        lean_arg = (
            os.path.relpath(temp_path, repo)
            if temp_path.is_relative_to(repo)
            else str(temp_path)
        )
        proc = subprocess.run(
            ["lake", "env", "lean", lean_arg],
            capture_output=True,
            text=True,
            cwd=str(repo),
            timeout=timeout_secs,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "output": f"Axiom audit timed out after {timeout_secs}s"}
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "output": "lake not found for axiom audit"}
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def audit_node_axioms(
    repo: Path,
    name: str,
    *,
    approved_axioms_path: Optional[Path],
    timeout_secs: float = 120.0,
) -> Dict[str, Any]:
    approved, load_error = load_approved_axioms(approved_axioms_path, name)
    if load_error:
        return {"ok": False, "axioms": [], "disallowed": [], "error": load_error}

    result = run_print_axioms(repo, name, timeout_secs=timeout_secs)
    if not result["ok"]:
        return {"ok": False, "axioms": [], "disallowed": [], "error": result["output"]}

    axioms = parse_print_axioms_output(result["output"])
    if axioms is None:
        return {
            "ok": False,
            "axioms": [],
            "disallowed": [],
            "error": f"Could not parse `#print axioms` output for {name}: {result['output'][:400]}",
        }

    disallowed = sorted(ax for ax in axioms if ax not in approved)
    return {
        "ok": len(disallowed) == 0,
        "axioms": axioms,
        "disallowed": disallowed,
        "error": "" if not disallowed else f"Unapproved axioms: {disallowed}",
    }


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
# JSON artifact validation
# ---------------------------------------------------------------------------

def _load_json_artifact(path: Path) -> Tuple[Optional[Any], List[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except FileNotFoundError:
        return None, [f"{path} not found"]
    except (json.JSONDecodeError, TypeError) as exc:
        return None, [f"{path} is not valid JSON: {exc}"]
    except OSError as exc:
        return None, [f"Could not read {path}: {exc}"]


def _expect_string(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
) -> Tuple[str, List[str]]:
    if not isinstance(value, str):
        return "", [f"{field} must be a string"]
    text = value.strip()
    if not allow_empty and not text:
        return "", [f"{field} must be non-empty"]
    return text, []


def _expect_string_list(
    value: Any,
    field: str,
    *,
    allow_empty: bool = True,
) -> Tuple[List[str], List[str]]:
    if not isinstance(value, list):
        return [], [f"{field} must be a list"]
    normalized: List[str] = []
    errors: List[str] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{field}[{i}] must be a string")
            continue
        text = item.strip()
        if not text:
            errors.append(f"{field}[{i}] must be non-empty")
            continue
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    if not allow_empty and not normalized:
        errors.append(f"{field} must be non-empty")
    return normalized, errors


def _normalize_issue_list(value: Any, field: str) -> Tuple[List[Dict[str, str]], List[str]]:
    if not isinstance(value, list):
        return [], [f"{field} must be a list"]
    normalized: List[Dict[str, str]] = []
    errors: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"{field}[{i}] must be an object")
            continue
        node, node_errors = _expect_string(item.get("node", ""), f"{field}[{i}].node")
        description, desc_errors = _expect_string(item.get("description", ""), f"{field}[{i}].description")
        errors.extend(node_errors)
        errors.extend(desc_errors)
        if node_errors or desc_errors:
            continue
        normalized.append({"node": node, "description": description})
    return normalized, errors


def _normalize_string_dict(value: Any, field: str, *, allowed_values: Optional[Sequence[str]] = None) -> Tuple[Dict[str, str], List[str]]:
    if not isinstance(value, dict):
        return {}, [f"{field} must be an object"]
    normalized: Dict[str, str] = {}
    errors: List[str] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{field} has an invalid key")
            continue
        if not isinstance(item, str):
            errors.append(f"{field}.{key} must be a string")
            continue
        text = item.strip()
        if not text:
            errors.append(f"{field}.{key} must be non-empty")
            continue
        if allowed_values is not None and text not in allowed_values:
            errors.append(f"{field}.{key} must be one of {list(allowed_values)}")
            continue
        normalized[key.strip()] = text
    return normalized, errors


def _validate_phase_block(
    value: Any,
    field: str,
) -> Tuple[Dict[str, Any], List[str]]:
    if not isinstance(value, dict):
        return {}, [f"{field} must be an object"]
    decision, errors = _expect_string(value.get("decision", ""), f"{field}.decision")
    issues, issue_errors = _normalize_issue_list(value.get("issues", []), f"{field}.issues")
    errors.extend(issue_errors)
    if decision and decision not in CORRESPONDENCE_DECISIONS:
        errors.append(f"{field}.decision must be one of {list(CORRESPONDENCE_DECISIONS)}")
    if decision == "PASS" and issues:
        errors.append(f"{field}.issues must be [] when {field}.decision is PASS")
    if decision == "FAIL" and not issues:
        errors.append(f"{field}.issues must be non-empty when {field}.decision is FAIL")
    return {"decision": decision, "issues": issues}, errors


def validate_correspondence_result_data(data: Any) -> Dict[str, Any]:
    errors: List[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["result must be a JSON object"], "data": None}
    correspondence, corr_errors = _validate_phase_block(data.get("correspondence"), "correspondence")
    paper, paper_errors = _validate_phase_block(data.get("paper_faithfulness"), "paper_faithfulness")
    summary, summary_errors = _expect_string(data.get("summary", ""), "summary")
    overall, overall_errors = _expect_string(data.get("overall", ""), "overall")
    errors.extend(corr_errors + paper_errors + summary_errors + overall_errors)
    if overall and overall not in ("APPROVE", "REJECT"):
        errors.append("overall must be one of ['APPROVE', 'REJECT']")
    expected_overall = "APPROVE" if correspondence.get("decision") == "PASS" and paper.get("decision") == "PASS" else "REJECT"
    if overall and not errors and overall != expected_overall:
        errors.append(f"overall must be {expected_overall} for the supplied phase decisions")
    normalized = {
        "correspondence": correspondence,
        "paper_faithfulness": paper,
        "overall": overall,
        "summary": summary,
    }
    return {"ok": not errors, "errors": errors, "data": normalized if not errors else None}


def validate_batch_soundness_result_data(data: Any) -> Dict[str, Any]:
    errors: List[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["result must be a JSON object"], "data": None}
    if not isinstance(data.get("soundness"), dict):
        return {"ok": False, "errors": ["soundness must be an object"], "data": None}
    soundness = data["soundness"]
    decision, decision_errors = _expect_string(soundness.get("decision", ""), "soundness.decision")
    issues, issue_errors = _normalize_issue_list(soundness.get("issues", []), "soundness.issues")
    summary, summary_errors = _expect_string(data.get("summary", ""), "summary")
    overall, overall_errors = _expect_string(data.get("overall", ""), "overall")
    errors.extend(decision_errors + issue_errors + summary_errors + overall_errors)
    if decision and decision not in BATCH_SOUNDNESS_DECISIONS:
        errors.append(f"soundness.decision must be one of {list(BATCH_SOUNDNESS_DECISIONS)}")
    if decision == "PASS" and issues:
        errors.append("soundness.issues must be [] when soundness.decision is PASS")
    if decision == "FAIL" and not issues:
        errors.append("soundness.issues must be non-empty when soundness.decision is FAIL")
    if overall and overall not in ("APPROVE", "REJECT"):
        errors.append("overall must be one of ['APPROVE', 'REJECT']")
    expected_overall = "APPROVE" if decision == "PASS" else "REJECT"
    if overall and not errors and overall != expected_overall:
        errors.append(f"overall must be {expected_overall} for the supplied soundness decision")
    normalized = {
        "soundness": {"decision": decision, "issues": issues},
        "overall": overall,
        "summary": summary,
    }
    return {"ok": not errors, "errors": errors, "data": normalized if not errors else None}


def validate_node_soundness_result_data(data: Any, *, node_name: str) -> Dict[str, Any]:
    errors: List[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["result must be a JSON object"], "data": None}
    node, node_errors = _expect_string(data.get("node", ""), "node")
    summary, summary_errors = _expect_string(data.get("summary", ""), "summary")
    overall, overall_errors = _expect_string(data.get("overall", ""), "overall")
    errors.extend(node_errors + summary_errors + overall_errors)
    if node and node != node_name:
        errors.append(f"node must equal {node_name}")
    if not isinstance(data.get("soundness"), dict):
        errors.append("soundness must be an object")
        soundness_block = {}
    else:
        soundness_block = data["soundness"]
    decision, decision_errors = _expect_string(soundness_block.get("decision", ""), "soundness.decision")
    explanation, explanation_errors = _expect_string(soundness_block.get("explanation", ""), "soundness.explanation")
    errors.extend(decision_errors + explanation_errors)
    if decision and decision not in NODE_SOUNDNESS_DECISIONS:
        errors.append(f"soundness.decision must be one of {list(NODE_SOUNDNESS_DECISIONS)}")
    if overall and overall not in ("APPROVE", "REJECT"):
        errors.append("overall must be one of ['APPROVE', 'REJECT']")
    expected_overall = "APPROVE" if decision == "SOUND" else "REJECT"
    if overall and decision and overall != expected_overall:
        errors.append(f"overall must be {expected_overall} when soundness.decision is {decision}")
    normalized = {
        "node": node,
        "soundness": {
            "decision": decision,
            "explanation": explanation,
        },
        "overall": overall,
        "summary": summary,
    }
    return {"ok": not errors, "errors": errors, "data": normalized if not errors else None}


def validate_worker_handoff_data(data: Any, *, phase: str, repo: Optional[Path] = None) -> Dict[str, Any]:
    errors: List[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["result must be a JSON object"], "data": None}
    summary, summary_errors = _expect_string(data.get("summary", ""), "summary")
    status, status_errors = _expect_string(data.get("status", ""), "status")
    new_nodes, new_nodes_errors = _expect_string_list(data.get("new_nodes", []), "new_nodes")
    errors.extend(summary_errors + status_errors + new_nodes_errors)
    if status and status not in WORKER_STATUSES:
        errors.append(f"status must be one of {list(WORKER_STATUSES)}")
    if phase != "theorem_stating" and status == "CRISIS":
        errors.append("status CRISIS is only allowed in theorem_stating")
    normalized: Dict[str, Any] = {
        "summary": summary,
        "status": status,
        "new_nodes": new_nodes,
    }
    if phase == "theorem_stating":
        difficulty_hints, diff_errors = _normalize_string_dict(
            data.get("difficulty_hints", {}),
            "difficulty_hints",
            allowed_values=("easy", "hard"),
        )
        kind_hints, kind_errors = _normalize_string_dict(
            data.get("kind_hints", {}),
            "kind_hints",
            allowed_values=("paper_main_result", "paper_intermediate"),
        )
        errors.extend(diff_errors)
        errors.extend(kind_errors)
        extra_hint_nodes = sorted(set(difficulty_hints) - set(new_nodes))
        if extra_hint_nodes:
            errors.append(
                "difficulty_hints keys must be listed in new_nodes "
                f"(only genuinely new nodes may be hinted): {extra_hint_nodes}"
            )
        extra_kind_nodes = sorted(set(kind_hints) - set(new_nodes))
        if extra_kind_nodes:
            errors.append(
                "kind_hints keys must be listed in new_nodes "
                f"(only genuinely new nodes may be classified): {extra_kind_nodes}"
            )
        normalized["difficulty_hints"] = difficulty_hints
        normalized["kind_hints"] = kind_hints
    if repo is not None:
        for name in new_nodes:
            lean_path = repo / "Tablet" / f"{name}.lean"
            tex_path = repo / "Tablet" / f"{name}.tex"
            if not lean_path.exists():
                errors.append(f"new_nodes entry {name} is missing {lean_path}")
            if not tex_path.exists():
                errors.append(f"new_nodes entry {name} is missing {tex_path}")
    return {"ok": not errors, "errors": errors, "data": normalized if not errors else None}


def _snapshot_tablet_dir(repo: Path) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    tablet_dir = repo / "Tablet"
    if not tablet_dir.exists():
        return snapshot
    for path in sorted(tablet_dir.iterdir()):
        if path.is_file():
            snapshot[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _detect_snapshot_changes(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, List[str]]:
    all_names = set(before) | set(after)
    created: List[str] = []
    modified: List[str] = []
    deleted: List[str] = []
    for name in sorted(all_names):
        if name not in before:
            created.append(name)
        elif name not in after:
            deleted.append(name)
        elif before[name] != after[name]:
            modified.append(name)
    return {"created": created, "modified": modified, "deleted": deleted}


def _append_error(
    errors: List[str],
    error_records: List[Dict[str, Any]],
    message: str,
    *,
    owner: Optional[str] = None,
) -> None:
    errors.append(message)
    error_records.append({"message": message, "owner": owner})


def _legacy_tablet_check_known_nodes(repo: Path) -> Set[str]:
    tablet_dir = repo / "Tablet"
    names = {p.stem for p in tablet_dir.glob("*.lean")}
    names |= {p.stem for p in tablet_dir.glob("*.tex")}
    return {n for n in names if n not in {"Preamble", "Axioms", "header"}}


def _legacy_tablet_error_owner(error: str, known_nodes: Set[str]) -> Optional[str]:
    prefix = error.split(":", 1)[0].strip()
    return prefix if prefix in known_nodes else None


def _current_tablet_node_names(repo: Path) -> Set[str]:
    tablet_dir = repo / "Tablet"
    if not tablet_dir.exists():
        return set()
    lean_files = {p.stem for p in tablet_dir.glob("*.lean") if p.stem != "Preamble"}
    tex_files = {p.stem for p in tablet_dir.glob("*.tex") if p.stem not in ("header", "Preamble")}
    return lean_files | tex_files


def _tablet_node_file_hash(repo: Path, name: str) -> str:
    from lagent_tablets.tablet import node_lean_path, node_tex_path

    h = hashlib.sha256()
    lean_path = node_lean_path(repo, name)
    tex_path = node_tex_path(repo, name)
    h.update(lean_path.read_bytes() if lean_path.exists() else b"")
    h.update(b"\0")
    h.update(tex_path.read_bytes() if tex_path.exists() else b"")
    return h.hexdigest()


def changed_tablet_nodes_since_snapshot(repo: Path, before_hashes: Dict[str, str]) -> List[str]:
    current_names = _current_tablet_node_names(repo)
    all_names = set(before_hashes) | current_names
    changed: List[str] = []
    for name in sorted(all_names):
        current_hash = _tablet_node_file_hash(repo, name) if name in current_names else ""
        if before_hashes.get(name, "") != current_hash:
            changed.append(name)
    return changed


def snapshot_tablet_node_hashes(repo: Path) -> Dict[str, str]:
    return {
        name: _tablet_node_file_hash(repo, name)
        for name in sorted(_current_tablet_node_names(repo))
    }


def check_proof_easy_scope(
    repo: Path,
    *,
    active_node: str,
    snapshot_before: Dict[str, str],
    imports_before: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Check easy-mode proof scope constraints against a pre-burst snapshot."""
    from lagent_tablets.tablet import extract_imports, node_lean_path

    after = _snapshot_tablet_dir(repo)
    changes = _detect_snapshot_changes(snapshot_before, after)
    active_lean = f"{active_node}.lean"
    errors: List[str] = []
    warnings: List[str] = []

    if changes["deleted"]:
        errors.append(f"Easy mode does not allow deleting files: {changes['deleted']}")

    created_content_files = [
        fname for fname in changes["created"]
        if fname.endswith(".lean") or fname.endswith(".tex")
    ]
    if created_content_files:
        errors.append(
            f"Easy mode only allows editing `{active_lean}`. Created: {sorted(created_content_files)}"
        )

    unexpected_modified = [fname for fname in changes["modified"] if fname != active_lean]
    if unexpected_modified:
        errors.append(
            f"Easy mode only allows editing `{active_lean}`. Modified: {sorted(unexpected_modified)}"
        )

    if imports_before is not None:
        lean_path = node_lean_path(repo, active_node)
        if lean_path.exists():
            imports_after = extract_imports(lean_path.read_text(encoding="utf-8"))
            if list(imports_after) != list(imports_before):
                errors.append(
                    "Easy-mode node cannot change imports. Only the active `.lean` proof body may change."
                )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "changes": changes,
        "created_content_files": created_content_files,
    }


def _load_tablet_state_for_checks(repo: Path) -> Any:
    from lagent_tablets.state import TabletState

    tablet_path = repo / ".agent-supervisor" / "tablet.json"
    if not tablet_path.exists():
        return TabletState()
    try:
        raw = json.loads(tablet_path.read_text(encoding="utf-8"))
    except Exception:
        return TabletState()
    return TabletState.from_dict(raw)


def check_coarse_package_guard(
    repo: Path,
    *,
    active_node: str,
    changes: Dict[str, List[str]],
    proof_edit_mode: str = "local",
) -> Dict[str, Any]:
    """Reject ordinary proof-mode edits that mutate the accepted coarse package."""
    if proof_edit_mode == "coarse_restructure":
        return {"ok": True, "errors": [], "warnings": []}

    from lagent_tablets.tablet import coarse_interface_fingerprint, coarse_node_names

    tablet = _load_tablet_state_for_checks(repo)
    coarse_names = coarse_node_names(tablet)
    if not coarse_names:
        return {"ok": True, "errors": [], "warnings": []}

    def _node_name(fname: str) -> str:
        return Path(fname).stem

    errors: List[str] = []
    changed_lean_nodes = {
        _node_name(fname)
        for fname in changes.get("modified", [])
        if fname.endswith(".lean")
    }
    changed_tex_nodes = {
        _node_name(fname)
        for fname in changes.get("modified", [])
        if fname.endswith(".tex")
    }
    deleted_nodes = {
        _node_name(fname)
        for fname in changes.get("deleted", [])
        if fname.endswith(".lean") or fname.endswith(".tex")
    }

    deleted_coarse = sorted(name for name in deleted_nodes if name in coarse_names)
    if deleted_coarse:
        errors.append(
            f"Accepted coarse nodes may not be deleted outside `coarse_restructure`: {deleted_coarse}"
        )

    changed_coarse_tex = sorted(name for name in changed_tex_nodes if name in coarse_names)
    if changed_coarse_tex:
        errors.append(
            f"Accepted coarse nodes may not change their `.tex` files outside `coarse_restructure`: {changed_coarse_tex}"
        )

    changed_other_coarse = sorted(
        name for name in changed_lean_nodes
        if name in coarse_names and name != active_node
    )
    if changed_other_coarse:
        errors.append(
            "Accepted coarse nodes outside the active node may not be modified in ordinary proof mode: "
            f"{changed_other_coarse}"
        )

    if active_node in coarse_names and active_node in changed_lean_nodes:
        node = tablet.nodes.get(active_node)
        expected = node.coarse_content_hash if node is not None else ""
        current = coarse_interface_fingerprint(
            tablet,
            repo,
            active_node,
            coarse_names=coarse_names,
        )
        if not expected:
            errors.append(
                f"Accepted coarse fingerprint missing for `{active_node}`. "
                "Use `coarse_restructure` if the coarse package must be re-established."
            )
        elif current != expected:
            errors.append(
                f"`{active_node}` changed the accepted coarse package interface. "
                "Use `proof_edit_mode: coarse_restructure` for coarse package changes."
            )

    return {"ok": not errors, "errors": errors, "warnings": []}


def check_proof_hard_scope(
    repo: Path,
    *,
    active_node: str,
    snapshot_before: Dict[str, str],
    proof_edit_mode: str = "local",
    authorized_nodes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Check proof-formalization hard-mode scope constraints."""
    from lagent_tablets.tablet import compute_target_impact_region

    after = _snapshot_tablet_dir(repo)
    changes = _detect_snapshot_changes(snapshot_before, after)
    supervisor_generated = {"INDEX.md", "README.md", "header.tex", "Tablet.lean"}
    allowed_modified = {"Preamble.lean"} | supervisor_generated
    if proof_edit_mode in {"restructure", "coarse_restructure"}:
        allowed_nodes = set(str(name) for name in (authorized_nodes or []))
        allowed_nodes |= compute_target_impact_region(repo, active_node)
        for name in allowed_nodes:
            allowed_modified.add(f"{name}.lean")
            allowed_modified.add(f"{name}.tex")
    else:
        allowed_modified |= {f"{active_node}.lean", f"{active_node}.tex"}
    errors: List[str] = []

    if changes["deleted"]:
        errors.append(f"Files were deleted (not allowed): {changes['deleted']}")

    unexpected_modified = [f for f in changes["modified"] if f not in allowed_modified]
    if unexpected_modified:
        if proof_edit_mode in {"restructure", "coarse_restructure"}:
            errors.append(
                f"Unexpected files modified: {unexpected_modified}. "
                f"Only nodes in `{active_node}`'s authorized impact region, Preamble.lean, "
                "and supervisor-generated support files may be modified."
            )
        else:
            errors.append(
                f"Unexpected files modified: {unexpected_modified}. "
                f"Only {active_node}, Preamble.lean, and supervisor-generated support files may be modified."
            )

    coarse_result = check_coarse_package_guard(
        repo,
        active_node=active_node,
        changes=changes,
        proof_edit_mode=proof_edit_mode,
    )
    errors.extend(coarse_result["errors"])

    return {"ok": not errors, "errors": errors, "warnings": [], "changes": changes}


def check_proof_worker_delta(
    repo: Path,
    *,
    active_node: str,
    snapshot_before: Dict[str, str],
    existing_nodes: Sequence[str],
    expected_active_hash: str = "",
    proof_edit_mode: str = "local",
    authorized_nodes: Optional[Sequence[str]] = None,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
    approved_axioms_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Canonical post-burst proof-worker validation for touched/new nodes.

    This is the artifact-creating deterministic gate shared by workers and the
    supervisor for proof_formalization. It validates the active node and any
    newly created helper nodes, then summarizes created/closed progress.
    """
    from lagent_tablets.tablet import has_sorry, node_lean_path, node_tex_path

    changes = _detect_snapshot_changes(snapshot_before, _snapshot_tablet_dir(repo))
    active_lean = f"{active_node}.lean"
    active_changed = active_lean in changes["modified"]
    new_lean_files = [
        fname.removesuffix(".lean")
        for fname in changes["created"]
        if fname.endswith(".lean")
    ]
    new_tex_files = [
        fname.removesuffix(".tex")
        for fname in changes["created"]
        if fname.endswith(".tex") and fname not in {"header.tex", "Preamble.tex"}
    ]
    existing = set(str(name) for name in existing_nodes)
    allowed_existing_nodes = set(str(name) for name in (authorized_nodes or []))

    stray_new_tex = sorted(name for name in new_tex_files if name not in set(new_lean_files))
    if stray_new_tex:
        return {
            "ok": False,
            "outcome": "INVALID",
            "detail": f"New .tex files without matching .lean files: {stray_new_tex}",
            "nodes_closed": [],
            "nodes_created": [],
            "build_output": "",
            "errors": [f"Unpaired .tex files created: {stray_new_tex}"],
            "warnings": [],
        }

    coarse_result = check_coarse_package_guard(
        repo,
        active_node=active_node,
        changes=changes,
        proof_edit_mode=proof_edit_mode,
    )
    if coarse_result["errors"]:
        return {
            "ok": False,
            "outcome": "INVALID",
            "detail": coarse_result["errors"][0],
            "nodes_closed": [],
            "nodes_created": [],
            "build_output": "",
            "errors": list(coarse_result["errors"]),
            "warnings": list(coarse_result.get("warnings", [])),
        }

    extra_changed_nodes = sorted(
        {
            Path(fname).stem
            for fname in changes["modified"]
            if fname.endswith(".lean")
            and Path(fname).stem not in {active_node, "Preamble", "Axioms"}
            and Path(fname).stem in existing
            and proof_edit_mode in {"restructure", "coarse_restructure"}
            and Path(fname).stem in allowed_existing_nodes
        }
    )

    if not active_changed and not new_lean_files and not extra_changed_nodes:
        return {
            "ok": True,
            "outcome": "NO_PROGRESS",
            "detail": "No files were changed.",
            "nodes_closed": [],
            "nodes_created": [],
            "build_output": "",
            "errors": [],
            "warnings": [],
        }

    nodes_closed: List[str] = []
    nodes_created: List[str] = []

    if active_changed:
        result = check_node(
            repo,
            active_node,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            expected_hash="" if proof_edit_mode == "coarse_restructure" else expected_active_hash,
            approved_axioms_path=approved_axioms_path,
        )
        if result["errors"]:
            return {
                "ok": False,
                "outcome": "INVALID",
                "detail": result["errors"][0],
                "nodes_closed": [],
                "nodes_created": [],
                "build_output": result.get("build_output", ""),
                "errors": list(result["errors"]),
                "warnings": list(result.get("warnings", [])),
            }
        if result["ok"]:
            nodes_closed.append(active_node)

    for name in extra_changed_nodes:
        result = check_node(
            repo,
            name,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            approved_axioms_path=approved_axioms_path,
        )
        if result["errors"]:
            return {
                "ok": False,
                "outcome": "INVALID",
                "detail": f"{name}: {result['errors'][0]}",
                "nodes_closed": nodes_closed,
                "nodes_created": nodes_created,
                "build_output": result.get("build_output", ""),
                "errors": [f"{name}: {err}" for err in result["errors"]],
                "warnings": list(result.get("warnings", [])),
            }
        if result["ok"] and name not in nodes_closed:
            nodes_closed.append(name)

    for name in new_lean_files:
        if name in ("Preamble", "Axioms") or name in existing:
            continue
        if not is_valid_node_name(name):
            return {
                "ok": False,
                "outcome": "INVALID",
                "detail": f"Invalid node name: {name!r}",
                "nodes_closed": [],
                "nodes_created": [],
                "build_output": "",
                "errors": [f"Invalid node name: {name!r}"],
                "warnings": [],
            }
        tex_path = node_tex_path(repo, name)
        if not tex_path.exists():
            return {
                "ok": False,
                "outcome": "INVALID",
                "detail": f"New node {name} has .lean but no .tex file",
                "nodes_closed": [],
                "nodes_created": [],
                "build_output": "",
                "errors": [f"New node {name} has .lean but no .tex file"],
                "warnings": [],
            }
        result = check_node(
            repo,
            name,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            approved_axioms_path=approved_axioms_path,
        )
        if result["errors"]:
            return {
                "ok": False,
                "outcome": "INVALID",
                "detail": f"New node {name}: {result['errors'][0]}",
                "nodes_closed": [],
                "nodes_created": [],
                "build_output": result.get("build_output", ""),
                "errors": [f"New node {name}: {err}" for err in result["errors"]],
                "warnings": list(result.get("warnings", [])),
            }

        if not has_sorry(node_lean_path(repo, name).read_text(encoding="utf-8")):
            nodes_closed.append(name)
        nodes_created.append(name)

    parts: List[str] = []
    if nodes_closed:
        parts.append(f"closed: {nodes_closed}")
    if nodes_created:
        parts.append(f"created: {nodes_created}")
    if extra_changed_nodes:
        parts.append(f"updated existing: {extra_changed_nodes}")
    if active_changed and active_node not in nodes_closed:
        parts.append(f"{active_node} modified (still open)")

    if not parts:
        return {
            "ok": True,
            "outcome": "NO_PROGRESS",
            "detail": "No meaningful changes detected.",
            "nodes_closed": [],
            "nodes_created": [],
            "build_output": "",
            "errors": [],
            "warnings": [],
        }

    return {
        "ok": True,
        "outcome": "PROGRESS",
        "detail": "; ".join(parts),
        "nodes_closed": nodes_closed,
        "nodes_created": nodes_created,
        "build_output": "",
        "errors": [],
        "warnings": [],
    }


def check_theorem_target_repair_scope(
    repo: Path,
    *,
    target: str,
    snapshot_before: Dict[str, str],
) -> Dict[str, Any]:
    """Check target-repair theorem-stating scope constraints."""
    after = _snapshot_tablet_dir(repo)
    changes = _detect_snapshot_changes(snapshot_before, after)
    target_tex = f"{target}.tex"
    supervisor_generated = {"INDEX.md", "README.md", "header.tex", "Tablet.lean"}
    errors: List[str] = []

    if changes["deleted"]:
        errors.append(f"Theorem target repair mode does not allow deleting files: {changes['deleted']}")

    created_content_files = [
        fname for fname in changes["created"]
        if fname.endswith(".lean") or fname.endswith(".tex")
    ]
    if created_content_files:
        errors.append(
            "Theorem target repair mode does not allow creating new node files. "
            f"Created: {sorted(created_content_files)}"
        )

    unexpected_modified = [
        fname for fname in changes["modified"]
        if fname not in ({target_tex} | supervisor_generated)
    ]
    if unexpected_modified:
        errors.append(
            f"Theorem target repair mode only allows editing `{target_tex}`. "
            f"Modified: {sorted(unexpected_modified)}"
        )

    return {"ok": not errors, "errors": errors, "warnings": [], "changes": changes}


def check_theorem_target_edit_scope(
    repo: Path,
    *,
    target: str,
    before_hashes: Dict[str, str],
    initial_scope: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Check target-restructure theorem-stating impact-region constraints."""
    from lagent_tablets.tablet import compute_target_impact_region

    if not target:
        return {"ok": True, "errors": [], "warnings": [], "changed_nodes": [], "allowed_nodes": []}

    final_names = _current_tablet_node_names(repo)
    if target not in final_names:
        return {
            "ok": False,
            "errors": [f"Current soundness target `{target}` must remain present in the tablet."],
            "warnings": [],
            "changed_nodes": [],
            "allowed_nodes": [],
        }

    changed_nodes = changed_tablet_nodes_since_snapshot(repo, before_hashes)
    if not changed_nodes:
        return {"ok": True, "errors": [], "warnings": [], "changed_nodes": [], "allowed_nodes": []}

    before_scope = set(str(name) for name in (initial_scope or []))
    after_scope = compute_target_impact_region(repo, target)
    allowed = before_scope | after_scope
    out_of_scope = [name for name in changed_nodes if name not in allowed]
    if not out_of_scope:
        return {
            "ok": True,
            "errors": [],
            "warnings": [],
            "changed_nodes": changed_nodes,
            "allowed_nodes": sorted(allowed),
        }

    allowed_preview = ", ".join(sorted(allowed)[:12])
    if len(allowed) > 12:
        allowed_preview += ", ..."
    return {
        "ok": False,
        "errors": [
            f"Out-of-scope theorem-stating edits for target `{target}`: "
            f"{', '.join(out_of_scope)}. "
            "When a current soundness target is set, changes must stay within that target's "
            "authorized impact region (target, prerequisites, and downstream consumers, "
            "before or after the cycle). "
            f"Allowed scope: {allowed_preview or '(empty)'}.",
        ],
        "warnings": [],
        "changed_nodes": changed_nodes,
        "allowed_nodes": sorted(allowed),
    }


def check_tablet_scoped(
    repo: Path,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
    baseline_errors: Sequence[str],
    allowed_nodes: Sequence[str],
    approved_axioms_path: Optional[Path] = None,
    timeout_secs: float = 300,
) -> Dict[str, Any]:
    """Run tablet checks, but only fail on newly introduced relevant errors.

    Relevant means either:
    - a node-level error on one of the allowed nodes, or
    - any new global error (for example a fresh build failure).
    """
    baseline = set(str(err) for err in baseline_errors)
    allowed = set(str(name) for name in allowed_nodes)
    full = check_tablet(
        repo,
        allowed_prefixes=allowed_prefixes,
        forbidden_keywords=forbidden_keywords,
        approved_axioms_path=approved_axioms_path,
        timeout_secs=timeout_secs,
    )
    error_records = full.get("error_records", [])
    if not error_records:
        known_nodes = _legacy_tablet_check_known_nodes(repo)
        error_records = [
            {"message": str(err), "owner": _legacy_tablet_error_owner(str(err), known_nodes)}
            for err in full.get("errors", [])
        ]
    new_relevant_errors: List[str] = []
    for record in error_records:
        if not isinstance(record, dict):
            continue
        message = str(record.get("message", "") or "")
        if not message or message in baseline:
            continue
        owner = record.get("owner")
        owner_name = str(owner).strip() if owner is not None else None
        if owner_name is None or owner_name in allowed:
            new_relevant_errors.append(message)
    return {
        "ok": not new_relevant_errors,
        "errors": new_relevant_errors,
        "warnings": full["warnings"],
        "all_errors": full["errors"],
        "error_records": error_records,
        "allowed_nodes": sorted(allowed),
        "build_output": full.get("build_output", ""),
    }


def check_cleanup_preserving(
    repo: Path,
    *,
    snapshot_before: Dict[str, str],
    baseline_declaration_hashes: Dict[str, str],
    baseline_correspondence_hashes: Dict[str, str],
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
    approved_axioms_path: Optional[Path] = None,
    timeout_secs: float = 300,
) -> Dict[str, Any]:
    """Validate a style-cleanup cycle is semantics-preserving.

    Cleanup is intentionally strict:
    - no new or deleted Tablet files
    - no `.tex` edits
    - any changed `.lean` node must preserve declaration hash and correspondence fingerprint
    - the full tablet must still pass canonical deterministic checks
    """
    after = _snapshot_tablet_dir(repo)
    changes = _detect_snapshot_changes(snapshot_before, after)
    errors: List[str] = []
    warnings: List[str] = []

    if changes["created"]:
        errors.append(f"cleanup may not create Tablet files: {changes['created']}")
    if changes["deleted"]:
        errors.append(f"cleanup may not delete Tablet files: {changes['deleted']}")

    tex_modified = [name for name in changes["modified"] if name.endswith(".tex")]
    if tex_modified:
        errors.append(f"cleanup may not modify .tex files: {tex_modified}")

    changed_nodes = sorted(
        {
            Path(name).stem
            for name in changes["modified"]
            if name.endswith(".lean") and Path(name).stem not in {"Preamble", "Axioms"}
        }
    )

    full = check_tablet(
        repo,
        allowed_prefixes=allowed_prefixes,
        forbidden_keywords=forbidden_keywords,
        approved_axioms_path=approved_axioms_path,
        timeout_secs=timeout_secs,
    )
    errors.extend(full["errors"])
    warnings.extend(full["warnings"])

    for node_name in changed_nodes:
        baseline_decl = str(baseline_declaration_hashes.get(node_name, "") or "")
        current_decl = declaration_hash((repo / "Tablet" / f"{node_name}.lean").read_text(encoding="utf-8"), node_name)
        if baseline_decl and current_decl != baseline_decl:
            errors.append(f"{node_name}: declaration hash changed during cleanup")

        baseline_corr = str(baseline_correspondence_hashes.get(node_name, "") or "")
        current_corr = correspondence_fingerprint(repo, node_name)
        if baseline_corr and current_corr != baseline_corr:
            errors.append(f"{node_name}: correspondence fingerprint changed during cleanup")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "changes": changes,
        "changed_nodes": changed_nodes,
        "build_output": full.get("build_output", ""),
    }


def validate_reviewer_decision_data(data: Any, *, phase: str, invalid_attempt: bool = False) -> Dict[str, Any]:
    from lagent_tablets.state import (
        normalize_open_blockers,
        normalize_orphan_resolutions,
        normalize_paper_focus_ranges,
    )

    errors: List[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["result must be a JSON object"], "data": None}

    decision, decision_errors = _expect_string(data.get("decision", ""), "decision")
    reason, reason_errors = _expect_string(data.get("reason", ""), "reason")
    next_prompt, next_prompt_errors = _expect_string(data.get("next_prompt", ""), "next_prompt", allow_empty=True)
    next_active_node, next_node_errors = _expect_string(data.get("next_active_node", ""), "next_active_node", allow_empty=True)
    reset_to_checkpoint, reset_errors = _expect_string(
        data.get("reset_to_checkpoint", ""),
        "reset_to_checkpoint",
        allow_empty=True,
    )
    errors.extend(decision_errors + reason_errors + next_prompt_errors + next_node_errors + reset_errors)

    if phase == "proof_formalization":
        allowed_decisions = PROOF_REVIEWER_DECISIONS
    elif phase == "proof_complete_style_cleanup":
        allowed_decisions = CLEANUP_REVIEWER_DECISIONS
    elif phase == "theorem_stating":
        allowed_decisions = ("CONTINUE", "NEED_INPUT") if invalid_attempt else THEOREM_REVIEWER_DECISIONS
    else:
        return {"ok": False, "errors": [f"unknown reviewer phase: {phase}"], "data": None}

    if decision and decision not in allowed_decisions:
        errors.append(f"decision must be one of {list(allowed_decisions)}")

    normalized: Dict[str, Any] = {
        "decision": decision,
        "reason": reason,
        "next_prompt": next_prompt,
        "next_active_node": next_active_node,
        "reset_to_checkpoint": reset_to_checkpoint,
    }

    paper_focus_ranges = normalize_paper_focus_ranges(data.get("paper_focus_ranges", []))
    if data.get("paper_focus_ranges", []) != [] and not paper_focus_ranges:
        errors.append("paper_focus_ranges must be a list of {start_line, end_line, reason}")
    normalized["paper_focus_ranges"] = paper_focus_ranges

    if phase == "proof_formalization":
        difficulty_assignments, diff_errors = _normalize_string_dict(
            data.get("difficulty_assignments", {}),
            "difficulty_assignments",
            allowed_values=("easy", "hard"),
        )
        elevate_to_hard, elevate_errors = _expect_string_list(
            data.get("elevate_to_hard", []),
            "elevate_to_hard",
        )
        proof_edit_mode, proof_edit_mode_errors = _expect_string(
            data.get("proof_edit_mode", "local"),
            "proof_edit_mode",
        )
        errors.extend(diff_errors + elevate_errors)
        errors.extend(proof_edit_mode_errors)
        if proof_edit_mode and proof_edit_mode not in PROOF_EDIT_MODES:
            errors.append(f"proof_edit_mode must be one of {list(PROOF_EDIT_MODES)}")
        normalized["difficulty_assignments"] = difficulty_assignments
        normalized["elevate_to_hard"] = elevate_to_hard
        normalized["proof_edit_mode"] = proof_edit_mode or "local"
    elif phase == "proof_complete_style_cleanup":
        normalized["difficulty_assignments"] = {}
        normalized["elevate_to_hard"] = []
        normalized["proof_edit_mode"] = "local"
    else:
        issues, issues_errors = _expect_string_list(data.get("issues", []), "issues")
        kind_assignments, kind_errors = _normalize_string_dict(
            data.get("kind_assignments", {}),
            "kind_assignments",
            allowed_values=("paper_main_result", "paper_intermediate"),
        )
        errors.extend(issues_errors)
        errors.extend(kind_errors)
        target_edit_mode, target_edit_mode_errors = _expect_string(
            data.get("target_edit_mode", "repair"),
            "target_edit_mode",
        )
        errors.extend(target_edit_mode_errors)
        if target_edit_mode and target_edit_mode not in THEOREM_TARGET_EDIT_MODES:
            errors.append(f"target_edit_mode must be one of {list(THEOREM_TARGET_EDIT_MODES)}")
        orphan_resolutions = normalize_orphan_resolutions(data.get("orphan_resolutions", []))
        if data.get("orphan_resolutions", []) != [] and not orphan_resolutions:
            errors.append("orphan_resolutions must be a list of valid orphan-resolution objects")
        raw_open_blockers = data.get("open_blockers", data.get("open_rejections", []))
        open_blockers = normalize_open_blockers(raw_open_blockers)
        if raw_open_blockers != [] and not open_blockers:
            errors.append("open_blockers must be a list of valid blocker objects")
        normalized["issues"] = issues
        normalized["kind_assignments"] = kind_assignments
        normalized["target_edit_mode"] = target_edit_mode or "repair"
        normalized["orphan_resolutions"] = orphan_resolutions
        normalized["open_blockers"] = open_blockers
        normalized["open_rejections"] = open_blockers

    if reset_to_checkpoint and phase != "theorem_stating":
        errors.append("reset_to_checkpoint is only allowed in theorem_stating")
    if phase == "theorem_stating" and reset_to_checkpoint and decision != "CONTINUE":
        errors.append("reset_to_checkpoint is only allowed when decision is CONTINUE")

    if phase == "theorem_stating" and invalid_attempt and next_active_node:
        errors.append("next_active_node must be empty on INVALID theorem_stating attempts")
    if phase == "theorem_stating" and decision == "ADVANCE_PHASE" and not next_active_node:
        errors.append("next_active_node is required when decision is ADVANCE_PHASE")

    return {"ok": not errors, "errors": errors, "data": normalized if not errors else None}


def validate_json_artifact(
    kind: str,
    path: Path,
    *,
    phase: Optional[str] = None,
    node_name: Optional[str] = None,
    repo: Optional[Path] = None,
    invalid_attempt: bool = False,
) -> Dict[str, Any]:
    data, load_errors = _load_json_artifact(path)
    if load_errors:
        return {"ok": False, "errors": load_errors, "data": None}
    assert data is not None
    if kind == "worker-handoff":
        if phase is None:
            return {"ok": False, "errors": ["phase is required for worker-handoff"], "data": None}
        return validate_worker_handoff_data(data, phase=phase, repo=repo)
    if kind == "reviewer-decision":
        if phase is None:
            return {"ok": False, "errors": ["phase is required for reviewer-decision"], "data": None}
        return validate_reviewer_decision_data(data, phase=phase, invalid_attempt=invalid_attempt)
    if kind == "correspondence-result":
        return validate_correspondence_result_data(data)
    if kind == "soundness-result":
        if node_name is None:
            return {"ok": False, "errors": ["node_name is required for soundness-result"], "data": None}
        return validate_node_soundness_result_data(data, node_name=node_name)
    if kind == "soundness-batch-result":
        return validate_batch_soundness_result_data(data)
    return {"ok": False, "errors": [f"unknown artifact kind: {kind}"], "data": None}


# ---------------------------------------------------------------------------
# Whole-tablet deterministic check
# ---------------------------------------------------------------------------

def run_lake_build_tablet(repo: Path, *, timeout_secs: float = 600.0) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["lake", "build", "Tablet"],
            capture_output=True,
            text=True,
            cwd=str(repo),
            timeout=timeout_secs,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "output": f"Timed out after {timeout_secs}s"}
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "output": "lake not found"}


def check_tablet(
    repo: Path,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
    approved_axioms_path: Optional[Path] = None,
    timeout_secs: float = 300,
) -> Dict[str, Any]:
    from lagent_tablets.tablet import (
        extract_declaration_name,
        extract_marker_name,
        is_valid_node_name,
        scan_preamble_definitions,
        validate_tex_format,
    )

    errors: List[str] = []
    error_records: List[Dict[str, Any]] = []
    warnings: List[str] = []
    node_results: Dict[str, Dict[str, Any]] = {}
    tablet_dir = repo / "Tablet"
    preamble = tablet_dir / "Preamble.lean"

    if not tablet_dir.exists():
        return {
            "ok": False,
            "errors": [f"{tablet_dir} not found"],
            "error_records": [{"message": f"{tablet_dir} not found", "owner": None}],
            "warnings": [],
            "nodes": {},
        }
    if not preamble.exists():
        _append_error(errors, error_records, f"{preamble} not found")
    else:
        preamble_content = preamble.read_text(encoding="utf-8")
        preamble_defs = scan_preamble_definitions(preamble_content)
        if preamble_defs:
            _append_error(errors, error_records, "Preamble.lean may only contain imports")
        preamble_import_violations = check_imports(preamble_content, allowed_prefixes)
        if preamble_import_violations:
            _append_error(
                errors,
                error_records,
                f"Preamble has unauthorized imports: {preamble_import_violations}",
            )
    preamble_tex = tablet_dir / "Preamble.tex"
    if preamble_tex.exists():
        tex_errors = validate_tex_format(preamble_tex.read_text(encoding="utf-8"), is_preamble=True)
        if tex_errors:
            _append_error(errors, error_records, f"Preamble: .tex format errors: {tex_errors}")

    for lean_path in sorted(tablet_dir.glob("*.lean")):
        name = lean_path.stem
        if name in ("Preamble", "Axioms"):
            continue
        if not is_valid_node_name(name):
            _append_error(errors, error_records, f"Invalid node name: {name}", owner=name)
            continue
        tex_path = tablet_dir / f"{name}.tex"
        if not tex_path.exists():
            _append_error(errors, error_records, f"{tex_path} not found", owner=name)
            continue
        lean_content = lean_path.read_text(encoding="utf-8")
        marker = extract_marker_name(lean_content)
        if marker != name:
            _append_error(errors, error_records, f"{name}: marker says {marker!r}, expected {name!r}", owner=name)
        decl_name = extract_declaration_name(lean_content)
        if decl_name != name:
            _append_error(errors, error_records, f"{name}: declaration name is {decl_name!r}, expected {name!r}", owner=name)
        tex_errors = validate_tex_format(tex_path.read_text(encoding="utf-8"))
        if tex_errors:
            _append_error(errors, error_records, f"{name}: .tex format errors: {tex_errors}", owner=name)
        node_result = check_node(
            repo,
            name,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            approved_axioms_path=approved_axioms_path,
            timeout_secs=timeout_secs,
        )
        node_results[name] = node_result
        for err in node_result["errors"]:
            _append_error(errors, error_records, f"{name}: {err}", owner=name)
        warnings.extend(f"{name}: {warn}" for warn in node_result["warnings"])

    for tex_path in sorted(tablet_dir.glob("*.tex")):
        name = tex_path.stem
        if name in ("header", "Preamble"):
            continue
        lean_path = tablet_dir / f"{name}.lean"
        if not lean_path.exists():
            _append_error(
                errors,
                error_records,
                f"{lean_path} not found (every .tex node needs a matching .lean file)",
            )

    build = run_lake_build_tablet(repo, timeout_secs=timeout_secs * 2)
    if not build["ok"] and not is_lake_package_error(build["output"]):
        err_lines = [line for line in build["output"].splitlines() if "error" in line.lower()]
        _append_error(
            errors,
            error_records,
            "lake build Tablet failed" + (f": {' | '.join(err_lines[:10])}" if err_lines else ""),
        )
    elif not build["ok"]:
        warnings.append("lake build Tablet reported Lake package noise")

    return {
        "ok": not errors,
        "errors": errors,
        "error_records": error_records,
        "warnings": warnings,
        "nodes": node_results,
        "build_output": build.get("output", ""),
    }


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
    expected_declaration_hash: str = "",
    approved_axioms_path: Optional[Path] = None,
    timeout_secs: float = 300,
    timeout_seconds: Optional[float] = None,
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
    from lagent_tablets.tablet import (
        extract_declaration_name,
        extract_marker_name,
        validate_tex_format,
    )

    lean_path = repo / "Tablet" / f"{name}.lean"
    tex_path = repo / "Tablet" / f"{name}.tex"
    errors: List[str] = []
    warnings: List[str] = []
    import_violations: List[str] = []
    forbidden_hits: List[Dict[str, Any]] = []
    sorry_warnings: List[str] = []
    marker_valid = True
    declaration_name_matches = True
    tex_format_valid = True
    declaration_role_warning = ""

    # File existence
    if not lean_path.exists():
        return {"ok": False, "errors": [f"{lean_path} not found"], "warnings": []}
    tex_content = ""
    if not tex_path.exists():
        errors.append(f"{tex_path} not found (every node needs a .tex file)")
        tex_format_valid = False
    else:
        tex_content = tex_path.read_text(encoding="utf-8")

    content = lean_path.read_text(encoding="utf-8")

    if timeout_seconds is not None:
        timeout_secs = timeout_seconds

    if not expected_hash and expected_declaration_hash:
        expected_hash = expected_declaration_hash

    # Declaration hash
    declaration_intact = True
    if expected_hash:
        actual = declaration_hash(content, name)
        if actual != expected_hash:
            declaration_intact = False
            errors.append(f"Declaration signature changed (expected {expected_hash[:16]}... got {actual[:16]}...)")
            errors.append("Only the proof body (after :=) may be modified, not the theorem statement.")

    # Imports
    import_violations = check_imports(content, allowed_prefixes)
    imports_valid = len(import_violations) == 0
    if import_violations:
        errors.append(f"Unauthorized imports: {import_violations}")

    # Marker / declaration / tex structure
    marker = extract_marker_name(content)
    if marker != name:
        marker_valid = False
        errors.append(f"Marker says {marker!r}, expected {name!r}")

    decl_name = extract_declaration_name(content)
    if decl_name != name:
        declaration_name_matches = False
        errors.append(f"Declaration name is {decl_name!r}, expected {name!r}")

    if tex_path.exists():
        tex_errors = validate_tex_format(tex_content)
        if tex_errors:
            tex_format_valid = False
            errors.append(f".tex format errors: {tex_errors}")
        else:
            decl_kind = declaration_kind(content, name)
            tex_env = tex_statement_environment(tex_content)
            if decl_kind == "definition" and tex_env and tex_env != "definition":
                declaration_role_warning = (
                    f"Lean declaration is a definition but .tex uses {tex_env}; "
                    "paper-facing concepts should be modeled as definition nodes."
                )
            elif decl_kind == "theorem_like" and tex_env == "definition":
                declaration_role_warning = (
                    "The .tex statement uses definition but the Lean declaration is theorem-like; "
                    "do not use theorem/lemma nodes as disguised definitions."
                )
            if declaration_role_warning:
                warnings.append(declaration_role_warning)

    # Forbidden keywords
    forbidden_hits = scan_forbidden(content, forbidden_keywords)
    non_sorry = [h for h in forbidden_hits if h["keyword"] != "sorry"]
    keyword_clean = len(non_sorry) == 0
    if non_sorry:
        errors.append(f"Forbidden keywords: {[h['keyword'] for h in non_sorry]}")
    sorry_in_source = any(h["keyword"] == "sorry" for h in forbidden_hits)

    # Sorry in definitions (always forbidden, even when sorry is allowed in theorems)
    from lagent_tablets.tablet import scan_sorry_in_definitions
    def_sorry_hits = scan_sorry_in_definitions(content)
    if def_sorry_hits:
        keyword_clean = False
        for h in def_sorry_hits:
            errors.append(f"sorry in definition at line {h['line']}: {h['text']}")

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

    axioms_valid = True
    axiom_violations: List[str] = []
    audited_axioms: List[str] = []
    if (
        compiles
        and sorry_free
        and keyword_clean
        and imports_valid
        and declaration_intact
        and marker_valid
        and declaration_name_matches
        and tex_format_valid
    ):
        axiom_audit = audit_node_axioms(
            repo,
            name,
            approved_axioms_path=approved_axioms_path,
            timeout_secs=min(timeout_secs, 120.0),
        )
        audited_axioms = list(axiom_audit.get("axioms", []))
        if not axiom_audit["ok"]:
            axioms_valid = False
            axiom_violations = list(axiom_audit.get("disallowed", []))
            errors.append(f"Axiom audit failed: {axiom_audit['error']}")

    ok = (
        compiles
        and sorry_free
        and keyword_clean
        and imports_valid
        and declaration_intact
        and marker_valid
        and declaration_name_matches
        and tex_format_valid
        and axioms_valid
    )

    return {
        "ok": ok,
        "compiles": compiles,
        "sorry_free": sorry_free,
        "keyword_clean": keyword_clean,
        "imports_valid": imports_valid,
        "declaration_intact": declaration_intact,
        "marker_valid": marker_valid,
        "declaration_name_matches": declaration_name_matches,
        "tex_format_valid": tex_format_valid,
        "axioms_valid": axioms_valid,
        "audited_axioms": audited_axioms,
        "axiom_violations": axiom_violations,
        "import_violations": import_violations,
        "forbidden_hits": forbidden_hits,
        "sorry_warnings": sorry_warnings,
        "errors": errors,
        "warnings": warnings,
        "build_output": build.get("output", ""),
    }


# ---------------------------------------------------------------------------
# CLI entry point (shared by workers, reviewers, and verifiers)
# ---------------------------------------------------------------------------

def _default_check_context(repo: Path) -> Tuple[List[str], List[str], Optional[Path]]:
    from lagent_tablets.config import FORBIDDEN_KEYWORDS_DEFAULT

    allowed_prefixes = ["Mathlib"]
    forbidden_keywords = list(FORBIDDEN_KEYWORDS_DEFAULT)
    approved_axioms_path = repo / "APPROVED_AXIOMS.json"
    return allowed_prefixes, forbidden_keywords, approved_axioms_path


def _expected_hash_from_tablet(repo: Path, node_name: str) -> str:
    tablet_path = repo / ".agent-supervisor" / "tablet.json"
    if not tablet_path.exists():
        return ""
    try:
        tablet = json.loads(tablet_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    node_data = tablet.get("nodes", {}).get(node_name, {})
    if not isinstance(node_data, dict):
        return ""
    return str(node_data.get("lean_statement_hash", ""))


def generate_check_node_sh(
    repo_path: Path,
    state_dir: Path,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
) -> str:
    """Generate the check_node.sh wrapper.

    The actual logic lives in this module; this wrapper is only for convenience.
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
    """Install deterministic checker scripts into state_dir/scripts/.

    The key script is check.py -- the single source of truth used by both the
    supervisor and workers. The shell wrappers are conveniences only.
    """
    scripts_dir = state_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    gid: Optional[int] = None
    try:
        import grp

        gid = grp.getgrnam("leanagent").gr_gid
        os.chown(str(scripts_dir), -1, gid)
        os.chmod(str(scripts_dir), 0o2755)
    except (ImportError, KeyError, PermissionError):
        gid = None

    check_src = Path(__file__)
    check_dst = scripts_dir / "check.py"
    source_root = str(Path(__file__).resolve().parent.parent)
    check_text = check_src.read_text(encoding="utf-8")
    bootstrap = (
        "import sys as _sys\n"
        f"_src_root = {source_root!r}\n"
        "if _src_root not in _sys.path:\n"
        "    _sys.path.insert(0, _src_root)\n\n"
    )
    shebang = "#!/usr/bin/env python3\n"
    if check_text.startswith(shebang):
        body = check_text[len(shebang):]
        prefix = shebang
    else:
        body = check_text
        prefix = ""
    future_line = "from __future__ import annotations\n"
    if future_line in body:
        body = body.replace(future_line, future_line + "\n" + bootstrap, 1)
    else:
        body = bootstrap + body
    check_dst.write_text(prefix + body, encoding="utf-8")
    shutil.copystat(check_src, check_dst)
    check_dst.chmod(0o755)
    if gid is not None:
        try:
            os.chown(str(check_dst), -1, gid)
        except PermissionError:
            pass

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
    if gid is not None:
        try:
            os.chown(str(check_node), -1, gid)
        except PermissionError:
            pass

    check_tablet = scripts_dir / "check_tablet.sh"
    check_tablet.write_text(
        generate_check_tablet_sh(
            repo_path,
            state_dir,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
        ),
        encoding="utf-8",
    )
    check_tablet.chmod(0o755)
    if gid is not None:
        try:
            os.chown(str(check_tablet), -1, gid)
        except PermissionError:
            pass


def _print_json_validation_result(result: Dict[str, Any], *, path: Path) -> int:
    if result["ok"]:
        print(f"OK: {path}")
        return 0
    for err in result["errors"]:
        print(f"FAIL: {err}")
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_args = list(argv if argv is not None else sys.argv[1:])
    command_names = {
        "node",
        "tablet",
        "tablet-scoped",
        "cleanup-preserving",
        "proof-easy-scope",
        "proof-hard-scope",
        "proof-worker-delta",
        "theorem-target-repair-scope",
        "theorem-target-edit-scope",
        "worker-handoff",
        "reviewer-decision",
        "correspondence-result",
        "soundness-result",
        "soundness-batch-result",
    }
    # Backward compatibility: `python3 check.py <node_name> [repo]`
    if raw_args and raw_args[0] not in command_names and not raw_args[0].startswith("-"):
        raw_args = ["node", *raw_args]

    parser = argparse.ArgumentParser(description="Deterministic lagent-tablets checker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    node_parser = subparsers.add_parser("node", help="Check one proof-formalization node")
    node_parser.add_argument("node_name")
    node_parser.add_argument("repo_path", nargs="?", default=".")

    tablet_parser = subparsers.add_parser("tablet", help="Check the whole tablet structure")
    tablet_parser.add_argument("repo_path", nargs="?", default=".")

    tablet_scoped_parser = subparsers.add_parser(
        "tablet-scoped",
        help="Check the tablet, failing only on newly introduced relevant errors",
    )
    tablet_scoped_parser.add_argument("repo_path", nargs="?", default=".")
    tablet_scoped_parser.add_argument("--scope-json", required=True)

    cleanup_parser = subparsers.add_parser(
        "cleanup-preserving",
        help="Check that cleanup edits are semantics-preserving",
    )
    cleanup_parser.add_argument("repo_path", nargs="?", default=".")
    cleanup_parser.add_argument("--scope-json", required=True)

    easy_scope_parser = subparsers.add_parser(
        "proof-easy-scope",
        help="Check easy-mode proof worker scope constraints",
    )
    easy_scope_parser.add_argument("repo_path", nargs="?", default=".")
    easy_scope_parser.add_argument("--scope-json", required=True)

    hard_scope_parser = subparsers.add_parser(
        "proof-hard-scope",
        help="Check hard-mode proof worker scope constraints",
    )
    hard_scope_parser.add_argument("repo_path", nargs="?", default=".")
    hard_scope_parser.add_argument("--scope-json", required=True)

    proof_delta_parser = subparsers.add_parser(
        "proof-worker-delta",
        help="Check active-node/new-node deterministic proof worker validation",
    )
    proof_delta_parser.add_argument("repo_path", nargs="?", default=".")
    proof_delta_parser.add_argument("--scope-json", required=True)

    theorem_repair_parser = subparsers.add_parser(
        "theorem-target-repair-scope",
        help="Check theorem-stating target-repair scope constraints",
    )
    theorem_repair_parser.add_argument("repo_path", nargs="?", default=".")
    theorem_repair_parser.add_argument("--scope-json", required=True)

    theorem_edit_parser = subparsers.add_parser(
        "theorem-target-edit-scope",
        help="Check theorem-stating target impact-region scope constraints",
    )
    theorem_edit_parser.add_argument("repo_path", nargs="?", default=".")
    theorem_edit_parser.add_argument("--scope-json", required=True)

    handoff_parser = subparsers.add_parser("worker-handoff", help="Validate a worker handoff raw JSON file")
    handoff_parser.add_argument("path")
    handoff_parser.add_argument("--phase", required=True, choices=["proof_formalization", "proof_complete_style_cleanup", "theorem_stating"])
    handoff_parser.add_argument("--repo", default=".")

    reviewer_parser = subparsers.add_parser("reviewer-decision", help="Validate a reviewer decision raw JSON file")
    reviewer_parser.add_argument("path")
    reviewer_parser.add_argument("--phase", required=True, choices=["proof_formalization", "proof_complete_style_cleanup", "theorem_stating"])

    corr_parser = subparsers.add_parser("correspondence-result", help="Validate a correspondence raw JSON file")
    corr_parser.add_argument("path")

    snd_parser = subparsers.add_parser("soundness-result", help="Validate a per-node soundness raw JSON file")
    snd_parser.add_argument("path")
    snd_parser.add_argument("--node", required=True)

    snd_batch_parser = subparsers.add_parser("soundness-batch-result", help="Validate a batch soundness raw JSON file")
    snd_batch_parser.add_argument("path")

    args = parser.parse_args(raw_args)

    if args.command == "node":
        repo = Path(args.repo_path).resolve()
        name = args.node_name
        allowed_prefixes, forbidden_keywords, approved_axioms_path = _default_check_context(repo)
        expected_hash = _expected_hash_from_tablet(repo, name)

        print(f"=== Checking node: {name} ===")
        print(f"  Repo: {repo}")
        result = check_node(
            repo,
            name,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            expected_hash=expected_hash,
            approved_axioms_path=approved_axioms_path,
        )
        for err in result["errors"]:
            print(f"  FAIL: {err}")
        for warn in result["warnings"]:
            print(f"  WARNING: {warn}")
        if result["declaration_intact"]:
            print("  Declaration: OK")
        if result["imports_valid"]:
            print("  Imports: OK")
        if result["keyword_clean"]:
            print("  Keywords: OK")
        if result["compiles"]:
            print("  Compiles: OK")
        if result["ok"]:
            print("  Status: CLOSED (all checks pass)")
        elif result["sorry_free"] and result["compiles"]:
            print("  Status: CLOSED (sorry-free, compiles)")
        elif not result["sorry_free"]:
            print("  Status: OPEN (has sorry)")
        else:
            print("  Status: INVALID (errors above)")
        print("=== Done ===")
        return 0 if not result["errors"] else 1

    if args.command == "tablet":
        repo = Path(args.repo_path).resolve()
        allowed_prefixes, forbidden_keywords, approved_axioms_path = _default_check_context(repo)
        print(f"=== Checking tablet: {repo} ===")
        result = check_tablet(
            repo,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            approved_axioms_path=approved_axioms_path,
        )
        for err in result["errors"]:
            print(f"FAIL: {err}")
        for warn in result["warnings"]:
            print(f"WARNING: {warn}")
        if result["ok"]:
            print("OK: tablet passes deterministic checks")
            return 0
        return 1

    if args.command == "tablet-scoped":
        repo = Path(args.repo_path).resolve()
        scope_path = Path(args.scope_json).resolve()
        allowed_prefixes, forbidden_keywords, approved_axioms_path = _default_check_context(repo)
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"FAIL: could not read scope json {scope_path}: {exc}")
            return 1
        baseline_errors = scope.get("baseline_errors", [])
        allowed_nodes = set(scope.get("allowed_nodes", []))
        target_name = str(scope.get("target", "") or "").strip()
        if target_name:
            from lagent_tablets.tablet import compute_target_impact_region
            allowed_nodes |= compute_target_impact_region(repo, target_name)
        print(f"=== Checking tablet (scoped): {repo} ===")
        result = check_tablet_scoped(
            repo,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            baseline_errors=baseline_errors,
            allowed_nodes=sorted(allowed_nodes),
            approved_axioms_path=approved_axioms_path,
        )
        for err in result["errors"]:
            print(f"FAIL: {err}")
        for warn in result["warnings"]:
            print(f"WARNING: {warn}")
        if result["ok"]:
            print("OK: no new relevant deterministic errors in scoped region")
            return 0
        return 1

    if args.command == "cleanup-preserving":
        repo = Path(args.repo_path).resolve()
        scope_path = Path(args.scope_json).resolve()
        allowed_prefixes, forbidden_keywords, approved_axioms_path = _default_check_context(repo)
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"FAIL: could not read scope json {scope_path}: {exc}")
            return 1
        print(f"=== Checking cleanup preservation: {repo} ===")
        result = check_cleanup_preserving(
            repo,
            snapshot_before=dict(scope.get("snapshot_before", {})),
            baseline_declaration_hashes=dict(scope.get("baseline_declaration_hashes", {})),
            baseline_correspondence_hashes=dict(scope.get("baseline_correspondence_hashes", {})),
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            approved_axioms_path=approved_axioms_path,
        )
        for err in result["errors"]:
            print(f"FAIL: {err}")
        for warn in result["warnings"]:
            print(f"WARNING: {warn}")
        if result["ok"]:
            print("OK: cleanup edits preserved tablet semantics")
            return 0
        return 1

    if args.command == "proof-easy-scope":
        repo = Path(args.repo_path).resolve()
        scope_path = Path(args.scope_json).resolve()
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"FAIL: could not read scope json {scope_path}: {exc}")
            return 1
        result = check_proof_easy_scope(
            repo,
            active_node=str(scope.get("active_node", "") or "").strip(),
            snapshot_before=dict(scope.get("snapshot_before", {})),
            imports_before=list(scope.get("imports_before", [])),
        )
        for err in result["errors"]:
            print(f"FAIL: {err}")
        if result["ok"]:
            print("OK: easy-mode proof scope respected")
            return 0
        return 1

    if args.command == "proof-hard-scope":
        repo = Path(args.repo_path).resolve()
        scope_path = Path(args.scope_json).resolve()
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"FAIL: could not read scope json {scope_path}: {exc}")
            return 1
        result = check_proof_hard_scope(
            repo,
            active_node=str(scope.get("active_node", "") or "").strip(),
            snapshot_before=dict(scope.get("snapshot_before", {})),
            proof_edit_mode=str(scope.get("proof_edit_mode", "local") or "local"),
            authorized_nodes=list(scope.get("authorized_nodes", [])),
        )
        for err in result["errors"]:
            print(f"FAIL: {err}")
        if result["ok"]:
            print("OK: hard-mode proof scope respected")
            return 0
        return 1

    if args.command == "proof-worker-delta":
        repo = Path(args.repo_path).resolve()
        scope_path = Path(args.scope_json).resolve()
        allowed_prefixes, forbidden_keywords, approved_axioms_path = _default_check_context(repo)
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"FAIL: could not read scope json {scope_path}: {exc}")
            return 1
        result = check_proof_worker_delta(
            repo,
            active_node=str(scope.get("active_node", "") or "").strip(),
            snapshot_before=dict(scope.get("snapshot_before", {})),
            existing_nodes=list(scope.get("existing_nodes", [])),
            expected_active_hash=str(scope.get("expected_active_hash", "") or ""),
            proof_edit_mode=str(scope.get("proof_edit_mode", "local") or "local"),
            authorized_nodes=list(scope.get("authorized_nodes", [])),
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            approved_axioms_path=approved_axioms_path,
        )
        for err in result["errors"]:
            print(f"FAIL: {err}")
        for warn in result.get("warnings", []):
            print(f"WARNING: {warn}")
        if result["ok"]:
            print(f"OK: {result['outcome']} -- {result['detail']}")
            return 0
        return 1

    if args.command == "theorem-target-repair-scope":
        repo = Path(args.repo_path).resolve()
        scope_path = Path(args.scope_json).resolve()
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"FAIL: could not read scope json {scope_path}: {exc}")
            return 1
        result = check_theorem_target_repair_scope(
            repo,
            target=str(scope.get("target", "") or "").strip(),
            snapshot_before=dict(scope.get("snapshot_before", {})),
        )
        for err in result["errors"]:
            print(f"FAIL: {err}")
        if result["ok"]:
            print("OK: theorem target repair scope respected")
            return 0
        return 1

    if args.command == "theorem-target-edit-scope":
        repo = Path(args.repo_path).resolve()
        scope_path = Path(args.scope_json).resolve()
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"FAIL: could not read scope json {scope_path}: {exc}")
            return 1
        result = check_theorem_target_edit_scope(
            repo,
            target=str(scope.get("target", "") or "").strip(),
            before_hashes=dict(scope.get("before_hashes", {})),
            initial_scope=list(scope.get("initial_scope", [])),
        )
        for err in result["errors"]:
            print(f"FAIL: {err}")
        if result["ok"]:
            print("OK: theorem target edit scope respected")
            return 0
        return 1

    if args.command == "worker-handoff":
        path = Path(args.path)
        repo = Path(args.repo).resolve()
        return _print_json_validation_result(
            validate_json_artifact("worker-handoff", path, phase=args.phase, repo=repo),
            path=path,
        )

    if args.command == "reviewer-decision":
        path = Path(args.path)
        return _print_json_validation_result(
            validate_json_artifact("reviewer-decision", path, phase=args.phase),
            path=path,
        )

    if args.command == "correspondence-result":
        path = Path(args.path)
        return _print_json_validation_result(
            validate_json_artifact("correspondence-result", path),
            path=path,
        )

    if args.command == "soundness-result":
        path = Path(args.path)
        return _print_json_validation_result(
            validate_json_artifact("soundness-result", path, node_name=args.node),
            path=path,
        )

    if args.command == "soundness-batch-result":
        path = Path(args.path)
        return _print_json_validation_result(
            validate_json_artifact("soundness-batch-result", path),
            path=path,
        )

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
