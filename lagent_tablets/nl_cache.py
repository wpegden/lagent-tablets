"""NL verification approval cache.

Two content-addressed sets:
- soundness_verified: set of .tex content hashes. A node's NL proof soundness
  is cached if its .tex hash AND all recursive dependency .tex hashes are in the set.
- correspondence_verified: set of (tex_hash, lean_hash) tuples. Cached only if
  both the .tex and .lean are unchanged.

This avoids re-running expensive verification agents when nothing changed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Set

from lagent_tablets.tablet import extract_tablet_imports, node_lean_path, node_tex_path, PREAMBLE_NAME


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


def _read_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


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

    def is_soundness_cached(self, repo: Path, node_name: str) -> bool:
        """Check if a node's NL proof soundness is cached.

        Returns True only if the node's .tex hash AND all recursive
        dependency .tex hashes are in the verified set.
        """
        all_nodes = _get_recursive_imports(repo, node_name)
        for name in all_nodes:
            tex_content = _read_file(node_tex_path(repo, name))
            if not tex_content.strip():
                return False
            h = _hash_content(tex_content)
            if h not in self.soundness_verified:
                return False
        return True

    def is_correspondence_cached(self, repo: Path, node_name: str) -> bool:
        """Check if a node's Lean/NL correspondence is cached.

        Correspondence depends on: the node's .tex, its .lean declaration,
        AND all recursively imported .lean declarations (since definitions
        in imports affect the meaning of the statement).

        We hash all of these together into a single fingerprint.
        """
        fingerprint = self._correspondence_fingerprint(repo, node_name)
        return fingerprint is not None and fingerprint in self.correspondence_verified

    def _correspondence_fingerprint(self, repo: Path, node_name: str) -> Optional[str]:
        """Compute a fingerprint for correspondence verification.

        Includes everything the correspondence agent sees:
        - The node's .tex and .lean (declaration only)
        - All recursively imported nodes' .tex and .lean (declaration only)
        - Preamble.lean (definitions that affect meaning)
        """
        tex_content = _read_file(node_tex_path(repo, node_name))
        lean_content = _read_file(node_lean_path(repo, node_name))
        if not tex_content.strip() or not lean_content.strip():
            return None

        parts = [
            "node:" + node_name,
            _hash_content(tex_content),
            _hash_content(_extract_declaration_with_imports(lean_content)),
        ]

        # Recursively imported nodes' .tex and .lean declarations
        all_deps = _get_recursive_imports(repo, node_name)
        all_deps.discard(node_name)
        for dep in sorted(all_deps):
            dep_tex = _read_file(node_tex_path(repo, dep))
            dep_lean = _read_file(node_lean_path(repo, dep))
            if dep_tex.strip():
                parts.append(f"dep_tex:{dep}:" + _hash_content(dep_tex))
            if dep_lean.strip():
                parts.append(f"dep_lean:{dep}:" + _hash_content(_extract_declaration_with_imports(dep_lean)))

        # Preamble (definitions)
        preamble = _read_file(repo / "Tablet" / "Preamble.lean")
        if preamble.strip():
            parts.append("preamble:" + _hash_content(preamble))

        return _hash_content("|".join(parts))

    def record_soundness_approval(self, repo: Path, node_names: List[str]):
        """Record that these nodes' NL proofs passed soundness verification."""
        for name in node_names:
            all_nodes = _get_recursive_imports(repo, name)
            for dep_name in all_nodes:
                tex_content = _read_file(node_tex_path(repo, dep_name))
                if tex_content.strip():
                    self.soundness_verified.add(_hash_content(tex_content))
        self.save()

    def record_correspondence_approval(self, repo: Path, node_names: List[str]):
        """Record that these nodes' Lean/NL correspondence passed."""
        for name in node_names:
            fp = self._correspondence_fingerprint(repo, name)
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
