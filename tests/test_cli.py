from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lagent_tablets.cli import (
    _trusted_main_result_label_review_issues,
    main,
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
            workflow=SimpleNamespace(main_result_labels=["main"]),
        )

    def _write_paper_statement_node(
        self,
        root: Path,
        name: str,
        *,
        env: str = "theorem",
        paper_tex: str = "\\begin{theorem}\\label{main}\nMain statement.\n\\end{theorem}\n",
    ) -> None:
        (root / "paper.tex").write_text(paper_tex, encoding="utf-8")
        tablet_dir = root / "Tablet"
        tablet_dir.mkdir(exist_ok=True)
        (tablet_dir / f"{name}.lean").write_text(
            f"import Tablet.Preamble\n\n-- [TABLET NODE: {name}]\n\ntheorem {name} : True := by\n  trivial\n",
            encoding="utf-8",
        )
        (tablet_dir / f"{name}.tex").write_text(
            f"\\begin{{{env}}}\nTrue.\n\\end{{{env}}}\n\\begin{{proof}}\nProof.\n\\end{{proof}}\n",
            encoding="utf-8",
        )

    def test_advance_phase_approval_captures_trusted_main_result_hashes(self):
        root = Path(tempfile.mkdtemp())
        (root / "human_approve.json").write_text("{}", encoding="utf-8")
        self._write_paper_statement_node(root, "main")
        config = self._config(root)
        state = SupervisorState(
            cycle=8,
            phase="theorem_stating",
            last_review={"decision": "ADVANCE_PHASE"},
        )
        tablet = TabletState(nodes={
            "main": TabletNode(
                name="main",
                kind="ordinary",
                status="open",
                paper_provenance={"start_line": 1, "end_line": 3, "tex_label": "main"},
            ),
        })

        with patch(
            "lagent_tablets.cli._capture_trusted_main_result_target_state",
            return_value={"main": {"nodes": ["main"], "fingerprint": "fp-main"}},
        ), \
             patch("lagent_tablets.cli.freeze_current_coarse_package") as mock_freeze:
            stop = should_stop(config, state, tablet, CycleOutcome("CONTINUE", "ok"))

        self.assertFalse(stop)
        self.assertEqual(state.phase, "proof_formalization")
        self.assertEqual(
            state.trusted_main_result_target_state,
            {"main": {"nodes": ["main"], "fingerprint": "fp-main"}},
        )
        self.assertIsNone(state.last_review)
        mock_freeze.assert_called_once_with(tablet, config.repo_path, cycle=state.cycle)

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
        self._write_paper_statement_node(root, "main")
        config = self._config(root)
        state = SupervisorState(
            cycle=12,
            phase="proof_formalization",
            last_review={
                "decision": "NEED_INPUT",
                "human_gate": "main_result_target_correspondence",
                "reason": "Trusted main-result targets drifted.",
            },
            trusted_main_result_target_state={
                "main": {"nodes": ["main"], "fingerprint": "old"},
            },
        )
        tablet = TabletState(nodes={
            "main": TabletNode(
                name="main",
                kind="ordinary",
                status="closed",
                paper_provenance={"start_line": 1, "end_line": 3, "tex_label": "main"},
            ),
        })

        with patch(
            "lagent_tablets.cli._capture_trusted_main_result_target_state",
            return_value={"main": {"nodes": ["main"], "fingerprint": "new"}},
        ):
            stop = should_stop(config, state, tablet, CycleOutcome("CONTINUE", "ok"))

        self.assertFalse(stop)
        self.assertEqual(
            state.trusted_main_result_target_state,
            {"main": {"nodes": ["main"], "fingerprint": "new"}},
        )
        self.assertEqual(state.last_review["decision"], "CONTINUE")
        self.assertFalse(state.awaiting_human_input)

    def test_proof_completion_enters_cleanup_with_last_good_ref(self):
        root = Path(tempfile.mkdtemp())
        config = self._config(root)
        state = SupervisorState(cycle=14, phase="proof_formalization")
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main": TabletNode(name="main", kind="ordinary", status="closed"),
        })

        stop = should_stop(config, state, tablet, CycleOutcome("PROGRESS", "all done"))

        self.assertFalse(stop)
        self.assertEqual(state.phase, "proof_complete_style_cleanup")
        self.assertEqual(state.cleanup_last_good_commit, "cycle-14")


class TestTrustedPaperStatementReviewIssues(unittest.TestCase):

    def _config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            state_dir=root,
            repo_path=root,
            max_cycles=0,
            workflow=SimpleNamespace(main_result_labels=["main", "gone"]),
        )

    def test_detects_changed_added_and_removed_paper_statements(self):
        root = Path(tempfile.mkdtemp())
        (root / "paper.tex").write_text(
            "\\begin{theorem}\\label{main}\nMain.\n\\end{theorem}\n"
            "\\begin{lemma}\\label{extra}\nExtra.\n\\end{lemma}\n",
            encoding="utf-8",
        )
        tablet_dir = root / "Tablet"
        tablet_dir.mkdir()
        for name, env in [("main", "theorem"), ("extra", "lemma")]:
            (tablet_dir / f"{name}.lean").write_text(
                f"import Tablet.Preamble\n\n-- [TABLET NODE: {name}]\n\ntheorem {name} : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / f"{name}.tex").write_text(
                f"\\begin{{{env}}}\nTrue.\n\\end{{{env}}}\n\\begin{{proof}}\nProof.\n\\end{{proof}}\n",
                encoding="utf-8",
            )
        config = self._config(root)
        state = SupervisorState(
            trusted_main_result_target_state={
                "label:main": {"target": {"tex_label": "main"}, "nodes": ["main"], "fingerprint": "old-main"},
                "label:gone": {"target": {"tex_label": "gone"}, "nodes": ["gone"], "fingerprint": "old-gone"},
            }
        )
        tablet = TabletState(nodes={
            "main": TabletNode(
                name="main",
                kind="ordinary",
                status="closed",
                paper_provenance={"start_line": 1, "end_line": 3, "tex_label": "main"},
            ),
            "extra": TabletNode(
                name="extra",
                kind="ordinary",
                status="open",
                paper_provenance={"start_line": 4, "end_line": 6, "tex_label": "extra"},
            ),
        })

        def fake_fp(_repo: Path, node_name: str) -> str:
            if node_name == "main":
                return "new-main"
            if node_name == "extra":
                return "fp-extra"
            raise AssertionError(f"unexpected node: {node_name}")

        with patch("lagent_tablets.nl_cache.NLCache.correspondence_fingerprint", autospec=True, side_effect=lambda self, repo, node_name: fake_fp(repo, node_name)):
            issues = _trusted_main_result_label_review_issues(config, state, tablet)

        self.assertEqual(
            issues,
            [
                "Configured main-result target `gone` is not covered by any non-helper node.",
                "main: correspondence changed since the last human-reviewed target package",
                "gone: covering node set changed since the last human-reviewed package (was ['gone'], now ['(none)'])",
            ],
        )

    def test_ignores_review_gate_before_any_targets_are_trusted(self):
        root = Path(tempfile.mkdtemp())
        config = self._config(root)
        state = SupervisorState(trusted_main_result_target_state={})
        tablet = TabletState()

        issues = _trusted_main_result_label_review_issues(config, state, tablet)

        self.assertEqual(issues, [])


class TestCliPermissionRepair(unittest.TestCase):

    def _config(self, root: Path, *, phase: str) -> SimpleNamespace:
        return SimpleNamespace(
            repo_path=root,
            state_dir=root / ".agent-supervisor",
            worker=SimpleNamespace(provider="codex", model="gpt-5.4"),
            reviewer=SimpleNamespace(provider="codex", model="gpt-5.4"),
            verification=SimpleNamespace(provider="codex"),
            tmux=SimpleNamespace(burst_user="worker"),
            workflow=SimpleNamespace(
                start_phase=phase,
                phase_overrides={},
                allowed_import_prefixes=["Mathlib"],
                forbidden_keyword_allowlist=[],
                main_result_labels=[],
            ),
            sandbox=SimpleNamespace(enabled=True, backend="bwrap"),
            startup_timeout_seconds=60.0,
            max_cycles=0,
            goal_file=root / "GOAL.txt",
        )

    def _run_main_once(
        self,
        *,
        phase: str,
        state: SupervisorState,
        tablet: TabletState,
        run_patch_target: str,
    ) -> list:
        root = Path(tempfile.mkdtemp())
        state_dir = root / ".agent-supervisor"
        state_dir.mkdir()
        config = self._config(root, phase=phase)

        class _PolicyManager:
            def __init__(self, _config: SimpleNamespace):
                self._policy = SimpleNamespace(
                    timing=SimpleNamespace(sleep_seconds=0),
                )

            def current(self):
                return self._policy

            def reload(self):
                return self._policy

        class _ConfigManager:
            def __init__(self, config_obj: SimpleNamespace):
                self.config = config_obj

            def check_reload(self):
                return False

        cycle_outcome = CycleOutcome("PROGRESS", "ok")
        run_mock_path = f"lagent_tablets.cli.{run_patch_target}"

        with patch("lagent_tablets.cli.load_config", return_value=config), \
             patch("lagent_tablets.cli.check_dependencies"), \
             patch("lagent_tablets.cli.ensure_directories"), \
             patch("lagent_tablets.cli.init_repo"), \
             patch("lagent_tablets.cli.load_state", return_value=state), \
             patch("lagent_tablets.cli.load_tablet", return_value=tablet), \
             patch("lagent_tablets.cli.PolicyManager", _PolicyManager), \
             patch("lagent_tablets.cli.ConfigManager", _ConfigManager), \
             patch("lagent_tablets.cli.write_scripts"), \
             patch("lagent_tablets.cli.regenerate_support_files"), \
             patch("lagent_tablets.cli.write_live_viewer_state"), \
             patch("lagent_tablets.cli._normalize_theorem_stating_replay_state", return_value=[]), \
             patch("lagent_tablets.cli._apply_trusted_main_result_review_gate"), \
             patch("lagent_tablets.cli.should_stop", return_value=True), \
             patch(run_mock_path, return_value=cycle_outcome), \
             patch("lagent_tablets.health.fix_lake_permissions") as mock_fix:
            rc = main(["--config", str(root / "cfg.json"), "--cycles", "1"])

        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(mock_fix.call_args_list), 2)
        for call in mock_fix.call_args_list:
            self.assertTrue(call.kwargs.get("include_package_builds"))
        return mock_fix.call_args_list

    def test_theorem_stating_cli_repairs_include_package_builds_after_cycle(self):
        root = Path(tempfile.mkdtemp())
        tablet = TabletState(nodes={"foo": TabletNode(name="foo", kind="ordinary", status="open")})
        state = SupervisorState(cycle=0, phase="theorem_stating")
        self._run_main_once(
            phase="theorem_stating",
            state=state,
            tablet=tablet,
            run_patch_target="run_theorem_stating_cycle",
        )

    def test_proof_cli_repairs_include_package_builds_after_cycle(self):
        tablet = TabletState(
            active_node="foo",
            nodes={"foo": TabletNode(name="foo", kind="helper_lemma", status="open")},
        )
        state = SupervisorState(cycle=2, phase="proof_formalization", active_node="foo")
        self._run_main_once(
            phase="proof_formalization",
            state=state,
            tablet=tablet,
            run_patch_target="run_cycle",
        )
