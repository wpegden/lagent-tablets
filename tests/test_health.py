"""Tests for permission normalization helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lagent_tablets.health import (
    fix_lake_permissions,
    prepare_supervisor_read_surfaces,
    validate_supervisor_read_surfaces,
)


class TestLakePermissions(unittest.TestCase):

    def test_fix_lake_permissions_can_normalize_package_build_artifacts(self):
        repo = Path(tempfile.mkdtemp())
        pkg_build = repo / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "lib" / "lean"
        pkg_build.mkdir(parents=True, exist_ok=True)
        bad_file = pkg_build / "Basic.olean"
        bad_file.write_text("x", encoding="utf-8")
        bad_file.chmod(0o600)

        fix_lake_permissions(repo, include_package_builds=True)

        self.assertEqual(bad_file.stat().st_mode & 0o777, 0o664)
        self.assertTrue((repo / ".lake" / "packages" / "mathlib" / ".lake" / "build").stat().st_mode & 0o020)

    def test_prepare_supervisor_read_surfaces_repairs_restrictive_shared_files(self):
        repo = Path(tempfile.mkdtemp())
        state_dir = repo / ".agent-supervisor"
        tablet_dir = repo / "Tablet"
        staging_dir = state_dir / "staging"
        tablet_dir.mkdir(parents=True, exist_ok=True)
        staging_dir.mkdir(parents=True, exist_ok=True)

        lean_file = tablet_dir / "foo.lean"
        tex_file = tablet_dir / "foo.tex"
        tablet_root = repo / "Tablet.lean"
        raw_file = staging_dir / "worker_handoff.raw.json"

        lean_file.write_text("theorem foo : True := by\n  trivial\n", encoding="utf-8")
        tex_file.write_text("\\begin{lemma}True\\end{lemma}\n", encoding="utf-8")
        tablet_root.write_text("import Tablet.foo\n", encoding="utf-8")
        raw_file.write_text("{}\n", encoding="utf-8")

        for path in (lean_file, tablet_root, raw_file):
            path.chmod(0o600)

        errors = prepare_supervisor_read_surfaces(
            repo,
            state_dir,
            include_tablet=True,
            include_staging=True,
            include_package_builds=False,
        )

        self.assertEqual(errors, [])
        self.assertEqual(lean_file.stat().st_mode & 0o777, 0o664)
        self.assertEqual(tablet_root.stat().st_mode & 0o777, 0o664)
        self.assertEqual(raw_file.stat().st_mode & 0o777, 0o664)

    def test_validate_supervisor_read_surfaces_rejects_symlink_and_nested_directory(self):
        repo = Path(tempfile.mkdtemp())
        state_dir = repo / ".agent-supervisor"
        tablet_dir = repo / "Tablet"
        staging_dir = state_dir / "staging"
        tablet_dir.mkdir(parents=True, exist_ok=True)
        staging_dir.mkdir(parents=True, exist_ok=True)

        (repo / "Tablet.lean").write_text("import Tablet.foo\n", encoding="utf-8")
        (tablet_dir / "foo.lean").write_text("theorem foo : True := by\n  trivial\n", encoding="utf-8")
        (tablet_dir / "foo.tex").write_text("\\begin{lemma}True\\end{lemma}\n", encoding="utf-8")
        (tablet_dir / "nested").mkdir()
        target = tablet_dir / "foo.lean"
        (staging_dir / "bad.raw.json").symlink_to(target)

        errors = validate_supervisor_read_surfaces(repo, state_dir)

        self.assertTrue(any("Tablet/nested: unexpected nested directory" in err for err in errors))
        self.assertTrue(any(".agent-supervisor/staging/bad.raw.json: symlinks are not allowed" in err for err in errors))
