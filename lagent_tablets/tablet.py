"""Tablet operations: node creation, .lean/.tex file generation, INDEX/README generation.

The tablet is a DAG of nodes where Lean imports define the dependency structure.
This module handles the file-level operations; Lean/Lake handles the graph logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from lagent_tablets.state import (
    TabletNode,
    TabletState,
    format_paper_provenance,
    normalize_paper_provenance,
    save_tablet,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TABLET_DIR = "Tablet"
PREAMBLE_NAME = "Preamble"
AXIOMS_NAME = "Axioms"
TABLET_NODE_MARKER = "-- [TABLET NODE: {}]"
TABLET_NODE_MARKER_RE = re.compile(r"^-- \[TABLET NODE: ([A-Za-z_][A-Za-z0-9_]*)\]$", re.MULTILINE)
LEAN_IMPORT_RE = re.compile(r"^import\s+([\w.]+)\s*$", re.MULTILINE)
LEAN_DECL_RE = re.compile(
    r"^(theorem|lemma|def|abbrev|noncomputable\s+def|noncomputable\s+theorem)\s+"
    r"([A-Za-z_][A-Za-z0-9_.']*)",
    re.MULTILINE,
)
NODEREF_RE = re.compile(r"\\noderef\{([^}]+)\}")

# .tex environments
TEX_STATEMENT_ENVS = {"theorem", "lemma", "definition", "corollary", "proposition", "helper"}
TEX_PROOF_BEARING_ENVS = {"theorem", "lemma", "corollary", "helper"}
TEX_MAIN_NODE_ENVS = {"theorem", "lemma", "definition", "corollary", "helper"}
TEX_PREAMBLE_ENVS = {"definition", "proposition"}
TEX_PAPER_STATEMENT_ENVS = {"theorem", "lemma", "corollary"}
TEX_STMT_BEGIN_RE = re.compile(r"\\begin\{(" + "|".join(TEX_STATEMENT_ENVS) + r")\}")
TEX_STMT_END_RE = re.compile(r"\\end\{(" + "|".join(TEX_STATEMENT_ENVS) + r")\}")
TEX_STMT_BLOCK_RE = re.compile(
    r"\\begin\{(" + "|".join(TEX_STATEMENT_ENVS) + r")\}(?:\[(.*?)\])?(.*?)\\end\{\1\}",
    re.DOTALL,
)
TEX_DOCUMENT_BEGIN_RE = re.compile(r"\\begin\{document\}")
TEX_DOCUMENT_END_RE = re.compile(r"\\end\{document\}")
TEX_PROOF_BEGIN_RE = re.compile(r"\\begin\{proof\}")
TEX_PROOF_END_RE = re.compile(r"\\end\{proof\}")

PLACEHOLDER_PHRASES = [
    "trivial", "obvious", "left to the reader", "by similar argument",
    "straightforward", "clear from", "follows immediately",
    "by a standard argument", "well known", "easy to see",
]
PAPER_LABEL_RE = re.compile(r"\\label\{([^{}]+)\}")
DEFAULT_MAIN_RESULT_ENVS = {"theorem", "corollary"}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def tablet_dir(repo_path: Path) -> Path:
    return repo_path / TABLET_DIR


def node_lean_path(repo_path: Path, name: str) -> Path:
    return tablet_dir(repo_path) / f"{name}.lean"


def node_tex_path(repo_path: Path, name: str) -> Path:
    return tablet_dir(repo_path) / f"{name}.tex"


def header_tex_path(repo_path: Path) -> Path:
    return tablet_dir(repo_path) / "header.tex"


def index_md_path(repo_path: Path) -> Path:
    return tablet_dir(repo_path) / "INDEX.md"


def readme_md_path(repo_path: Path) -> Path:
    return tablet_dir(repo_path) / "README.md"


def preamble_lean_path(repo_path: Path) -> Path:
    return node_lean_path(repo_path, PREAMBLE_NAME)


def axioms_lean_path(repo_path: Path) -> Path:
    return node_lean_path(repo_path, AXIOMS_NAME)


# ---------------------------------------------------------------------------
# .lean file generation
# ---------------------------------------------------------------------------

def generate_node_lean(
    name: str,
    lean_statement: str,
    imports: List[str],
) -> str:
    """Generate a .lean file for a new tablet node.

    Args:
        name: Node name (valid Lean identifier)
        lean_statement: Full Lean declaration text (e.g., "theorem foo (x : Nat) : x = x")
        imports: List of import targets (e.g., ["Tablet.Preamble", "Tablet.helper_a"])
    """
    lines = []
    for imp in imports:
        lines.append(f"import {imp}")
    lines.append("")
    lines.append(TABLET_NODE_MARKER.format(name))
    lines.append("-- Do not rename or remove the declaration below.")
    lines.append("")
    # Ensure the statement ends with :=
    stmt = lean_statement.rstrip()
    if not stmt.endswith(":="):
        stmt = stmt + " :="
    lines.append(stmt)
    lines.append("sorry")
    lines.append("")
    return "\n".join(lines)


def declaration_line(lean_content: str, *, node_name: Optional[str] = None) -> Optional[str]:
    """Extract the main declaration line (theorem/lemma/def ... :=) from a .lean file.

    If node_name is given, finds the declaration matching that name.
    Otherwise, uses the TABLET NODE marker to determine the expected name,
    then finds the declaration matching it. Falls back to first declaration.
    """
    # Determine target name from marker if not provided
    if node_name is None:
        marker_match = TABLET_NODE_MARKER_RE.search(lean_content)
        if marker_match:
            node_name = marker_match.group(1)

    # Parse all declarations
    all_decls: List[Tuple[str, str]] = []  # (name, full_line)
    decl_lines: List[str] = []
    current_name: Optional[str] = None

    for line in lean_content.splitlines():
        stripped = line.strip()
        match = LEAN_DECL_RE.match(stripped)
        if match:
            # Save previous incomplete declaration
            if decl_lines and current_name:
                all_decls.append((current_name, " ".join(decl_lines)))
            current_name = match.group(2)
            decl_lines = [stripped]
            if ":=" in stripped:
                all_decls.append((current_name, " ".join(decl_lines)))
                decl_lines = []
                current_name = None
            continue
        if decl_lines:
            decl_lines.append(stripped)
            if ":=" in stripped:
                if current_name:
                    all_decls.append((current_name, " ".join(decl_lines)))
                decl_lines = []
                current_name = None

    if decl_lines and current_name:
        all_decls.append((current_name, " ".join(decl_lines)))

    # Find the target declaration
    if node_name:
        for name, line in all_decls:
            if name == node_name:
                return line

    # Fallback: return last declaration (most likely the main one after helpers)
    if all_decls:
        return all_decls[-1][1]
    return None


def normalize_declaration(decl: str) -> str:
    """Normalize a declaration line for hash comparison.

    Strips:
    - Proof start (:= by, := sorry, :=)
    - Common namespace prefixes (Filter., Real., Nat., Int., etc.)
    - Extra whitespace

    This handles the case where `open Filter Real` changes `Filter.Tendsto`
    to just `Tendsto` -- the theorem is semantically identical.
    """
    d = decl.strip()
    # Remove trailing proof start
    for suffix in [":= by", ":=by", ":= sorry", ":=sorry", ":="]:
        if d.endswith(suffix):
            d = d[:-len(suffix)].strip()
            break
    # Strip common namespace prefixes that `open` statements remove
    for prefix in ["Filter.", "Real.", "Nat.", "Int.", "Set.", "Finset.",
                    "MeasureTheory.", "Topology.", "ENNReal.", "NNReal."]:
        d = d.replace(prefix, "")
    # Normalize whitespace
    d = " ".join(d.split())
    return d


def declaration_hash(lean_content: str, *, node_name: Optional[str] = None) -> str:
    """SHA-256 hash of the normalized declaration (without proof start)."""
    decl = declaration_line(lean_content, node_name=node_name)
    if decl is None:
        return ""
    normalized = normalize_declaration(decl)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def extract_marker_name(lean_content: str) -> Optional[str]:
    """Extract the node name from the -- [TABLET NODE: name] marker."""
    match = TABLET_NODE_MARKER_RE.search(lean_content)
    return match.group(1) if match else None


def extract_declaration_name(lean_content: str) -> Optional[str]:
    """Extract the declaration name from the Lean file."""
    match = LEAN_DECL_RE.search(lean_content)
    return match.group(2) if match else None


def extract_imports(lean_content: str) -> List[str]:
    """Extract all import targets from a .lean file."""
    return LEAN_IMPORT_RE.findall(lean_content)


def extract_tablet_imports(lean_content: str) -> List[str]:
    """Extract Tablet.* import names (without the Tablet. prefix)."""
    return [
        imp.split(".", 1)[1]
        for imp in extract_imports(lean_content)
        if imp.startswith("Tablet.") and imp != "Tablet"
    ]


def validate_imports(lean_content: str, allowed_prefixes: List[str]) -> List[str]:
    """Check all imports match Tablet.* or allowed prefixes. Return list of violations.

    Bare top-level imports like `import Mathlib` (without a submodule) are
    always rejected — only specific submodule imports are allowed.
    """
    violations = []
    for imp in extract_imports(lean_content):
        if imp.startswith("Tablet."):
            continue
        # Reject bare top-level imports (e.g., "Mathlib" without a submodule)
        if imp in allowed_prefixes:
            violations.append(f"{imp} (bare import not allowed -- use specific submodules like {imp}.SomeModule)")
            continue
        if any(imp.startswith(prefix + ".") for prefix in allowed_prefixes):
            continue
        violations.append(imp)
    return violations


def has_sorry(lean_content: str) -> bool:
    """Check if the lean content contains sorry (in non-comment, non-string context)."""
    masked = mask_comments_and_strings(lean_content)
    return bool(re.search(r"\bsorry\b", masked))


# ---------------------------------------------------------------------------
# Comment/string masking
# ---------------------------------------------------------------------------

def mask_comments_and_strings(text: str) -> str:
    """Replace comments and string literals with spaces, preserving line structure.

    Handles:
    - Line comments: -- ...
    - Block comments: /- ... -/ (nested)
    - String literals: "..."
    """
    result = []
    i = 0
    n = len(text)
    block_depth = 0

    while i < n:
        if block_depth > 0:
            if i + 1 < n and text[i] == "/" and text[i + 1] == "-":
                block_depth += 1
                result.append("  ")
                i += 2
            elif i + 1 < n and text[i] == "-" and text[i + 1] == "/":
                block_depth -= 1
                result.append("  ")
                i += 2
            elif text[i] == "\n":
                result.append("\n")
                i += 1
            else:
                result.append(" ")
                i += 1
        elif i + 1 < n and text[i] == "/" and text[i + 1] == "-":
            block_depth = 1
            result.append("  ")
            i += 2
        elif i + 1 < n and text[i] == "-" and text[i + 1] == "-":
            result.append("  ")
            i += 2
            while i < n and text[i] != "\n":
                result.append(" ")
                i += 1
        elif text[i] == '"':
            result.append(" ")
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\n":
                    result.append("\n")
                else:
                    result.append(" ")
                i += 1
            if i < n:
                result.append(" ")
                i += 1
        else:
            result.append(text[i])
            i += 1

    return "".join(result)


def scan_preamble_definitions(preamble_content: str) -> List[Dict[str, Any]]:
    """Check that Preamble.lean contains no definitions — only imports.

    All definitions must be in their own node files with .tex counterparts.
    """
    masked = mask_comments_and_strings(preamble_content)
    hits = []
    for lineno, (masked_line, original_line) in enumerate(
        zip(masked.splitlines(), preamble_content.splitlines()), start=1
    ):
        if re.match(r"(noncomputable\s+)?def\b", masked_line.strip()):
            hits.append({
                "keyword": "def in Preamble",
                "line": lineno,
                "text": original_line.strip(),
            })
    return hits


def scan_sorry_in_definitions(lean_content: str) -> List[Dict[str, Any]]:
    """Check for sorry used in definitions (not proof-bearing declarations).

    sorry is allowed in proof-bearing theorem-like declaration bodies
    (`helper`, `lemma`, `theorem`, `corollary`) but NEVER in definitions,
    as a sorry'd definition provides no properties and makes downstream proofs impossible.
    """
    masked = mask_comments_and_strings(lean_content)
    hits = []
    in_def = False
    for lineno, (masked_line, original_line) in enumerate(
        zip(masked.splitlines(), lean_content.splitlines()), start=1
    ):
        stripped = masked_line.strip()
        # Track if we're inside a def/noncomputable def body
        if re.match(r"(noncomputable\s+)?def\b", stripped):
            in_def = True
        elif re.match(r"(theorem|lemma|example)\b", stripped):
            in_def = False
        # Check for sorry on any line while in a definition (including the def line itself)
        if in_def and re.search(r"\bsorry\b", masked_line):
            hits.append({
                "keyword": "sorry (in definition)",
                "line": lineno,
                "text": original_line.strip(),
            })
        # Reset on blank lines
        if not stripped:
            in_def = False
    return hits


def scan_forbidden_keywords(lean_content: str, forbidden: List[str]) -> List[Dict[str, Any]]:
    """Scan masked lean source for forbidden keywords. Returns list of {keyword, line, text}."""
    def _pattern(keyword: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_']+", keyword):
            return r"\b" + re.escape(keyword) + r"\b"
        return r"(?<![A-Za-z0-9_'])" + re.escape(keyword) + r"(?![A-Za-z0-9_'])"

    masked = mask_comments_and_strings(lean_content)
    hits = []
    for lineno, (masked_line, original_line) in enumerate(
        zip(masked.splitlines(), lean_content.splitlines()), start=1
    ):
        for keyword in forbidden:
            pattern = _pattern(keyword)
            if re.search(pattern, masked_line):
                hits.append({"keyword": keyword, "line": lineno, "text": original_line.strip()})
    return hits


# ---------------------------------------------------------------------------
# .tex validation
# ---------------------------------------------------------------------------

def validate_tex_format(tex_content: str, *, is_preamble: bool = False) -> List[str]:
    """Validate .tex file format. Returns list of error messages (empty = valid)."""
    errors = []

    if is_preamble:
        # Preamble: zero or more proposition/definition environments, no proof
        if TEX_PROOF_BEGIN_RE.search(tex_content):
            errors.append("Preamble .tex must not contain \\begin{proof}")
        for env in TEX_STATEMENT_ENVS - TEX_PREAMBLE_ENVS:
            if re.search(r"\\begin\{" + env + r"\}", tex_content):
                errors.append(
                    "Preamble .tex should only use proposition/definition environments, "
                    f"found {env}"
                )
        return errors

    # Regular node: exactly one statement env, exactly one proof env (if open)
    stmt_begins = TEX_STMT_BEGIN_RE.findall(tex_content)
    proof_begins = TEX_PROOF_BEGIN_RE.findall(tex_content)

    if len(stmt_begins) == 0:
        errors.append("Missing statement environment (theorem/lemma/definition/corollary/helper)")
    elif len(stmt_begins) > 1:
        errors.append(f"Multiple statement environments found ({len(stmt_begins)}), expected exactly 1")
    else:
        stmt_env = stmt_begins[0]
        if stmt_env not in TEX_MAIN_NODE_ENVS:
            errors.append(
                "Ordinary tablet nodes must use theorem/lemma/definition/corollary/helper environments, "
                f"found {stmt_env}"
            )

    # Proof is optional for closed nodes -- caller decides whether to require it
    # We just validate format if present
    if len(proof_begins) > 1:
        errors.append(f"Multiple proof environments found ({len(proof_begins)}), expected at most 1")

    return errors


def extract_tex_statement_items(
    tex_content: str,
    *,
    is_preamble: bool = False,
) -> List[Dict[str, str]]:
    """Extract top-level statement environments as lightweight structured items."""
    allowed = TEX_PREAMBLE_ENVS if is_preamble else TEX_MAIN_NODE_ENVS
    items: List[Dict[str, str]] = []
    for index, match in enumerate(TEX_STMT_BLOCK_RE.finditer(tex_content), start=1):
        env = str(match.group(1) or "").strip()
        if env not in allowed:
            continue
        title = str(match.group(2) or "").strip()
        body = str(match.group(3) or "").strip()
        item_id = f"Preamble[{index}]" if is_preamble else f"Item[{index}]"
        items.append(
            {
                "id": item_id,
                "env": env,
                "title": title,
                "body": body,
            }
        )
    return items


def tex_statement_env_from_content(
    tex_content: str,
    *,
    is_preamble: bool = False,
) -> str:
    items = extract_tex_statement_items(tex_content, is_preamble=is_preamble)
    if not items:
        return ""
    return str(items[0].get("env", "") or "").strip().lower()


def node_statement_environment(repo_path: Path, node_name: str) -> str:
    if node_name == PREAMBLE_NAME:
        tex_path = tablet_dir(repo_path) / "Preamble.tex"
        if not tex_path.exists():
            return ""
        return tex_statement_env_from_content(
            tex_path.read_text(encoding="utf-8"),
            is_preamble=True,
        )
    tex_path = node_tex_path(repo_path, node_name)
    if not tex_path.exists():
        return ""
    return tex_statement_env_from_content(tex_path.read_text(encoding="utf-8"))


def is_paper_statement_environment(env: str) -> bool:
    return str(env or "").strip().lower() in TEX_PAPER_STATEMENT_ENVS


def is_proof_bearing_statement_environment(env: str) -> bool:
    return str(env or "").strip().lower() in TEX_PROOF_BEARING_ENVS


def strip_tex_comments_preserve_lines(tex: str) -> str:
    """Remove TeX comments while preserving line structure.

    A `%` starts a comment unless it is escaped by an odd number of immediately
    preceding backslashes. Newlines are preserved so downstream line numbers
    still refer to the original source.
    """
    stripped_lines: List[str] = []
    for line in tex.splitlines(keepends=True):
        newline = ""
        body = line
        if line.endswith("\r\n"):
            newline = "\r\n"
            body = line[:-2]
        elif line.endswith("\n"):
            newline = "\n"
            body = line[:-1]
        cut = len(body)
        backslashes = 0
        for idx, ch in enumerate(body):
            if ch == "\\":
                backslashes += 1
                continue
            if ch == "%":
                if backslashes % 2 == 0:
                    cut = idx
                    break
                backslashes = 0
                continue
            backslashes = 0
        stripped_lines.append(body[:cut] + newline)
    return "".join(stripped_lines)


def extract_paper_statement_blocks(
    paper_text: str,
    *,
    envs: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Extract statement-like TeX blocks from a paper with labels and line ranges."""
    wanted_envs = {str(env).strip().lower() for env in (envs or TEX_STATEMENT_ENVS)}
    search_start = 0
    search_end = len(paper_text)
    begin_match = TEX_DOCUMENT_BEGIN_RE.search(paper_text)
    end_match = TEX_DOCUMENT_END_RE.search(paper_text)
    if begin_match and end_match and end_match.start() >= begin_match.end():
        search_start = begin_match.end()
        search_end = end_match.start()
    blocks: List[Dict[str, Any]] = []
    search_text = strip_tex_comments_preserve_lines(paper_text[search_start:search_end])
    line_offset = paper_text.count("\n", 0, search_start)
    for match in TEX_STMT_BLOCK_RE.finditer(search_text):
        env = str(match.group(1) or "").strip().lower()
        if env not in wanted_envs:
            continue
        title = str(match.group(2) or "").strip()
        body = str(match.group(3) or "").strip()
        full_block = str(match.group(0) or "")
        labels = []
        seen_labels: Set[str] = set()
        for label in PAPER_LABEL_RE.findall(full_block):
            if label not in seen_labels:
                seen_labels.add(label)
                labels.append(label)
        start_line = line_offset + search_text.count("\n", 0, match.start()) + 1
        end_line = line_offset + search_text.count("\n", 0, match.end()) + 1
        blocks.append(
            {
                "env": env,
                "title": title,
                "body": body,
                "text": full_block.strip(),
                "labels": labels,
                "start_line": start_line,
                "end_line": end_line,
            }
        )
    return blocks


def extract_paper_statement_labels(
    paper_path: Path,
    *,
    envs: Optional[Set[str]] = None,
) -> Set[str]:
    """Return the set of TeX labels attached to statement environments in a paper."""
    if not paper_path.exists():
        return set()
    paper_text = paper_path.read_text(encoding="utf-8", errors="replace")
    labels: Set[str] = set()
    for block in extract_paper_statement_blocks(paper_text, envs=envs):
        for label in block.get("labels", []):
            cleaned = str(label).strip()
            if cleaned:
                labels.add(cleaned)
    return labels


def normalize_main_result_target(raw: Any) -> Dict[str, Any]:
    """Normalize a configured main-result target selector.

    A target may be identified by:
    - `{"tex_label": "main"}`
    - `{"start_line": 120, "end_line": 140}`
    - `{"start_line": 120, "end_line": 140, "tex_label": "main"}`
    - `"main"` as a shorthand for `{"tex_label": "main"}`
    """
    if isinstance(raw, str):
        label = raw.strip()
        return {"tex_label": label} if label else {}
    if not isinstance(raw, dict):
        return {}

    label = str(raw.get("tex_label", "") or "").strip()
    normalized = normalize_paper_provenance(raw)
    if normalized:
        if label:
            normalized["tex_label"] = label
        return normalized
    if label:
        return {"tex_label": label}
    return {}


def format_main_result_target(raw: Any) -> str:
    target = normalize_main_result_target(raw)
    if not target:
        return "(invalid target)"
    label = str(target.get("tex_label", "") or "").strip()
    if "start_line" in target and "end_line" in target:
        start_line = int(target["start_line"])
        end_line = int(target["end_line"])
        line_text = f"line {start_line}" if start_line == end_line else f"lines {start_line}-{end_line}"
        return f"{label} ({line_text})" if label else line_text
    return label or "(invalid target)"


def main_result_target_key(raw: Any) -> str:
    target = normalize_main_result_target(raw)
    if not target:
        return ""
    label = str(target.get("tex_label", "") or "").strip()
    if label:
        return f"label:{label}"
    if "start_line" in target and "end_line" in target:
        return f"lines:{int(target['start_line'])}-{int(target['end_line'])}"
    return ""


def infer_main_result_targets_from_paper(
    paper_path: Path,
    *,
    envs: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Infer default main-result target selectors from paper statements.

    Labeled statements become label-based targets with their line range attached.
    Unlabeled statements become line-range targets.
    """
    if not paper_path.exists():
        return []
    paper_text = paper_path.read_text(encoding="utf-8", errors="replace")
    blocks = extract_paper_statement_blocks(
        paper_text,
        envs=envs or DEFAULT_MAIN_RESULT_ENVS,
    )
    targets: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for block in blocks:
        block_labels = [str(label).strip() for label in block.get("labels", []) if str(label).strip()]
        if block_labels:
            target = {
                "start_line": int(block["start_line"]),
                "end_line": int(block["end_line"]),
                "tex_label": block_labels[0],
            }
        else:
            target = {
                "start_line": int(block["start_line"]),
                "end_line": int(block["end_line"]),
            }
        key = main_result_target_key(target)
        if not key or key in seen:
            continue
        seen.add(key)
        targets.append(target)
    return targets


def infer_main_result_labels_from_paper(
    paper_path: Path,
    *,
    strict: bool = False,
) -> List[str]:
    """Infer default main-result labels from labeled paper theorems/corollaries."""
    if not paper_path.exists():
        return []
    blocks = infer_main_result_targets_from_paper(
        paper_path,
        envs=DEFAULT_MAIN_RESULT_ENVS,
    )
    labels: List[str] = []
    seen_labels: Set[str] = set()
    unlabeled: List[str] = []
    for block in blocks:
        block_labels = [str(block.get("tex_label", "") or "").strip()] if str(block.get("tex_label", "") or "").strip() else []
        if not block_labels:
            unlabeled.append(
                f"statement lines {block['start_line']}-{block['end_line']}"
            )
            continue
        for label in block_labels:
            if label in seen_labels:
                continue
            seen_labels.add(label)
            labels.append(label)
    if strict and unlabeled:
        raise ValueError(
            "Default main-result label inference requires every paper theorem/corollary to carry a TeX label. "
            f"Missing labels for: {', '.join(unlabeled)}"
        )
    return labels


def resolve_main_result_targets(
    *,
    paper_path: Optional[Path],
    raw_targets: Any = None,
    raw_labels: Any = None,
) -> List[Dict[str, Any]]:
    """Resolve configured main-result selectors to normalized target objects."""
    resolved: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    label_index: Dict[str, Dict[str, Any]] = {}
    if paper_path and paper_path.exists():
        for target in infer_main_result_targets_from_paper(paper_path):
            label = str(target.get("tex_label", "") or "").strip()
            if label and label not in label_index:
                label_index[label] = dict(target)

    def add_target(raw: Any) -> None:
        target = normalize_main_result_target(raw)
        if not target:
            return
        label = str(target.get("tex_label", "") or "").strip()
        if label and label in label_index:
            enriched = dict(label_index[label])
            target = enriched
        key = main_result_target_key(target)
        if not key or key in seen:
            return
        seen.add(key)
        resolved.append(target)

    if isinstance(raw_targets, list) and raw_targets:
        for raw_target in raw_targets:
            add_target(raw_target)
        return resolved

    if isinstance(raw_labels, list) and raw_labels:
        for raw_label in raw_labels:
            add_target(str(raw_label))
        return resolved

    if paper_path and paper_path.exists():
        return infer_main_result_targets_from_paper(paper_path)
    return []


def main_result_target_matches_provenance(target: Any, provenance_raw: Any) -> bool:
    target_norm = normalize_main_result_target(target)
    provenance = normalize_paper_provenance(provenance_raw)
    if not target_norm or not provenance:
        return False
    target_label = str(target_norm.get("tex_label", "") or "").strip()
    if target_label:
        return str(provenance.get("tex_label", "") or "").strip() == target_label
    return (
        int(provenance.get("start_line", 0) or 0) == int(target_norm.get("start_line", 0) or 0)
        and int(provenance.get("end_line", 0) or 0) == int(target_norm.get("end_line", 0) or 0)
    )


def main_result_target_coverage(
    tablet: TabletState,
    repo_path: Path,
    main_result_targets: Sequence[Any],
) -> Dict[str, Dict[str, Any]]:
    """Return non-helper and helper coverage for each configured main-result target."""
    coverage: Dict[str, Dict[str, Any]] = {}
    ordered_keys: List[str] = []
    for raw_target in main_result_targets:
        target = normalize_main_result_target(raw_target)
        key = main_result_target_key(target)
        if not key or key in coverage:
            continue
        ordered_keys.append(key)
        coverage[key] = {"target": target, "nodes": [], "helper_nodes": []}

    if not coverage:
        return {}

    for name, node in sorted(tablet.nodes.items()):
        if name in {PREAMBLE_NAME, AXIOMS_NAME}:
            continue
        provenance = normalize_paper_provenance(node.paper_provenance)
        env = node_statement_environment(repo_path, name)
        for key in ordered_keys:
            target = coverage[key]["target"]
            if not main_result_target_matches_provenance(target, provenance):
                continue
            if env == "helper":
                coverage[key]["helper_nodes"].append(name)
            else:
                coverage[key]["nodes"].append(name)

    for key in ordered_keys:
        coverage[key]["nodes"].sort()
        coverage[key]["helper_nodes"].sort()
    return coverage


def main_result_target_issues(
    tablet: TabletState,
    repo_path: Path,
    main_result_targets: Sequence[Any],
) -> List[Dict[str, Any]]:
    """Describe missing/invalid configured main-result target coverage."""
    issues: List[Dict[str, Any]] = []
    coverage = main_result_target_coverage(tablet, repo_path, main_result_targets)
    for key, entry in coverage.items():
        target = dict(entry.get("target", {}))
        target_text = format_main_result_target(target)
        helper_nodes = list(entry.get("helper_nodes", []))
        nodes = list(entry.get("nodes", []))
        if helper_nodes:
            issues.append(
                {
                    "target": target,
                    "target_key": key,
                    "kind": "helper_forbidden",
                    "nodes": helper_nodes,
                    "reason": f"Configured main-result target `{target_text}` is attached to helper node(s): {', '.join(helper_nodes)}.",
                }
            )
        if not nodes:
            issues.append(
                {
                    "target": target,
                    "target_key": key,
                    "kind": "missing",
                    "nodes": [],
                    "reason": f"Configured main-result target `{target_text}` is not covered by any non-helper node.",
                }
            )
    return issues


def main_result_label_coverage(
    tablet: TabletState,
    repo_path: Path,
    main_result_labels: Sequence[str],
) -> Dict[str, Dict[str, List[str]]]:
    """Backward-compatible label-only coverage view."""
    coverage = main_result_target_coverage(
        tablet,
        repo_path,
        [{"tex_label": str(raw).strip()} for raw in main_result_labels if str(raw).strip()],
    )
    return {
        str(entry["target"].get("tex_label", "")): {
            "nodes": list(entry.get("nodes", [])),
            "helper_nodes": list(entry.get("helper_nodes", [])),
        }
        for entry in coverage.values()
        if str(entry.get("target", {}).get("tex_label", "")).strip()
    }


def main_result_label_issues(
    tablet: TabletState,
    repo_path: Path,
    main_result_labels: Sequence[str],
) -> List[Dict[str, Any]]:
    """Backward-compatible label-only issues view."""
    raw_issues = main_result_target_issues(
        tablet,
        repo_path,
        [{"tex_label": str(raw).strip()} for raw in main_result_labels if str(raw).strip()],
    )
    issues: List[Dict[str, Any]] = []
    for entry in raw_issues:
        target = normalize_main_result_target(entry.get("target", {}))
        label = str(target.get("tex_label", "") or "").strip()
        reason = str(entry.get("reason", "") or "").strip()
        if label:
            reason = reason.replace("main-result target", "main-result label")
        issues.append(
            {
                "label": label,
                "kind": str(entry.get("kind", "") or "").strip(),
                "nodes": list(entry.get("nodes", [])),
                "reason": reason,
            }
        )
    return issues


def main_result_covering_nodes(
    tablet: TabletState,
    repo_path: Path,
    main_result_targets: Sequence[Any],
) -> Set[str]:
    """Return the non-helper node set currently covering configured targets."""
    coverage = main_result_target_coverage(tablet, repo_path, main_result_targets)
    nodes: Set[str] = set()
    for entry in coverage.values():
        nodes.update(entry.get("nodes", []))
    return nodes


def find_unsupported_nodes(
    tablet: TabletState,
    repo_path: Path,
    main_result_targets: Sequence[Any],
) -> List[str]:
    """Find nodes outside the support closure of all configured covered targets.

    This pruning is suspended until every configured target label has at least one
    non-helper covering node.
    """
    targets = [normalize_main_result_target(raw) for raw in main_result_targets]
    targets = [target for target in targets if target]
    if not targets:
        return []
    if any(issue.get("kind") == "missing" for issue in main_result_target_issues(tablet, repo_path, targets)):
        return []
    supported = main_result_covering_nodes(tablet, repo_path, targets)
    for name in sorted(list(supported)):
        supported |= compute_import_closure(repo_path, name)
    unsupported = [
        name
        for name in sorted(tablet.nodes.keys())
        if name not in {PREAMBLE_NAME, AXIOMS_NAME}
        and name not in supported
    ]
    return unsupported


def extract_noderefs(tex_content: str) -> List[str]:
    """Extract all \\noderef{name} references from .tex content."""
    return NODEREF_RE.findall(tex_content)


def check_placeholder_language(tex_content: str) -> List[str]:
    """Check for placeholder phrases in .tex proof content. Returns matches found."""
    lowered = tex_content.lower()
    return [phrase for phrase in PLACEHOLDER_PHRASES if phrase in lowered]


# ---------------------------------------------------------------------------
# Import closure computation
# ---------------------------------------------------------------------------

def compute_import_closure(
    repo_path: Path,
    node_name: str,
    *,
    _cache: Optional[Dict[str, Set[str]]] = None,
) -> Set[str]:
    """Compute the transitive set of all Tablet nodes imported by a given node.

    Returns a set of node names (not including the node itself).
    """
    if _cache is None:
        _cache = {}
    if node_name in _cache:
        return _cache[node_name]

    lean_path = node_lean_path(repo_path, node_name)
    if not lean_path.exists():
        _cache[node_name] = set()
        return set()

    content = lean_path.read_text(encoding="utf-8")
    direct = set(extract_tablet_imports(content))

    closure: Set[str] = set(direct)
    for dep in direct:
        closure |= compute_import_closure(repo_path, dep, _cache=_cache)

    _cache[node_name] = closure
    return closure


def compute_reverse_import_closure(
    repo_path: Path,
    node_name: str,
) -> Set[str]:
    """Compute the transitive set of Tablet nodes that (directly or indirectly) import a node."""
    tablet_dir = repo_path / "Tablet"
    if not tablet_dir.exists():
        return set()

    importers: Dict[str, Set[str]] = {}
    for lean_path in tablet_dir.glob("*.lean"):
        name = lean_path.stem
        if name in (PREAMBLE_NAME, AXIOMS_NAME):
            continue
        content = lean_path.read_text(encoding="utf-8")
        for dep in extract_tablet_imports(content):
            importers.setdefault(dep, set()).add(name)

    closure: Set[str] = set()
    stack = list(importers.get(node_name, set()))
    while stack:
        parent = stack.pop()
        if parent in closure:
            continue
        closure.add(parent)
        stack.extend(sorted(importers.get(parent, set())))
    return closure


def compute_target_impact_region(
    repo_path: Path,
    node_name: str,
) -> Set[str]:
    """Compute the target-centered edit region for theorem-stating restructures.

    The impact region includes the target itself, its prerequisite closure, and
    every downstream consumer that imports the target directly or transitively.
    """
    if not node_name or not node_lean_path(repo_path, node_name).exists():
        return set()
    return (
        {node_name}
        | compute_import_closure(repo_path, node_name)
        | compute_reverse_import_closure(repo_path, node_name)
    )


def coarse_node_names(tablet: TabletState) -> Set[str]:
    """Return the explicit coarse-package node set."""
    return {
        name
        for name, node in tablet.nodes.items()
        if name not in {PREAMBLE_NAME, AXIOMS_NAME} and node.coarse
    }


def _extract_tex_statement_block(tex_content: str) -> str:
    proof_start = tex_content.find("\\begin{proof}")
    if proof_start >= 0:
        return tex_content[:proof_start].strip()
    return tex_content.strip()


def coarse_interface_fingerprint(
    tablet: TabletState,
    repo_path: Path,
    node_name: str,
    *,
    coarse_names: Optional[Set[str]] = None,
) -> str:
    """Fingerprint the accepted coarse interface of one node.

    This fingerprint is intentionally stable under Lean proof-body edits and
    helper-import additions, but changes when the paper-facing coarse package
    itself changes.
    """
    node = tablet.nodes.get(node_name)
    if node is None or node_name in {PREAMBLE_NAME, AXIOMS_NAME}:
        return ""
    coarse_set = set(coarse_names or coarse_node_names(tablet))
    lean_path = node_lean_path(repo_path, node_name)
    tex_path = node_tex_path(repo_path, node_name)
    if not lean_path.exists() or not tex_path.exists():
        return ""
    lean_content = lean_path.read_text(encoding="utf-8")
    tex_content = tex_path.read_text(encoding="utf-8")
    coarse_imports = sorted(
        dep for dep in extract_tablet_imports(lean_content)
        if dep in coarse_set
    )
    from lagent_tablets.nl_cache import correspondence_fingerprint

    payload = {
        "kind": node.kind,
        "correspondence_fingerprint": correspondence_fingerprint(repo_path, node_name) or "",
        "tex_statement": _extract_tex_statement_block(tex_content),
        "coarse_imports": coarse_imports,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def refresh_coarse_package_hashes(
    tablet: TabletState,
    repo_path: Path,
    *,
    cycle: Optional[int] = None,
    new_coarse: Optional[Set[str]] = None,
) -> None:
    """Refresh persisted fingerprints for the explicit coarse package."""
    if new_coarse:
        for name in new_coarse:
            node = tablet.nodes.get(name)
            if node is not None and name not in {PREAMBLE_NAME, AXIOMS_NAME}:
                node.coarse = True
    coarse_set = coarse_node_names(tablet)
    for name in coarse_set:
        node = tablet.nodes.get(name)
        if node is None:
            continue
        node.coarse = True
        node.coarse_content_hash = coarse_interface_fingerprint(
            tablet,
            repo_path,
            name,
            coarse_names=coarse_set,
        )
        if cycle is not None:
            tablet.last_modified_at_cycle = cycle


def freeze_current_coarse_package(
    tablet: TabletState,
    repo_path: Path,
    *,
    cycle: Optional[int] = None,
) -> None:
    """Mark the current accepted theorem-stating package as coarse."""
    new_coarse = {
        name for name in tablet.nodes
        if name not in {PREAMBLE_NAME, AXIOMS_NAME}
    }
    refresh_coarse_package_hashes(
        tablet,
        repo_path,
        cycle=cycle,
        new_coarse=new_coarse,
    )


def find_orphan_nodes(tablet: TabletState, repo_path: Path) -> List[str]:
    """Legacy helper: return simple leaf nodes without any env-based exemptions.

    Main runtime semantics now use configured main-result labels plus support
    closure via ``find_unsupported_nodes``. This helper remains only for
    compatibility with older callers/tests that still ask for raw leaf nodes.
    """
    imported_by_something: Set[str] = set()
    for name in tablet.nodes:
        if name in {PREAMBLE_NAME, AXIOMS_NAME}:
            continue
        lean_path = node_lean_path(repo_path, name)
        if lean_path.exists():
            content = lean_path.read_text(encoding="utf-8")
            for dep in extract_tablet_imports(content):
                imported_by_something.add(dep)

    orphans = []
    for name in tablet.nodes:
        if name in {PREAMBLE_NAME, AXIOMS_NAME}:
            continue
        if name not in imported_by_something:
            orphans.append(name)
    return sorted(orphans)


# ---------------------------------------------------------------------------
# Preamble validation
# ---------------------------------------------------------------------------

def validate_preamble_diff(old_content: str, new_content: str, allowed_prefixes: List[str]) -> List[str]:
    """Validate that preamble changes are import-additions only. Returns errors."""
    old_lines = set(old_content.strip().splitlines())
    new_lines = new_content.strip().splitlines()
    errors = []
    for line in new_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped in old_lines:
            continue
        # New line -- must be an allowed import
        match = re.match(r"^import\s+([\w.]+)\s*$", stripped)
        if not match:
            errors.append(f"Non-import line added to Preamble: {stripped!r}")
            continue
        target = match.group(1)
        if not any(target.startswith(p + ".") or target == p for p in allowed_prefixes):
            errors.append(f"Import with disallowed prefix in Preamble: {target!r}")

    # Check for removed lines
    new_set = set(l.strip() for l in new_lines if l.strip() and not l.strip().startswith("--"))
    for line in old_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped not in new_set:
            errors.append(f"Line removed from Preamble (only additions allowed): {stripped!r}")

    return errors


# ---------------------------------------------------------------------------
# INDEX.md and README.md generation
# ---------------------------------------------------------------------------

def generate_index_md(tablet: TabletState, repo_path: Path) -> str:
    """Generate Tablet/INDEX.md content."""
    lines = ["# Tablet Index", ""]
    lines.append("| Name | Env | Status | Paper Ref | Title | Imports |")
    lines.append("|------|-----|--------|-----------|-------|---------|")

    for name in sorted(tablet.nodes.keys()):
        node = tablet.nodes[name]
        lean_path = node_lean_path(repo_path, name)
        imports_str = ""
        env = "-"
        if name == PREAMBLE_NAME:
            env = "preamble"
        elif (repo_path / "Tablet" / f"{name}.tex").exists():
            env = node_statement_environment(repo_path, name) or "-"
        if lean_path.exists() and name != PREAMBLE_NAME:
            content = lean_path.read_text(encoding="utf-8")
            tablet_imports = extract_tablet_imports(content)
            imports_str = ", ".join(tablet_imports) if tablet_imports else "-"

        lines.append(
            f"| {name} | {env} | {node.status} | {format_paper_provenance(node.paper_provenance) or '-'} | {node.title} | {imports_str} |"
        )

    lines.append("")
    m = tablet.metrics()
    lines.append(f"**Total:** {m['total_nodes']} nodes | **Closed:** {m['closed_nodes']} | **Open:** {m['open_nodes']}")
    lines.append("")
    return "\n".join(lines)


def generate_readme_md(tablet: TabletState) -> str:
    """Generate Tablet/README.md content (paper-facing summary)."""
    lines = ["# Proof Tablet", ""]

    referenced_nodes = [
        (n, node)
        for n, node in sorted(tablet.nodes.items())
        if node.kind != "preamble" and node.paper_provenance
    ]
    structural_nodes = [
        (n, node)
        for n, node in sorted(tablet.nodes.items())
        if node.kind != "preamble" and not node.paper_provenance
    ]

    if referenced_nodes:
        lines.append("## Nodes With Paper References")
        lines.append("")
        lines.append("| Name | Provenance | Title | Status |")
        lines.append("|------|------------|-------|--------|")
        for name, node in referenced_nodes:
            lines.append(
                f"| {name} | {format_paper_provenance(node.paper_provenance)} | {node.title} | {node.status} |"
            )
        lines.append("")

    if structural_nodes:
        lines.append("## Nodes Without Paper References")
        lines.append("")
        lines.append("| Name | Title | Status |")
        lines.append("|------|-------|--------|")
        for name, node in structural_nodes:
            lines.append(f"| {name} | {node.title} | {node.status} |")
        lines.append("")

    m = tablet.metrics()
    lines.append(f"**Summary:** {m['closed_nodes']}/{m['total_nodes']} closed")
    lines.append("")
    return "\n".join(lines)


def generate_header_tex() -> str:
    """Generate Tablet/header.tex content."""
    return (
        "% Tablet LaTeX header -- generated by lagent-supervisor\n"
        "% Do not edit manually.\n"
        "\n"
        "\\newcommand{\\noderef}[1]{\\texttt{#1}}\n"
    )


def _safe_write(path: Path, content: str) -> None:
    """Write a file safely, handling cross-user ownership.

    If the file exists and is owned by another user (e.g., lagentworker),
    we can't chmod or overwrite it directly. Instead, delete it first
    (which works if the DIRECTORY is writable by our group) then create a new file.
    """
    import tempfile
    if path.exists():
        try:
            path.write_text(content, encoding="utf-8")
            path.chmod(0o664)
            return
        except PermissionError:
            # File owned by another user -- delete and recreate
            try:
                path.unlink()
            except PermissionError:
                # Can't delete either -- try temp file + rename in the same dir
                fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
                try:
                    os.write(fd, content.encode("utf-8"))
                    os.close(fd)
                    os.chmod(tmp, 0o664)
                    os.replace(tmp, str(path))
                except Exception:
                    os.close(fd)
                    os.unlink(tmp)
                    raise
                return
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o664)
    except PermissionError:
        pass


def generate_tablet_root_lean(tablet: TabletState) -> str:
    """Generate the root Tablet.lean file that imports all tablet modules.

    Lake requires this file to exist for the Tablet lean_lib.
    """
    lines = ["-- Auto-generated by lagent-supervisor. Do not edit."]
    for name in sorted(tablet.nodes.keys()):
        lines.append(f"import Tablet.{name}")
    lines.append("")
    return "\n".join(lines)


def regenerate_support_files(tablet: TabletState, repo_path: Path) -> None:
    """Regenerate INDEX.md, README.md, header.tex, and Tablet.lean root.

    These are supervisor-generated files. We ensure they're writable by
    the supervisor (group-writable) since lagentworker might have created
    them in a previous cycle.
    """
    tdir = tablet_dir(repo_path)
    tdir.mkdir(parents=True, exist_ok=True)

    for target, content_fn in [
        (index_md_path(repo_path), lambda: generate_index_md(tablet, repo_path)),
        (readme_md_path(repo_path), lambda: generate_readme_md(tablet)),
        (repo_path / "Tablet.lean", lambda: generate_tablet_root_lean(tablet)),
    ]:
        _safe_write(target, content_fn())

    htex = header_tex_path(repo_path)
    if not htex.exists():
        htex.write_text(generate_header_tex(), encoding="utf-8")
        try:
            htex.chmod(0o664)
        except PermissionError:
            pass


# ---------------------------------------------------------------------------
# Node registration
# ---------------------------------------------------------------------------

def register_new_node(
    tablet: TabletState,
    repo_path: Path,
    *,
    name: str,
    kind: str,
    title: str = "",
    paper_provenance: Optional[Dict[str, Any]] = None,
    cycle: Optional[int] = None,
) -> TabletNode:
    """Register a new node in the tablet state (after its .lean and .tex files exist)."""
    lean_path = node_lean_path(repo_path, name)
    lean_content = lean_path.read_text(encoding="utf-8") if lean_path.exists() else ""
    node = TabletNode(
        name=name,
        kind=kind,
        status="open",
        title=title,
        paper_provenance=dict(paper_provenance or {}),
        lean_statement_hash=declaration_hash(lean_content, node_name=name),
    )
    tablet.nodes[name] = node
    if cycle is not None:
        tablet.last_modified_at_cycle = cycle
    return node


def mark_node_closed(
    tablet: TabletState,
    name: str,
    cycle: int,
    *,
    content_hash: str = "",
) -> None:
    """Mark a node as closed."""
    node = tablet.nodes.get(name)
    if node:
        node.status = "closed"
        node.closed_content_hash = content_hash
        node.closed_at_cycle = cycle
        node.soundness_status = "pass"
        node.verification_at_cycle = cycle


def mark_node_open(tablet: TabletState, name: str, cycle: int) -> None:
    """Mark a node as open (invalidated)."""
    node = tablet.nodes.get(name)
    if node:
        node.status = "open"
        node.closed_content_hash = ""
        node.invalidated_at_cycle = cycle
        node.closed_at_cycle = None
        node.soundness_status = "?"
        node.soundness_content_hash = ""


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

LEAN_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_NAMES = {PREAMBLE_NAME, AXIOMS_NAME}


def is_valid_node_name(name: str) -> bool:
    """Check if a name is a valid Lean identifier and not reserved."""
    return bool(LEAN_IDENT_RE.match(name)) and name not in RESERVED_NAMES


def find_name_conflicts(tablet: TabletState, new_names: List[str]) -> List[str]:
    """Check new names against existing tablet nodes. Returns conflicts."""
    existing = set(tablet.nodes.keys())
    return [name for name in new_names if name in existing]
