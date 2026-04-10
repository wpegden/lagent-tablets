"""Tests for permission normalization helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lagent_tablets.health import fix_lake_permissions


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
