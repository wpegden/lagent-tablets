from __future__ import annotations

import unittest
from pathlib import Path


class TestSetupRepoScript(unittest.TestCase):
    def test_setup_supports_yes_flag_and_target_confirmation(self):
        text = Path("scripts/setup_repo.sh").read_text(encoding="utf-8")
        self.assertIn("--yes", text)
        self.assertIn("Resolved main-result targets:", text)
        self.assertIn("Proceed with these targets? [y/N]", text)

    def test_setup_requires_cache_get_to_succeed(self):
        text = Path("scripts/setup_repo.sh").read_text(encoding="utf-8")
        self.assertIn("lake exe cache get", text)
        self.assertNotIn("lake exe cache get >/dev/null 2>&1 || true", text)

    def test_setup_verifies_worker_mathlib_imports_with_example_file(self):
        text = Path("scripts/setup_repo.sh").read_text(encoding="utf-8")
        self.assertIn("lake env lean .agent-supervisor/scratch/example.lean", text)

    def test_setup_prewarms_as_supervisor_and_revalidates_as_worker_after_permission_fix(self):
        text = Path("scripts/setup_repo.sh").read_text(encoding="utf-8")
        self.assertIn('echo "  Prewarming Lean dependencies and build artifacts as supervisor user..."', text)
        self.assertIn('echo "  Fixed shared Lean build permissions for supervisor access"', text)
        self.assertIn("lake env lean .agent-supervisor/scratch/example.lean", text)
        worker_validation_idx = text.rindex("lake env lean .agent-supervisor/scratch/example.lean")
        self.assertLess(
            text.index('echo "  Prewarming Lean dependencies and build artifacts as supervisor user..."'),
            text.index('echo "  Fixed shared Lean build permissions for supervisor access"'),
        )
        self.assertLess(
            text.index('echo "  Fixed shared Lean build permissions for supervisor access"'),
            worker_validation_idx,
        )
        self.assertLess(
            worker_validation_idx,
            text.index('python3 "$REPO/.agent-supervisor/scripts/check.py" tablet "$REPO"'),
        )


if __name__ == "__main__":
    unittest.main()
