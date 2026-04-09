"""Tests for the deterministic check.py module."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lagent_tablets.check import (
    check_node,
    validate_correspondence_result_data,
    validate_node_soundness_result_data,
    validate_reviewer_decision_data,
    validate_worker_handoff_data,
)


class TestAxiomAudit(unittest.TestCase):

    def _make_repo(self, lean_text: str) -> Path:
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (tablet / "foo.lean").write_text(lean_text, encoding="utf-8")
        (tablet / "foo.tex").write_text("\\begin{theorem}Foo\\end{theorem}\n", encoding="utf-8")
        return repo

    def test_closed_node_rejects_unapproved_axioms(self):
        repo = self._make_repo("-- [TABLET NODE: foo]\ntheorem foo : True := by\n  trivial\n")

        with patch("lagent_tablets.check.run_lake_env_lean", return_value={"ok": True, "returncode": 0, "output": ""}):
            with patch(
                "lagent_tablets.check.run_print_axioms",
                return_value={"ok": True, "returncode": 0, "output": "'foo' depends on axioms: [propext, sorryAx]"},
            ):
                result = check_node(
                    repo,
                    "foo",
                    allowed_prefixes=["Mathlib"],
                    forbidden_keywords=["sorry", "axiom"],
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["axioms_valid"])
        self.assertEqual(result["axiom_violations"], ["sorryAx"])
        self.assertTrue(any("Axiom audit failed" in err for err in result["errors"]))

    def test_closed_node_accepts_repo_specific_axiom_override(self):
        repo = self._make_repo("-- [TABLET NODE: foo]\ntheorem foo : True := by\n  trivial\n")
        (repo / "APPROVED_AXIOMS.json").write_text(
            '{"nodes": {"foo": ["sorryAx"]}}',
            encoding="utf-8",
        )

        with patch("lagent_tablets.check.run_lake_env_lean", return_value={"ok": True, "returncode": 0, "output": ""}):
            with patch(
                "lagent_tablets.check.run_print_axioms",
                return_value={"ok": True, "returncode": 0, "output": "'foo' depends on axioms: [propext, sorryAx]"},
            ):
                result = check_node(
                    repo,
                    "foo",
                    allowed_prefixes=["Mathlib"],
                    forbidden_keywords=["sorry", "axiom"],
                    approved_axioms_path=repo / "APPROVED_AXIOMS.json",
                )

        self.assertTrue(result["ok"])
        self.assertTrue(result["axioms_valid"])
        self.assertEqual(result["axiom_violations"], [])

    def test_open_node_skips_axiom_audit(self):
        repo = self._make_repo("-- [TABLET NODE: foo]\ntheorem foo : True := by\n  sorry\n")

        with patch("lagent_tablets.check.run_lake_env_lean", return_value={"ok": True, "returncode": 0, "output": ""}):
            with patch("lagent_tablets.check.run_print_axioms") as mock_print_axioms:
                result = check_node(
                    repo,
                    "foo",
                    allowed_prefixes=["Mathlib"],
                    forbidden_keywords=["sorry"],
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["sorry_free"])
        mock_print_axioms.assert_not_called()


class TestArtifactValidation(unittest.TestCase):

    def test_validates_correspondence_result(self):
        result = validate_correspondence_result_data({
            "correspondence": {"decision": "PASS", "issues": []},
            "paper_faithfulness": {"decision": "FAIL", "issues": [{"node": "foo", "description": "Mismatch"}]},
            "overall": "REJECT",
            "summary": "Paper mismatch remains",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["overall"], "REJECT")

    def test_rejects_node_soundness_with_wrong_node(self):
        result = validate_node_soundness_result_data({
            "node": "bar",
            "soundness": {"decision": "SOUND", "explanation": "fine"},
            "overall": "APPROVE",
            "summary": "ok",
        }, node_name="foo")
        self.assertFalse(result["ok"])
        self.assertTrue(any("node must equal foo" in err for err in result["errors"]))

    def test_validates_theorem_reviewer_decision(self):
        result = validate_reviewer_decision_data({
            "decision": "CONTINUE",
            "reason": "Need to fix correspondence.",
            "next_prompt": "Fix the quantifier mismatch.",
            "next_active_node": "",
            "issues": ["Missing quantifier"],
            "paper_focus_ranges": [{"start_line": 10, "end_line": 12, "reason": "statement"}],
            "orphan_resolutions": [],
            "open_rejections": [{"node": "foo", "phase": "correspondence", "reason": "Missing quantifier"}],
        }, phase="theorem_stating")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["decision"], "CONTINUE")

    def test_validates_worker_handoff_new_nodes_exist(self):
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (tablet / "helper.lean").write_text("-- [TABLET NODE: helper]\ntheorem helper : True := by\n  trivial\n")
        (tablet / "helper.tex").write_text("\\begin{lemma}True\\end{lemma}\n")

        result = validate_worker_handoff_data({
            "summary": "Added helper",
            "status": "NOT_STUCK",
            "new_nodes": ["helper"],
            "difficulty_hints": {"helper": "easy"},
        }, phase="theorem_stating", repo=repo)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
