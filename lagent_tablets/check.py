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
WORKER_STATUSES: Tuple[str, ...] = ("NOT_STUCK", "STUCK", "DONE", "NEED_INPUT")
PROOF_REVIEWER_DECISIONS: Tuple[str, ...] = (
    "CONTINUE",
    "ADVANCE_PHASE",
    "STUCK",
    "NEED_INPUT",
    "DONE",
)
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


def run_print_axioms(
    repo: Path,
    name: str,
    *,
    timeout_secs: float = 120.0,
) -> Dict[str, Any]:
    """Run `#print axioms <decl>` against a temporary Lean file."""
    temp_path: Optional[Path] = None
    try:
        temp_dir = repo / ".agent-supervisor" / "staging"
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            temp_dir = Path(tempfile.gettempdir()) / "lagent-tablets-check"
            temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lean",
            dir=str(temp_dir),
            prefix=f"axioms_{name}_",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(f"import Tablet.{name}\n#print axioms {name}\n")
            temp_path = Path(handle.name)
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


def _tablet_check_known_nodes(repo: Path) -> Set[str]:
    tablet_dir = repo / "Tablet"
    names = {p.stem for p in tablet_dir.glob("*.lean")}
    names |= {p.stem for p in tablet_dir.glob("*.tex")}
    return {n for n in names if n not in {"Preamble", "Axioms", "header"}}


def _tablet_error_owner(error: str, known_nodes: Set[str]) -> Optional[str]:
    prefix = error.split(":", 1)[0].strip()
    return prefix if prefix in known_nodes else None


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
    known_nodes = _tablet_check_known_nodes(repo)
    new_relevant_errors: List[str] = []
    for err in full["errors"]:
        if err in baseline:
            continue
        owner = _tablet_error_owner(err, known_nodes)
        if owner is None or owner in allowed:
            new_relevant_errors.append(err)
    return {
        "ok": not new_relevant_errors,
        "errors": new_relevant_errors,
        "warnings": full["warnings"],
        "all_errors": full["errors"],
        "allowed_nodes": sorted(allowed),
        "build_output": full.get("build_output", ""),
    }


def validate_reviewer_decision_data(data: Any, *, phase: str) -> Dict[str, Any]:
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
    errors.extend(decision_errors + reason_errors + next_prompt_errors + next_node_errors)

    if phase == "proof_formalization":
        allowed_decisions = PROOF_REVIEWER_DECISIONS
    elif phase == "theorem_stating":
        allowed_decisions = THEOREM_REVIEWER_DECISIONS
    else:
        return {"ok": False, "errors": [f"unknown reviewer phase: {phase}"], "data": None}

    if decision and decision not in allowed_decisions:
        errors.append(f"decision must be one of {list(allowed_decisions)}")

    normalized: Dict[str, Any] = {
        "decision": decision,
        "reason": reason,
        "next_prompt": next_prompt,
        "next_active_node": next_active_node,
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
        errors.extend(diff_errors + elevate_errors)
        normalized["difficulty_assignments"] = difficulty_assignments
        normalized["elevate_to_hard"] = elevate_to_hard
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
        return validate_reviewer_decision_data(data, phase=phase)
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
    warnings: List[str] = []
    node_results: Dict[str, Dict[str, Any]] = {}
    tablet_dir = repo / "Tablet"
    preamble = tablet_dir / "Preamble.lean"

    if not tablet_dir.exists():
        return {"ok": False, "errors": [f"{tablet_dir} not found"], "warnings": [], "nodes": {}}
    if not preamble.exists():
        errors.append(f"{preamble} not found")
    else:
        preamble_content = preamble.read_text(encoding="utf-8")
        preamble_defs = scan_preamble_definitions(preamble_content)
        if preamble_defs:
            errors.append("Preamble.lean may only contain imports")
        preamble_import_violations = check_imports(preamble_content, allowed_prefixes)
        if preamble_import_violations:
            errors.append(f"Preamble has unauthorized imports: {preamble_import_violations}")

    for lean_path in sorted(tablet_dir.glob("*.lean")):
        name = lean_path.stem
        if name in ("Preamble", "Axioms"):
            continue
        if not is_valid_node_name(name):
            errors.append(f"Invalid node name: {name}")
            continue
        tex_path = tablet_dir / f"{name}.tex"
        if not tex_path.exists():
            errors.append(f"{tex_path} not found")
            continue
        lean_content = lean_path.read_text(encoding="utf-8")
        marker = extract_marker_name(lean_content)
        if marker != name:
            errors.append(f"{name}: marker says {marker!r}, expected {name!r}")
        decl_name = extract_declaration_name(lean_content)
        if decl_name != name:
            errors.append(f"{name}: declaration name is {decl_name!r}, expected {name!r}")
        tex_errors = validate_tex_format(tex_path.read_text(encoding="utf-8"))
        if tex_errors:
            errors.append(f"{name}: .tex format errors: {tex_errors}")
        node_result = check_node(
            repo,
            name,
            allowed_prefixes=allowed_prefixes,
            forbidden_keywords=forbidden_keywords,
            approved_axioms_path=approved_axioms_path,
            timeout_secs=timeout_secs,
        )
        node_results[name] = node_result
        errors.extend(f"{name}: {err}" for err in node_result["errors"])
        warnings.extend(f"{name}: {warn}" for warn in node_result["warnings"])

    build = run_lake_build_tablet(repo, timeout_secs=timeout_secs * 2)
    if not build["ok"] and not is_lake_package_error(build["output"]):
        err_lines = [line for line in build["output"].splitlines() if "error" in line.lower()]
        errors.append("lake build Tablet failed" + (f": {' | '.join(err_lines[:10])}" if err_lines else ""))
    elif not build["ok"]:
        warnings.append("lake build Tablet reported Lake package noise")

    return {
        "ok": not errors,
        "errors": errors,
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

    handoff_parser = subparsers.add_parser("worker-handoff", help="Validate a worker handoff raw JSON file")
    handoff_parser.add_argument("path")
    handoff_parser.add_argument("--phase", required=True, choices=["proof_formalization", "theorem_stating"])
    handoff_parser.add_argument("--repo", default=".")

    reviewer_parser = subparsers.add_parser("reviewer-decision", help="Validate a reviewer decision raw JSON file")
    reviewer_parser.add_argument("path")
    reviewer_parser.add_argument("--phase", required=True, choices=["proof_formalization", "theorem_stating"])

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
