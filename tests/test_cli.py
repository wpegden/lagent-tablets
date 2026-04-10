from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lagent_tablets.cli import (
    _trusted_main_result_review_issues,
    should_stop,
)
from lagent_tablets.cycle import CycleOutcome
from lagent_tablets.state import SupervisorState, TabletNode, TabletState


class TestHumanInputHandling(unittest.TestCase):

    def _config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            state_dir=root,
            repo_path=root,
            max_cycles=0,
        )

    def test_advance_phase_approval_captures_trusted_main_result_hashes(self):
        root = Path(tempfile.mkdtemp())
        (root / "human_approve.json").write_text("{}", encoding="utf-8")
        config = self._config(root)
        state = SupervisorState(
            cycle=8,
            phase="theorem_stating",
            last_review={"decision": "ADVANCE_PHASE"},
        )
        tablet = TabletState(nodes={
            "main": TabletNode(name="main", kind="paper_main_result", status="open"),
        })

        with patch("lagent_tablets.cli._capture_trusted_main_result_hashes", return_value={"main": "fp-main"}):
            stop = should_stop(config, state, tablet, CycleOutcome("CONTINUE", "ok"))

        self.assertFalse(stop)
        self.assertEqual(state.phase, "proof_formalization")
        self.assertEqual(state.trusted_main_result_hashes, {"main": "fp-main"})
        self.assertIsNone(state.last_review)

    def test_need_input_feedback_is_consumed(self):
        root = Path(tempfile.mkdtemp())
        (root / "human_feedback.json").write_text(json.dumps({"feedback": "Please restore the original statement."}), encoding="utf-8")
        config = self._config(root)
        state = SupervisorState(
            cycle=9,
            phase="proof_formalization",
            last_review={"decision": "NEED_INPUT", "reason": "Need human review."},
        )
        tablet = TabletState()

        stop = should_stop(config, state, tablet, CycleOutcome("CONTINUE", "ok"))

        self.assertFalse(stop)
        self.assertEqual(state.human_input, "Please restore the original statement.")
        self.assertEqual(state.last_review["decision"], "CONTINUE")
        self.assertFalse(state.awaiting_human_input)

    def test_need_input_approval_retrusts_main_results_for_trust_gate(self):
        root = Path(tempfile.mkdtemp())
        (root / "human_approve.json").write_text("{}", encoding="utf-8")
        config = self._config(root)
        state = SupervisorState(
            cycle=12,
            phase="proof_formalization",
            last_review={
                "decision": "NEED_INPUT",
                "human_gate": "paper_main_result_correspondence",
                "reason": "Trusted paper main results drifted.",
            },
            trusted_main_result_hashes={"main": "old"},
        )
        tablet = TabletState(nodes={
            "main": TabletNode(name="main", kind="paper_main_result", status="closed"),
        })

        with patch("lagent_tablets.cli._capture_trusted_main_result_hashes", return_value={"main": "new"}):
            stop = should_stop(config, state, tablet, CycleOutcome("CONTINUE", "ok"))

        self.assertFalse(stop)
        self.assertEqual(state.trusted_main_result_hashes, {"main": "new"})
        self.assertEqual(state.last_review["decision"], "CONTINUE")
        self.assertFalse(state.awaiting_human_input)

    def test_proof_completion_enters_cleanup_with_last_good_ref(self):
        root = Path(tempfile.mkdtemp())
        config = self._config(root)
        state = SupervisorState(cycle=14, phase="proof_formalization")
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main": TabletNode(name="main", kind="paper_main_result", status="closed"),
        })

        stop = should_stop(config, state, tablet, CycleOutcome("PROGRESS", "all done"))

        self.assertFalse(stop)
        self.assertEqual(state.phase, "proof_complete_style_cleanup")
        self.assertEqual(state.cleanup_last_good_commit, "cycle-14")


class TestTrustedMainResultReviewIssues(unittest.TestCase):

    def _config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            state_dir=root,
            repo_path=root,
            max_cycles=0,
        )

    def test_detects_changed_added_and_removed_main_results(self):
        root = Path(tempfile.mkdtemp())
        config = self._config(root)
        state = SupervisorState(
            trusted_main_result_hashes={
                "main": "old-main",
                "gone": "old-gone",
            }
        )
        tablet = TabletState(nodes={
            "main": TabletNode(name="main", kind="paper_main_result", status="closed"),
            "extra": TabletNode(name="extra", kind="paper_main_result", status="open"),
        })

        def fake_fp(_repo: Path, node_name: str) -> str:
            if node_name == "main":
                return "new-main"
            if node_name == "extra":
                return "fp-extra"
            raise AssertionError(f"unexpected node: {node_name}")

        with patch("lagent_tablets.nl_cache.NLCache.correspondence_fingerprint", autospec=True, side_effect=lambda self, repo, node_name: fake_fp(repo, node_name)):
            issues = _trusted_main_result_review_issues(config, state, tablet)

        self.assertEqual(
            issues,
            [
                "gone: removed from the main-result set after human review",
                "extra: newly classified as a paper main result after human review",
                "main: correspondence changed since the last human-reviewed package",
            ],
        )
