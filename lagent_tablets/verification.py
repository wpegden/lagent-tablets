"""Compatibility wrappers around the canonical deterministic checker.

Historically this module contained a separate implementation of deterministic
checking. The canonical logic now lives in ``lagent_tablets.check``. Keep thin
wrappers here only for backwards compatibility with older imports and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from lagent_tablets.check import (
    generate_check_node_sh,
    generate_check_tablet_sh,
    is_lake_package_error as _is_lake_package_error,
    write_scripts,
)
from lagent_tablets.check import check_node as _check_node_dict
from lagent_tablets.check import check_tablet as _check_tablet_dict
from lagent_tablets.config import FORBIDDEN_KEYWORDS_DEFAULT


@dataclass
class NodeCheckResult:
    """Compatibility dataclass view of the canonical node-check result."""

    name: str
    exists: bool = True
    compiles: bool = False
    sorry_free: bool = False
    keyword_clean: bool = False
    imports_valid: bool = False
    declaration_intact: bool = True
    axioms_valid: bool = True
    returncode: Optional[int] = None
    build_output: str = ""
    sorry_warnings: List[str] = field(default_factory=list)
    forbidden_hits: List[Dict[str, Any]] = field(default_factory=list)
    import_violations: List[str] = field(default_factory=list)
    axiom_violations: List[str] = field(default_factory=list)
    audited_axioms: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def closed(self) -> bool:
        return (
            self.compiles
            and self.sorry_free
            and self.keyword_clean
            and self.imports_valid
            and self.declaration_intact
            and self.axioms_valid
        )


@dataclass
class TabletCheckResult:
    """Compatibility dataclass view of the canonical tablet-check result."""

    nodes: Dict[str, NodeCheckResult] = field(default_factory=dict)
    build_ok: bool = False
    build_output: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return self.build_ok and all(r.closed for r in self.nodes.values()) and not self.errors


def check_node(
    repo_path: Path,
    name: str,
    *,
    allowed_prefixes: List[str],
    forbidden_keywords: List[str],
    expected_declaration_hash: str = "",
    timeout_seconds: float = 120.0,
    burst_user: Optional[str] = None,
    approved_axioms_path: Optional[Path] = None,
) -> NodeCheckResult:
    """Compatibility wrapper over ``lagent_tablets.check.check_node``."""
    result = _check_node_dict(
        repo_path,
        name,
        allowed_prefixes=allowed_prefixes,
        forbidden_keywords=forbidden_keywords,
        expected_declaration_hash=expected_declaration_hash,
        approved_axioms_path=approved_axioms_path,
        timeout_seconds=timeout_seconds,
    )
    return NodeCheckResult(
        name=name,
        exists=not any("not found" in err.lower() for err in result.get("errors", [])),
        compiles=bool(result.get("compiles", False)),
        sorry_free=bool(result.get("sorry_free", False)),
        keyword_clean=bool(result.get("keyword_clean", False)),
        imports_valid=bool(result.get("imports_valid", False)),
        declaration_intact=bool(result.get("declaration_intact", True)),
        axioms_valid=bool(result.get("axioms_valid", True)),
        build_output=str(result.get("build_output", "")),
        import_violations=list(result.get("import_violations", [])),
        axiom_violations=list(result.get("axiom_violations", [])),
        audited_axioms=list(result.get("audited_axioms", [])),
        error="; ".join(result.get("errors", [])),
    )


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
    approved_axioms_path: Optional[Path] = None,
) -> TabletCheckResult:
    """Compatibility wrapper over ``lagent_tablets.check.check_tablet``.

    ``node_names`` and ``declaration_hashes`` are kept in the signature for
    compatibility, but the canonical checker validates the full tablet and
    derives declaration expectations elsewhere.
    """
    result = _check_tablet_dict(
        repo_path,
        allowed_prefixes=allowed_prefixes,
        forbidden_keywords=forbidden_keywords,
        approved_axioms_path=approved_axioms_path,
        timeout_secs=timeout_seconds,
    )
    node_results = {
        name: NodeCheckResult(
            name=name,
            compiles=bool(data.get("compiles", False)),
            sorry_free=bool(data.get("sorry_free", False)),
            keyword_clean=bool(data.get("keyword_clean", False)),
            imports_valid=bool(data.get("imports_valid", False)),
            declaration_intact=bool(data.get("declaration_intact", True)),
            axioms_valid=bool(data.get("axioms_valid", True)),
            build_output=str(data.get("build_output", "")),
            import_violations=list(data.get("import_violations", [])),
            axiom_violations=list(data.get("axiom_violations", [])),
            audited_axioms=list(data.get("audited_axioms", [])),
            error="; ".join(data.get("errors", [])),
        )
        for name, data in result.get("nodes", {}).items()
    }
    build_ok = not any("lake build Tablet failed" in err for err in result.get("errors", []))
    return TabletCheckResult(
        nodes=node_results,
        build_ok=build_ok,
        build_output=str(result.get("build_output", "")),
        errors=list(result.get("errors", [])),
        warnings=list(result.get("warnings", [])),
    )


__all__ = [
    "FORBIDDEN_KEYWORDS_DEFAULT",
    "NodeCheckResult",
    "TabletCheckResult",
    "check_node",
    "check_tablet",
    "generate_check_node_sh",
    "generate_check_tablet_sh",
    "write_scripts",
    "_is_lake_package_error",
]
