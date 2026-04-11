from __future__ import annotations

import unittest
from pathlib import Path


class TestSetupRepoScript(unittest.TestCase):
    def test_setup_requires_cache_get_to_succeed(self):
        text = Path("scripts/setup_repo.sh").read_text(encoding="utf-8")
        self.assertIn("lake exe cache get", text)
        self.assertNotIn("lake exe cache get >/dev/null 2>&1 || true", text)

    def test_setup_verifies_worker_mathlib_imports_with_example_file(self):
        text = Path("scripts/setup_repo.sh").read_text(encoding="utf-8")
        self.assertIn("lake env lean .agent-supervisor/scratch/example.lean", text)


if __name__ == "__main__":
    unittest.main()
