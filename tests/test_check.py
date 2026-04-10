"""Tests for the deterministic check.py module."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lagent_tablets.check import (
    check_proof_hard_scope,
    check_proof_worker_delta,
    check_node,
    check_tablet,
    check_tablet_scoped,
    run_print_axioms,
    validate_correspondence_result_data,
    validate_node_soundness_result_data,
    validate_reviewer_decision_data,
    validate_worker_handoff_data,
)
from lagent_tablets.state import TabletNode, TabletState, save_tablet
from lagent_tablets.verification import write_scripts


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

    def test_run_print_axioms_uses_supervisor_staging_dir(self):
        repo = self._make_repo("-- [TABLET NODE: foo]\ntheorem foo : True := by\n  trivial\n")
        (repo / ".agent-supervisor" / "staging").mkdir(parents=True, exist_ok=True)

        def fake_run(cmd, capture_output, text, cwd, timeout):
            self.assertEqual(Path(cwd), repo)
            self.assertEqual(cmd[:3], ["lake", "env", "lean"])
            self.assertTrue(cmd[3].startswith(".agent-supervisor/staging/axioms_foo_"))
            class Proc:
                returncode = 0
                stdout = "'foo' does not depend on any axioms"
                stderr = ""
            return Proc()

        with patch("lagent_tablets.check.subprocess.run", side_effect=fake_run):
            result = run_print_axioms(repo, "foo")

        self.assertTrue(result["ok"])

    def test_check_node_rejects_marker_mismatch(self):
        repo = self._make_repo("-- [TABLET NODE: bar]\ntheorem foo : True := by\n  trivial\n")

        with patch("lagent_tablets.check.run_lake_env_lean", return_value={"ok": True, "returncode": 0, "output": ""}):
            result = check_node(
                repo,
                "foo",
                allowed_prefixes=["Mathlib"],
                forbidden_keywords=["sorry", "axiom"],
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["marker_valid"])
        self.assertTrue(any("Marker says" in err for err in result["errors"]))

    def test_check_node_rejects_bad_tex_format(self):
        repo = self._make_repo("-- [TABLET NODE: foo]\ntheorem foo : True := by\n  trivial\n")
        (repo / "Tablet" / "foo.tex").write_text("\\begin{proof}\nBroken.\n\\end{proof}\n", encoding="utf-8")

        with patch("lagent_tablets.check.run_lake_env_lean", return_value={"ok": True, "returncode": 0, "output": ""}):
            result = check_node(
                repo,
                "foo",
                allowed_prefixes=["Mathlib"],
                forbidden_keywords=["sorry", "axiom"],
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["tex_format_valid"])
        self.assertTrue(any(".tex format errors" in err for err in result["errors"]))

    def test_check_node_warns_when_definition_like_roles_mismatch(self):
        repo = self._make_repo("-- [TABLET NODE: foo]\ntheorem foo : True := by\n  trivial\n")
        (repo / "Tablet" / "foo.tex").write_text(
            "\\begin{definition}[foo]\nFoo.\n\\end{definition}\n",
            encoding="utf-8",
        )

        with patch("lagent_tablets.check.run_lake_env_lean", return_value={"ok": True, "returncode": 0, "output": ""}):
            result = check_node(
                repo,
                "foo",
                allowed_prefixes=["Mathlib"],
                forbidden_keywords=["sorry", "axiom"],
            )

        self.assertTrue(any("disguised definitions" in warn for warn in result["warnings"]))

    def test_check_tablet_validates_preamble_tex_with_definitions(self):
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (tablet / "Preamble.lean").write_text("import Mathlib\n", encoding="utf-8")
        (tablet / "Preamble.tex").write_text(
            "\\begin{definition}[ambient]\nAmbient object.\n\\end{definition}\n"
            "\\begin{proposition}[notation]\nNotation convention.\n\\end{proposition}\n",
            encoding="utf-8",
        )
        (tablet / "foo.lean").write_text("-- [TABLET NODE: foo]\ntheorem foo : True := by\n  trivial\n", encoding="utf-8")
        (tablet / "foo.tex").write_text("\\begin{theorem}True\\end{theorem}\n", encoding="utf-8")

        with patch("lagent_tablets.check.run_lake_env_lean", return_value={"ok": True, "returncode": 0, "output": ""}), \
             patch("lagent_tablets.check.run_lake_build_tablet", return_value={"ok": True, "output": "", "returncode": 0}), \
             patch("lagent_tablets.check.run_print_axioms", return_value={"ok": True, "returncode": 0, "output": "'foo' does not depend on any axioms"}):
            result = check_tablet(
                repo,
                allowed_prefixes=["Mathlib"],
                forbidden_keywords=["sorry", "axiom"],
            )

        self.assertTrue(result["ok"])

    def test_check_tablet_rejects_tex_without_matching_lean(self):
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (tablet / "Preamble.lean").write_text("import Mathlib.Topology.Basic\n", encoding="utf-8")
        (tablet / "orphan.tex").write_text("\\begin{lemma}True\\end{lemma}\n", encoding="utf-8")

        with patch("lagent_tablets.check.run_lake_build_tablet", return_value={"ok": True, "output": "", "returncode": 0}):
            result = check_tablet(
                repo,
                allowed_prefixes=["Mathlib"],
                forbidden_keywords=["sorry", "axiom"],
            )

        self.assertFalse(result["ok"])
        self.assertTrue(any("every .tex node needs a matching .lean file" in err for err in result["errors"]))

    def test_check_proof_worker_delta_rejects_stray_tex_without_lean(self):
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (tablet / "main_thm.lean").write_text("-- [TABLET NODE: main_thm]\ntheorem main_thm : True := by\n  sorry\n", encoding="utf-8")
        (tablet / "main_thm.tex").write_text("\\begin{theorem}True\\end{theorem}\n", encoding="utf-8")
        before = {"main_thm.lean": "a", "main_thm.tex": "b"}
        (tablet / "helper.tex").write_text("\\begin{lemma}True\\end{lemma}\n", encoding="utf-8")

        result = check_proof_worker_delta(
            repo,
            active_node="main_thm",
            snapshot_before=before,
            existing_nodes=["main_thm"],
            allowed_prefixes=["Mathlib"],
            forbidden_keywords=["sorry", "axiom"],
        )

        self.assertEqual(result["outcome"], "INVALID")
        self.assertIn("Unpaired .tex files", result["errors"][0])

    def test_check_proof_hard_scope_allows_authorized_existing_node_in_restructure_mode(self):
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (tablet / "main_thm.lean").write_text("-- [TABLET NODE: main_thm]\ntheorem main_thm : True := by\n  sorry\n", encoding="utf-8")
        (tablet / "main_thm.tex").write_text("\\begin{theorem}True\\end{theorem}\n", encoding="utf-8")
        (tablet / "helper.lean").write_text("-- [TABLET NODE: helper]\ntheorem helper : True := by\n  sorry\n", encoding="utf-8")
        (tablet / "helper.tex").write_text("\\begin{lemma}True\\end{lemma}\n", encoding="utf-8")
        before = {
            "main_thm.lean": "a",
            "main_thm.tex": "b",
            "helper.lean": "c",
            "helper.tex": "d",
        }
        (tablet / "helper.lean").write_text("-- [TABLET NODE: helper]\ntheorem helper : True := by\n  trivial\n", encoding="utf-8")

        result = check_proof_hard_scope(
            repo,
            active_node="main_thm",
            snapshot_before=before,
            proof_edit_mode="restructure",
            authorized_nodes=["main_thm", "helper"],
        )

        self.assertTrue(result["ok"])

    def test_check_proof_hard_scope_rejects_coarse_tex_change_without_coarse_restructure(self):
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (repo / ".agent-supervisor").mkdir()
        (tablet / "main_thm.lean").write_text(
            "-- [TABLET NODE: main_thm]\ntheorem main_thm : True := by\n  trivial\n",
            encoding="utf-8",
        )
        (tablet / "main_thm.tex").write_text("\\begin{theorem}True\\end{theorem}\n", encoding="utf-8")
        save_tablet(
            repo / ".agent-supervisor" / "tablet.json",
            TabletState(nodes={
                "main_thm": TabletNode(
                    name="main_thm",
                    kind="paper_main_result",
                    status="closed",
                    coarse=True,
                    coarse_content_hash="stable",
                ),
            }),
        )
        before = {"main_thm.lean": "a", "main_thm.tex": "b"}
        (tablet / "main_thm.tex").write_text("\\begin{theorem}False\\end{theorem}\n", encoding="utf-8")

        result = check_proof_hard_scope(
            repo,
            active_node="main_thm",
            snapshot_before=before,
            proof_edit_mode="local",
        )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("Accepted coarse nodes may not change their `.tex` files" in err for err in result["errors"])
        )

    def test_check_proof_hard_scope_allows_active_coarse_proof_change(self):
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (repo / ".agent-supervisor").mkdir()
        (tablet / "main_thm.lean").write_text(
            "-- [TABLET NODE: main_thm]\ntheorem main_thm : True := by\n  sorry\n",
            encoding="utf-8",
        )
        (tablet / "main_thm.tex").write_text("\\begin{theorem}True\\end{theorem}\n", encoding="utf-8")
        save_tablet(
            repo / ".agent-supervisor" / "tablet.json",
            TabletState(nodes={
                "main_thm": TabletNode(
                    name="main_thm",
                    kind="paper_main_result",
                    status="open",
                    coarse=True,
                    coarse_content_hash="stable",
                ),
            }),
        )
        before = {
            "main_thm.lean": hashlib.sha256((tablet / "main_thm.lean").read_bytes()).hexdigest(),
            "main_thm.tex": hashlib.sha256((tablet / "main_thm.tex").read_bytes()).hexdigest(),
        }
        (tablet / "main_thm.lean").write_text(
            "-- [TABLET NODE: main_thm]\ntheorem main_thm : True := by\n  trivial\n",
            encoding="utf-8",
        )

        with patch("lagent_tablets.tablet.coarse_interface_fingerprint", return_value="stable"):
            result = check_proof_hard_scope(
                repo,
                active_node="main_thm",
                snapshot_before=before,
                proof_edit_mode="local",
            )

        self.assertTrue(result["ok"])


class TestWriteScripts(unittest.TestCase):

    def test_generated_check_py_bootstraps_package_root(self):
        repo = Path(tempfile.mkdtemp())
        state_dir = repo / ".agent-supervisor"
        write_scripts(
            repo,
            state_dir,
            allowed_prefixes=["Mathlib"],
            forbidden_keywords=["sorry"],
        )
        generated = (state_dir / "scripts" / "check.py").read_text(encoding="utf-8")
        self.assertIn("_src_root =", generated)
        self.assertIn("sys.path", generated)


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
            "target_edit_mode": "repair",
            "next_active_node": "",
            "issues": ["Missing quantifier"],
            "kind_assignments": {"main_result": "paper_main_result"},
            "paper_focus_ranges": [{"start_line": 10, "end_line": 12, "reason": "statement"}],
            "orphan_resolutions": [],
            "open_blockers": [{"node": "foo", "phase": "correspondence", "reason": "Missing quantifier"}],
        }, phase="theorem_stating")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["decision"], "CONTINUE")
        self.assertEqual(result["data"]["open_blockers"][0]["phase"], "correspondence")
        self.assertEqual(result["data"]["kind_assignments"]["main_result"], "paper_main_result")

    def test_validates_theorem_reviewer_decision_with_soundness_blocker(self):
        result = validate_reviewer_decision_data({
            "decision": "CONTINUE",
            "reason": "Need to fix the target proof.",
            "next_prompt": "Repair the NL proof.",
            "target_edit_mode": "repair",
            "next_active_node": "",
            "issues": ["Soundness gap remains"],
            "paper_focus_ranges": [],
            "orphan_resolutions": [],
            "open_blockers": [{"node": "foo", "phase": "soundness", "reason": "Proof does not yet derive the claim from its children."}],
        }, phase="theorem_stating")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["open_blockers"][0]["phase"], "soundness")

    def test_rejects_invalid_theorem_target_edit_mode(self):
        result = validate_reviewer_decision_data({
            "decision": "CONTINUE",
            "reason": "Need to restructure.",
            "next_prompt": "Add a missing intermediate.",
            "target_edit_mode": "wide_open",
            "next_active_node": "",
            "issues": ["Need richer DAG structure"],
            "paper_focus_ranges": [],
            "orphan_resolutions": [],
            "open_blockers": [],
        }, phase="theorem_stating")
        self.assertFalse(result["ok"])
        self.assertTrue(any("target_edit_mode must be one of" in err for err in result["errors"]))

    def test_validates_proof_reviewer_decision_with_proof_edit_mode(self):
        result = validate_reviewer_decision_data({
            "decision": "CONTINUE",
            "reason": "Need a broader local refactor.",
            "next_prompt": "Keep the same node and widen scope.",
            "next_active_node": "main_thm",
            "paper_focus_ranges": [],
            "difficulty_assignments": {},
            "elevate_to_hard": [],
            "proof_edit_mode": "restructure",
        }, phase="proof_formalization")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["proof_edit_mode"], "restructure")

    def test_validates_proof_reviewer_decision_with_coarse_restructure_mode(self):
        result = validate_reviewer_decision_data({
            "decision": "CONTINUE",
            "reason": "The accepted coarse package itself must change.",
            "next_prompt": "Keep the same node and authorize a coarse restructure.",
            "next_active_node": "main_thm",
            "paper_focus_ranges": [],
            "difficulty_assignments": {},
            "elevate_to_hard": [],
            "proof_edit_mode": "coarse_restructure",
        }, phase="proof_formalization")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["proof_edit_mode"], "coarse_restructure")

    def test_validates_cleanup_reviewer_decision(self):
        result = validate_reviewer_decision_data({
            "decision": "DONE",
            "reason": "Cleanup is complete.",
            "next_prompt": "",
            "next_active_node": "",
            "paper_focus_ranges": [],
        }, phase="proof_complete_style_cleanup")
        self.assertTrue(result["ok"])

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
            "kind_hints": {"helper": "paper_intermediate"},
        }, phase="theorem_stating", repo=repo)
        self.assertTrue(result["ok"])

    def test_worker_handoff_rejects_kind_hints_outside_new_nodes(self):
        result = validate_worker_handoff_data({
            "summary": "Added helper",
            "status": "NOT_STUCK",
            "new_nodes": ["helper"],
            "difficulty_hints": {"helper": "easy"},
            "kind_hints": {"main_thm": "paper_main_result"},
        }, phase="theorem_stating")
        self.assertFalse(result["ok"])
        self.assertTrue(any("kind_hints keys must be listed in new_nodes" in err for err in result["errors"]))

    def test_validates_cleanup_worker_handoff(self):
        result = validate_worker_handoff_data({
            "summary": "Tidied imports and comments.",
            "status": "DONE",
            "new_nodes": [],
        }, phase="proof_complete_style_cleanup")
        self.assertTrue(result["ok"])

    def test_scoped_tablet_check_ignores_unrelated_baseline_debt(self):
        repo = Path(tempfile.mkdtemp())
        tablet = repo / "Tablet"
        tablet.mkdir()
        (tablet / "target.lean").write_text("-- [TABLET NODE: target]\ntheorem target : True := by\n  trivial\n")
        (tablet / "target.tex").write_text("\\begin{theorem}True\\end{theorem}\n")
        (tablet / "other.lean").write_text("-- [TABLET NODE: other]\ntheorem other : True := by\n  trivial\n")
        (tablet / "other.tex").write_text("\\begin{theorem}True\\end{theorem}\n")

        with patch(
            "lagent_tablets.check.check_tablet",
            return_value={
                "ok": False,
                "errors": [
                    "other: .tex format errors: ['Missing statement environment (theorem/lemma/definition/proposition)']",
                    "target: Compilation failed",
                ],
                "warnings": [],
                "build_output": "",
            },
        ):
            result = check_tablet_scoped(
                repo,
                allowed_prefixes=["Mathlib"],
                forbidden_keywords=["sorry", "axiom"],
                baseline_errors=[
                    "other: .tex format errors: ['Missing statement environment (theorem/lemma/definition/proposition)']",
                ],
                allowed_nodes=["target"],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"], ["target: Compilation failed"])


if __name__ == "__main__":
    unittest.main()
