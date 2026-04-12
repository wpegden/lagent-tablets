from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from lagent_tablets.prompt_action_catalog import (
    PROMPT_ACTION_SPECS,
    render_prompt_action_file,
    render_readme,
    write_prompt_action_catalog,
)


class TestPromptActionCatalog(TestCase):
    def test_every_prompt_has_action_spec(self):
        expected = {
            "correspondence_basic",
            "correspondence_full_context_multiple_changed_nodes",
            "correspondence_single_changed_node",
            "nl_proof_batch",
            "node_soundness_leaf",
            "node_soundness_with_children_and_previous_issues",
            "proof_reviewer_cleanup",
            "proof_reviewer_standard",
            "proof_worker_cleanup",
            "proof_worker_easy_local",
            "proof_worker_hard_coarse_restructure",
            "proof_worker_hard_local",
            "proof_worker_hard_restructure",
            "theorem_reviewer_invalid_with_reset_options",
            "theorem_reviewer_target_resolved",
            "theorem_reviewer_with_main_result_target_issues",
            "theorem_reviewer_with_unsupported_nodes",
            "theorem_worker_broad_initial_empty",
            "theorem_worker_broad_with_blockers_and_retry",
            "theorem_worker_target_repair",
            "theorem_worker_target_restructure",
            "verification_wrapper_compat",
        }
        self.assertEqual(set(PROMPT_ACTION_SPECS), expected)

    def test_rendered_prompt_action_file_has_expected_sections(self):
        spec = PROMPT_ACTION_SPECS["proof_worker_easy_local"]
        text = render_prompt_action_file("proof_worker_easy_local", spec)
        self.assertIn("**May Read / Consult**", text)
        self.assertIn("**Apparently Available Actions**", text)
        self.assertIn("**Prompt-Instructed Completion Steps**", text)
        self.assertIn("Source enforcement note", text)
        self.assertIn("Edit only the proof body", text)

    def test_writer_creates_readme_and_prompt_files(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "prompt_action_catalog"
        write_prompt_action_catalog(out)
        files = sorted(p.name for p in out.glob("*.md"))
        self.assertEqual(len(files), len(PROMPT_ACTION_SPECS) + 1)
        self.assertIn("README.md", files)
        self.assertIn("theorem_worker_broad_initial_empty.md", files)
        self.assertIn("proof_reviewer_standard.md", files)
        self.assertIn("Prompt Action Catalog", render_readme())
