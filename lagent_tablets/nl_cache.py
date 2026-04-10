"""NL verification approval cache.

Two content-addressed sets:
- soundness_verified: per-node fingerprints for NL-proof soundness
- correspondence_verified: per-node fingerprints for Lean/NL correspondence

This avoids re-running expensive verification agents when nothing meaning-bearing
has changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from lagent_tablets.tablet import extract_tablet_imports, node_lean_path, node_tex_path, PREAMBLE_NAME


_LEAN_CORRESPONDENCE_CACHE: Dict[Tuple[str, Tuple[Tuple[str, str], ...], str], Optional[str]] = {}


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


def _extract_declaration_with_imports(lean_content: str) -> str:
    """Extract imports + declaration line, excluding the proof body.

    This captures the full meaning-bearing part of a .lean file:
    imports affect meaning (through definitions), and the declaration
    is the statement being checked. The proof body doesn't affect
    correspondence.
    """
    lines = lean_content.split("\n")
    result_lines = []
    for line in lines:
        result_lines.append(line)
        stripped = line.strip()
        # Stop after the declaration line (before the proof body)
        if ":= sorry" in stripped or ":= by" in stripped or stripped.endswith(":="):
            break
    return "\n".join(result_lines)


def _extract_meaning_bearing_lean_text(lean_content: str) -> str:
    """Extract a conservative text-level meaning summary for correspondence.

    For theorem-like declarations we ignore proof text and keep imports plus the
    declaration line. For definitions/inductives/structures, the body is
    meaning-bearing and we keep the full file text.
    """
    stripped = lean_content.lstrip()
    theorem_like_prefixes = (
        "theorem ",
        "lemma ",
        "example ",
        "corollary ",
    )
    if stripped.startswith(theorem_like_prefixes):
        return _extract_declaration_with_imports(lean_content)
    return lean_content.strip()


def _extract_tex_statement(tex_content: str) -> str:
    """Extract the statement portion of a node .tex file, excluding the proof."""
    proof_start = tex_content.find("\\begin{proof}")
    if proof_start >= 0:
        return tex_content[:proof_start].strip()
    return tex_content.strip()


_TEX_ENV_RE = re.compile(r"\\begin\{([A-Za-z*]+)\}")


def _tex_statement_environment(tex_statement: str) -> str:
    match = _TEX_ENV_RE.search(tex_statement)
    if not match:
        return ""
    return match.group(1).strip().lower()


def _tex_statement_is_definition(tex_statement: str) -> bool:
    return _tex_statement_environment(tex_statement) == "definition"


def _read_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _git_read_file(repo: Path, rev: str, rel_path: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{rev}:{rel_path}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _get_recursive_imports(repo: Path, node_name: str, visited: Set[str] = None) -> Set[str]:
    """Get all transitive .tex dependencies of a node (through imports)."""
    if visited is None:
        visited = set()
    if node_name in visited or node_name == PREAMBLE_NAME:
        return visited
    visited.add(node_name)
    lean_path = node_lean_path(repo, node_name)
    if lean_path.exists():
        content = lean_path.read_text(encoding="utf-8", errors="replace")
        for imp in extract_tablet_imports(content):
            if imp != PREAMBLE_NAME:
                _get_recursive_imports(repo, imp, visited)
    return visited


def _get_recursive_imports_from_reader(
    node_name: str,
    read_lean: Callable[[str], str],
    visited: Optional[Set[str]] = None,
) -> Set[str]:
    if visited is None:
        visited = set()
    if node_name in visited or node_name == PREAMBLE_NAME:
        return visited
    visited.add(node_name)
    content = read_lean(node_name)
    if content:
        for imp in extract_tablet_imports(content):
            if imp != PREAMBLE_NAME:
                _get_recursive_imports_from_reader(imp, read_lean, visited)
    return visited


def _get_direct_imports(repo: Path, node_name: str) -> List[str]:
    """Get direct non-preamble Tablet imports for one node."""
    lean_path = node_lean_path(repo, node_name)
    if not lean_path.exists():
        return []
    content = lean_path.read_text(encoding="utf-8", errors="replace")
    return [imp for imp in extract_tablet_imports(content) if imp != PREAMBLE_NAME]


def _get_direct_imports_from_reader(node_name: str, read_lean: Callable[[str], str]) -> List[str]:
    content = read_lean(node_name)
    if not content:
        return []
    return [imp for imp in extract_tablet_imports(content) if imp != PREAMBLE_NAME]


def _legacy_correspondence_fingerprint(repo: Path, node_name: str) -> Optional[str]:
    """Source-level fallback for repos without a Lean project.

    This is kept for synthetic/unit-test repos that have Tablet files but no lake project.
    Real formalization repos should use the Lean-aware fingerprint below.
    """
    tex_content = _read_file(node_tex_path(repo, node_name))
    lean_content = _read_file(node_lean_path(repo, node_name))
    tex_statement = _extract_tex_statement(tex_content)
    if not tex_statement or not lean_content.strip():
        return None

    parts = [
        "node:" + node_name,
        _hash_content(tex_statement),
        _hash_content(_extract_declaration_with_imports(lean_content)),
    ]

    all_deps = _get_recursive_imports(repo, node_name)
    all_deps.discard(node_name)
    for dep in sorted(all_deps):
        dep_tex = _extract_tex_statement(_read_file(node_tex_path(repo, dep)))
        dep_lean = _read_file(node_lean_path(repo, dep))
        if dep_tex and _tex_statement_is_definition(dep_tex):
            parts.append(f"dep_tex:{dep}:" + _hash_content(dep_tex))
        if dep_lean.strip():
            parts.append(f"dep_lean:{dep}:" + _hash_content(_extract_declaration_with_imports(dep_lean)))

    preamble = _read_file(repo / "Tablet" / "Preamble.lean")
    if preamble.strip():
        parts.append("preamble:" + _hash_content(preamble))

    return _hash_content("|".join(parts))


def legacy_correspondence_fingerprint(repo: Path, node_name: str) -> Optional[str]:
    """Expose the pre-Lean-aware correspondence fingerprint for migration logic.

    This is used only to recognize persisted legacy correspondence hashes and
    upgrade them in place to the current semantic fingerprint without reopening
    correspondence work purely because the hashing scheme changed.
    """
    return _legacy_correspondence_fingerprint(repo, node_name)


def _has_lake_project(repo: Path) -> bool:
    return (repo / "lakefile.lean").exists() or (repo / "lakefile.toml").exists()


def _lean_project_snapshot_key(repo: Path) -> Tuple[Tuple[str, str], ...]:
    """Cheap cache key for Lean semantic fingerprints.

    This is only an in-process cache, but it still should not be gameable by preserving mtimes
    or file sizes. So we key off file contents for the Lean/project files that can affect
    elaborated statement meaning.
    """
    paths: List[Path] = []
    tablet_dir = repo / "Tablet"
    if tablet_dir.exists():
        paths.extend(sorted(tablet_dir.glob("*.lean")))
    for extra in ("lakefile.lean", "lakefile.toml", "lean-toolchain"):
        p = repo / extra
        if p.exists():
            paths.append(p)
    snapshot: List[Tuple[str, str]] = []
    for path in paths:
        try:
            rel = str(path.relative_to(repo))
            snapshot.append((rel, _hash_content(path.read_text(encoding="utf-8", errors="replace"))))
        except OSError:
            continue
    return tuple(snapshot)


def _lean_fingerprint_script_path() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "lean_semantic_fingerprint.lean"


def _run_lean_correspondence_payloads(repo: Path, node_names: List[str]) -> Dict[str, Optional[str]]:
    """Return Lean semantic payloads for the requested nodes.

    The helper script runs inside the formalization repo via `lake env lean --run` and inspects
    each declaration's elaborated constant info. Each payload is a single-line canonical string
    that the Python side hashes together with the NL statement.
    """
    if not node_names:
        return {}

    script_path = _lean_fingerprint_script_path()
    if not script_path.exists():
        return {name: None for name in node_names}

    try:
        proc = subprocess.run(
            ["lake", "env", "lean", "--run", str(script_path), *node_names],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return {name: None for name in node_names}

    payloads: Dict[str, Optional[str]] = {name: None for name in node_names}
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        if line.startswith("FP\t"):
            _, node_name, payload = line.split("\t", 2)
            if node_name in payloads:
                payloads[node_name] = payload
        elif line.startswith("ERR\t"):
            _, node_name, _err = line.split("\t", 2)
            if node_name in payloads:
                payloads[node_name] = None
    return payloads


def _lean_semantic_statement_payload(repo: Path, node_name: str) -> Optional[str]:
    snapshot_key = _lean_project_snapshot_key(repo)
    cache_key = (str(repo.resolve()), snapshot_key, node_name)
    if cache_key in _LEAN_CORRESPONDENCE_CACHE:
        return _LEAN_CORRESPONDENCE_CACHE[cache_key]

    payloads = _run_lean_correspondence_payloads(repo, [node_name])
    payload = payloads.get(node_name)
    _LEAN_CORRESPONDENCE_CACHE[cache_key] = payload
    return payload


def prime_correspondence_fingerprints(repo: Path, node_names: List[str]) -> None:
    """Warm the Lean semantic fingerprint cache for a batch of nodes."""
    if not node_names or not _has_lake_project(repo):
        return
    snapshot_key = _lean_project_snapshot_key(repo)
    repo_key = str(repo.resolve())
    to_compute = [
        name for name in node_names
        if (repo_key, snapshot_key, name) not in _LEAN_CORRESPONDENCE_CACHE
    ]
    if not to_compute:
        return
    payloads = _run_lean_correspondence_payloads(repo, to_compute)
    for name in to_compute:
        _LEAN_CORRESPONDENCE_CACHE[(repo_key, snapshot_key, name)] = payloads.get(name)


class NLCache:
    """Manages the NL verification approval cache."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.soundness_verified: Set[str] = set()
        self.correspondence_verified: Set[str] = set()
        self._load()

    def _load(self):
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self.soundness_verified = set(data.get("soundness_verified", []))
                raw_corr = data.get("correspondence_verified", [])
                # Handle both old format (list of pairs) and new format (list of strings)
                self.correspondence_verified = set()
                for item in raw_corr:
                    if isinstance(item, str):
                        self.correspondence_verified.add(item)
                    # Skip old tuple format — will be re-verified
            except (json.JSONDecodeError, TypeError):
                pass

    def save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "soundness_verified": sorted(self.soundness_verified),
            "correspondence_verified": sorted(self.correspondence_verified),
        }
        self.cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def soundness_fingerprint(self, repo: Path, node_name: str) -> Optional[str]:
        return soundness_fingerprint(repo, node_name)

    def correspondence_fingerprint(self, repo: Path, node_name: str) -> Optional[str]:
        return correspondence_fingerprint(repo, node_name)

    def is_soundness_cached(self, repo: Path, node_name: str) -> bool:
        """Check if a node's NL proof soundness is cached."""
        fingerprint = self.soundness_fingerprint(repo, node_name)
        return fingerprint is not None and fingerprint in self.soundness_verified

    def is_correspondence_cached(self, repo: Path, node_name: str) -> bool:
        """Check if a node's Lean/NL correspondence is cached.

        Correspondence depends on:
        - the node's `.tex` statement block, excluding proof text
        - the Lean-elaborated semantic fingerprint of the node's own declaration
          (including any definition/inductive context actually referenced by the statement)
        """
        fingerprint = self.correspondence_fingerprint(repo, node_name)
        return fingerprint is not None and fingerprint in self.correspondence_verified

    def record_soundness_approval(self, repo: Path, node_names: List[str]):
        """Record that these nodes' NL proofs passed soundness verification."""
        for name in node_names:
            fp = self.soundness_fingerprint(repo, name)
            if fp:
                self.soundness_verified.add(fp)
        self.save()

    def record_correspondence_approval(self, repo: Path, node_names: List[str]):
        """Record that these nodes' Lean/NL correspondence passed."""
        for name in node_names:
            fp = self.correspondence_fingerprint(repo, name)
            if fp:
                self.correspondence_verified.add(fp)
        self.save()

    def filter_uncached(self, repo: Path, node_names: List[str], check_type: str) -> List[str]:
        """Return only the nodes that need verification (not cached)."""
        uncached = []
        for name in node_names:
            if check_type == "soundness":
                if not self.is_soundness_cached(repo, name):
                    uncached.append(name)
            elif check_type == "correspondence":
                if not self.is_correspondence_cached(repo, name):
                    uncached.append(name)
        return uncached


def soundness_fingerprint(repo: Path, node_name: str) -> Optional[str]:
    """Fingerprint the meaning-bearing NL proof context for one node.

    Soundness is about whether this node's NL proof follows from its direct
    children's NL statements. So the fingerprint includes:
    - the node's own full `.tex` content (statement + proof)
    - the direct child import list
    - each direct child's statement block, excluding proof text
    """
    tex_content = _read_file(node_tex_path(repo, node_name))
    if not tex_content.strip():
        return None

    direct_children = sorted(_get_direct_imports(repo, node_name))
    parts = [
        f"node:{node_name}",
        f"self_tex:{_hash_content(tex_content)}",
        "children:" + ",".join(direct_children),
    ]
    for child in direct_children:
        child_tex = _extract_tex_statement(_read_file(node_tex_path(repo, child)))
        if not child_tex:
            return None
        parts.append(f"child_stmt:{child}:{_hash_content(child_tex)}")
    return _hash_content("|".join(parts))


def correspondence_text_fingerprint(repo: Path, node_name: str) -> Optional[str]:
    """Conservative text-level fingerprint for correspondence invalidation.

    This tracks only the NL statement-level source context that should reopen
    correspondence work quickly:
    - the node's own `.tex` statement block
    - recursive imported nodes' `.tex` statement blocks, but only when those
      imported nodes are definitions

    Proof-only changes in `.tex` do not affect this fingerprint because only
    the statement block before `\\begin{proof}` is used. Lean-side drift is
    handled by the semantic correspondence fingerprint instead of this fast path.
    """
    tex_content = _read_file(node_tex_path(repo, node_name))
    tex_statement = _extract_tex_statement(tex_content)
    if not tex_statement:
        return None

    parts = [
        "node:" + node_name,
        "tex:" + _hash_content(tex_statement),
    ]

    all_deps = _get_recursive_imports(repo, node_name)
    all_deps.discard(node_name)
    for dep in sorted(all_deps):
        dep_tex = _extract_tex_statement(_read_file(node_tex_path(repo, dep)))
        if dep_tex and _tex_statement_is_definition(dep_tex):
            parts.append(f"dep_tex:{dep}:" + _hash_content(dep_tex))

    return _hash_content("|".join(parts))


def historical_correspondence_text_fingerprint(repo: Path, rev: str, node_name: str) -> Optional[str]:
    """Current fast correspondence text fingerprint evaluated at a git revision."""

    def read_tex(name: str) -> str:
        return _git_read_file(repo, rev, f"Tablet/{name}.tex")

    def read_lean(name: str) -> str:
        return _git_read_file(repo, rev, f"Tablet/{name}.lean")

    tex_statement = _extract_tex_statement(read_tex(node_name))
    if not tex_statement:
        return None

    parts = [
        "node:" + node_name,
        "tex:" + _hash_content(tex_statement),
    ]

    all_deps = _get_recursive_imports_from_reader(node_name, read_lean)
    all_deps.discard(node_name)
    for dep in sorted(all_deps):
        dep_tex = _extract_tex_statement(read_tex(dep))
        if dep_tex and _tex_statement_is_definition(dep_tex):
            parts.append(f"dep_tex:{dep}:" + _hash_content(dep_tex))

    return _hash_content("|".join(parts))


def legacy_correspondence_text_fingerprint(repo: Path, node_name: str) -> Optional[str]:
    """Expose the pre-semantic-change text fingerprint for migration logic."""
    tex_content = _read_file(node_tex_path(repo, node_name))
    tex_statement = _extract_tex_statement(tex_content)
    if not tex_statement:
        return None

    lean_content = _read_file(node_lean_path(repo, node_name))
    if not lean_content.strip():
        return None

    parts = [
        "node:" + node_name,
        "tex:" + _hash_content(tex_statement),
        "lean:" + _hash_content(_extract_meaning_bearing_lean_text(lean_content)),
    ]

    all_deps = _get_recursive_imports(repo, node_name)
    all_deps.discard(node_name)
    for dep in sorted(all_deps):
        dep_tex = _extract_tex_statement(_read_file(node_tex_path(repo, dep)))
        if dep_tex:
            parts.append(f"dep_tex:{dep}:" + _hash_content(dep_tex))
        dep_lean = _read_file(node_lean_path(repo, dep))
        if dep_lean.strip():
            parts.append(
                f"dep_lean:{dep}:" + _hash_content(_extract_meaning_bearing_lean_text(dep_lean))
            )

    preamble = _read_file(repo / "Tablet" / "Preamble.lean")
    if preamble.strip():
        parts.append("preamble:" + _hash_content(preamble))

    return _hash_content("|".join(parts))


def historical_legacy_correspondence_text_fingerprint(repo: Path, rev: str, node_name: str) -> Optional[str]:
    """Legacy text fingerprint evaluated at a git revision for migration checks."""

    def read_tex(name: str) -> str:
        return _git_read_file(repo, rev, f"Tablet/{name}.tex")

    def read_lean(name: str) -> str:
        return _git_read_file(repo, rev, f"Tablet/{name}.lean")

    tex_statement = _extract_tex_statement(read_tex(node_name))
    if not tex_statement:
        return None

    lean_content = read_lean(node_name)
    if not lean_content.strip():
        return None

    parts = [
        "node:" + node_name,
        "tex:" + _hash_content(tex_statement),
        "lean:" + _hash_content(_extract_meaning_bearing_lean_text(lean_content)),
    ]

    all_deps = _get_recursive_imports_from_reader(node_name, read_lean)
    all_deps.discard(node_name)
    for dep in sorted(all_deps):
        dep_tex = _extract_tex_statement(read_tex(dep))
        if dep_tex:
            parts.append(f"dep_tex:{dep}:" + _hash_content(dep_tex))
        dep_lean = read_lean(dep)
        if dep_lean.strip():
            parts.append(
                f"dep_lean:{dep}:" + _hash_content(_extract_meaning_bearing_lean_text(dep_lean))
            )

    preamble = _git_read_file(repo, rev, "Tablet/Preamble.lean")
    if preamble.strip():
        parts.append("preamble:" + _hash_content(preamble))

    return _hash_content("|".join(parts))


def previous_correspondence_text_fingerprint(repo: Path, node_name: str) -> Optional[str]:
    """Expose the immediately previous .tex-only-all-deps fingerprint for migration."""
    tex_content = _read_file(node_tex_path(repo, node_name))
    tex_statement = _extract_tex_statement(tex_content)
    if not tex_statement:
        return None

    parts = [
        "node:" + node_name,
        "tex:" + _hash_content(tex_statement),
    ]

    all_deps = _get_recursive_imports(repo, node_name)
    all_deps.discard(node_name)
    for dep in sorted(all_deps):
        dep_tex = _extract_tex_statement(_read_file(node_tex_path(repo, dep)))
        if dep_tex:
            parts.append(f"dep_tex:{dep}:" + _hash_content(dep_tex))

    return _hash_content("|".join(parts))


def correspondence_fingerprint(repo: Path, node_name: str) -> Optional[str]:
    """Compute a fingerprint for correspondence verification.

    This tracks statement-level meaning, not proof structure:
    - the node's `.tex` statement block, excluding proof text
    - a Lean-aware semantic fingerprint of the node's own declaration, built from the
      elaborated declaration type/value plus the definition/inductive context it actually uses

    That means proof-only changes and descendant theorem churn do not invalidate correspondence,
    but changes to definitions that the statement genuinely depends on do.
    """
    tex_content = _read_file(node_tex_path(repo, node_name))
    tex_statement = _extract_tex_statement(tex_content)
    if not tex_statement:
        return None

    if not _has_lake_project(repo):
        return _legacy_correspondence_fingerprint(repo, node_name)

    semantic_payload = _lean_semantic_statement_payload(repo, node_name)
    if semantic_payload is None:
        return None

    parts = [
        "node:" + node_name,
        "tex:" + _hash_content(tex_statement),
        "lean_semantic:" + _hash_content(semantic_payload),
    ]
    return _hash_content("|".join(parts))
