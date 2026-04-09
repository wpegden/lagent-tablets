"""Tests for verification module."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lagent_tablets.verification import (
    FORBIDDEN_KEYWORDS_DEFAULT,
    NodeCheckResult,
    generate_check_node_sh,
    generate_check_tablet_sh,
    write_scripts,
)


class TestNodeCheckResult(unittest.TestCase):

    def test_closed_when_all_pass(self):
        r = NodeCheckResult(
            name="foo",
            compiles=True,
            sorry_free=True,
            keyword_clean=True,
            imports_valid=True,
            declaration_intact=True,
        )
        self.assertTrue(r.closed)

    def test_not_closed_with_sorry(self):
        r = NodeCheckResult(
            name="foo",
            compiles=True,
            sorry_free=False,
            keyword_clean=True,
            imports_valid=True,
        )
        self.assertFalse(r.closed)

    def test_not_closed_with_bad_imports(self):
        r = NodeCheckResult(
            name="foo",
            compiles=True,
            sorry_free=True,
            keyword_clean=True,
            imports_valid=False,
        )
        self.assertFalse(r.closed)

    def test_not_closed_with_forbidden_keyword(self):
        r = NodeCheckResult(
            name="foo",
            compiles=True,
            sorry_free=True,
            keyword_clean=False,
            imports_valid=True,
        )
        self.assertFalse(r.closed)


class TestScriptGeneration(unittest.TestCase):

    def test_default_forbidden_keywords_include_hardening_entries(self):
        self.assertIn("partial", FORBIDDEN_KEYWORDS_DEFAULT)
        self.assertIn("implemented_by", FORBIDDEN_KEYWORDS_DEFAULT)
        self.assertIn("macro", FORBIDDEN_KEYWORDS_DEFAULT)
        self.assertIn("#eval", FORBIDDEN_KEYWORDS_DEFAULT)

    def test_check_node_sh_is_valid_bash(self):
        repo = Path(tempfile.mkdtemp())
        state = Path(tempfile.mkdtemp())
        script = generate_check_node_sh(
            repo, state,
            allowed_prefixes=["Mathlib"],
            forbidden_keywords=FORBIDDEN_KEYWORDS_DEFAULT,
        )
        self.assertIn("#!/bin/bash", script)
        self.assertIn("check.py", script)
        self.assertIn(" node ", script)
        self.assertIn(str(repo), script)

    def test_check_tablet_sh_is_valid_bash(self):
        repo = Path(tempfile.mkdtemp())
        state = Path(tempfile.mkdtemp())
        script = generate_check_tablet_sh(
            repo, state,
            allowed_prefixes=["Mathlib"],
            forbidden_keywords=FORBIDDEN_KEYWORDS_DEFAULT,
        )
        self.assertIn("#!/bin/bash", script)
        self.assertIn("check.py", script)
        self.assertIn(" tablet ", script)

    def test_write_scripts_creates_files(self):
        repo = Path(tempfile.mkdtemp())
        state = Path(tempfile.mkdtemp())
        write_scripts(
            repo, state,
            allowed_prefixes=["Mathlib"],
            forbidden_keywords=FORBIDDEN_KEYWORDS_DEFAULT,
        )
        self.assertTrue((state / "scripts" / "check_node.sh").exists())
        self.assertTrue((state / "scripts" / "check_tablet.sh").exists())
        # Check they're executable
        import os
        self.assertTrue(os.access(state / "scripts" / "check_node.sh", os.X_OK))

    def test_scripts_include_custom_prefixes(self):
        repo = Path(tempfile.mkdtemp())
        state = Path(tempfile.mkdtemp())
        script = generate_check_node_sh(
            repo, state,
            allowed_prefixes=["Mathlib", "MyCustomLib"],
            forbidden_keywords=["sorry", "axiom"],
        )
        self.assertIn("check.py", script)


if __name__ == "__main__":
    unittest.main()
