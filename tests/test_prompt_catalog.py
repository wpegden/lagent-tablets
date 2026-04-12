from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lagent_tablets.prompt_catalog import (
    REPO_SENTINEL,
    SCENARIOS,
    generate_prompt_catalog,
    main,
)


class TestPromptCatalog(unittest.TestCase):
    def test_generate_prompt_catalog_writes_every_registered_scenario(self):
        output_dir = Path(tempfile.mkdtemp()) / "prompt_catalog"
        written = generate_prompt_catalog(output_dir)

        expected = {"README.md", *[scenario.filename for scenario in SCENARIOS]}
        actual = {path.name for path in written}
        self.assertEqual(actual, expected)

        for name in expected:
            path = output_dir / name
            self.assertTrue(path.exists(), name)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.strip(), name)
            self.assertNotIn("lagent-prompt-catalog.", text)

        sample = (output_dir / "proof_worker_easy_local.md").read_text(encoding="utf-8")
        self.assertIn(REPO_SENTINEL, sample)
        self.assertIn("Builder: `build_worker_prompt`", sample)
        self.assertIn("YOUR ROLE: **Worker** (proof_formalization phase, EASY node)", sample)
        self.assertIn("| bound_corollary | corollary | open | easy |", sample)
        self.assertNotIn("Only `sorry` in theorem/lemma proof bodies is allowed.", sample)

        cleanup_worker = (output_dir / "proof_worker_cleanup.md").read_text(encoding="utf-8")
        self.assertIn("Tablet: 7/7 nodes closed", cleanup_worker)
        self.assertNotIn("Floating note", cleanup_worker)

        cleanup_reviewer = (output_dir / "proof_reviewer_cleanup.md").read_text(encoding="utf-8")
        self.assertIn("Tablet: 7/7 nodes closed", cleanup_reviewer)
        self.assertNotIn("Unsupported nodes exist", cleanup_reviewer)

        target_issues = (output_dir / "theorem_reviewer_with_main_result_target_issues.md").read_text(encoding="utf-8")
        self.assertIn("cor:bound: not yet covered by any non-helper node", target_issues)
        self.assertIn("Configured main-result target `cor:bound` is not covered by any non-helper node.", target_issues)

        correspondence = (output_dir / "correspondence_basic.md").read_text(encoding="utf-8")
        self.assertIn("If a `definition` node includes structured paper provenance", correspondence)
        self.assertIn("`sorry` is only allowed in proof-bearing theorem-like declarations", correspondence)

    def test_cli_main_accepts_custom_output_dir(self):
        output_dir = Path(tempfile.mkdtemp()) / "catalog"
        code = main(["--output-dir", str(output_dir)])
        self.assertEqual(code, 0)
        self.assertTrue((output_dir / "README.md").exists())


if __name__ == "__main__":
    unittest.main()
