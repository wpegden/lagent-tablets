"""Tests for theorem-stating cycle helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lagent_tablets.cycle import (
    _apply_verification_to_tablet,
    _enforce_theorem_stating_orphan_candidates,
    _enforce_theorem_stating_open_rejections,
    _node_content_hash,
    _prune_deleted_tablet_nodes,
    _reconcile_theorem_stating_open_rejections,
    _run_nl_verification,
)
from lagent_tablets.state import TabletNode, TabletState


class TestTheoremStatingOpenRejections(unittest.TestCase):

    def test_reconcile_prefers_reviewer_reason_and_drops_stale_entries(self):
        nl_verification = [{
            "check": "correspondence",
            "overall": "REJECT",
            "agent_results": [
                {
                    "agent": "A",
                    "correspondence": {
                        "decision": "FAIL",
                        "issues": [
                            {"node": "main_thm", "description": "The Lean statement drops a quantifier."},
                        ],
                    },
                    "paper_faithfulness": {
                        "decision": "FAIL",
                        "issues": [
                            {"node": "helper", "description": "This helper is not a faithful paper step."},
                        ],
                    },
                },
                {
                    "agent": "B",
                    "correspondence": {
                        "decision": "FAIL",
                        "issues": [
                            {"node": "main_thm", "description": "The Lean statement drops a quantifier."},
                        ],
                    },
                },
            ],
        }]
        reviewer_rejections = [
            {
                "node": "main_thm",
                "phase": "correspondence",
                "reason": "Restore the missing universal quantifier in Lean.",
            },
            {
                "node": "stale_node",
                "phase": "correspondence",
                "reason": "Old issue that should not survive.",
            },
        ]

        reconciled = _reconcile_theorem_stating_open_rejections(
            nl_verification,
            reviewer_rejections,
        )

        self.assertEqual(
            reconciled,
            [
                {
                    "node": "helper",
                    "phase": "paper_faithfulness",
                    "reason": "This helper is not a faithful paper step.",
                },
                {
                    "node": "main_thm",
                    "phase": "correspondence",
                    "reason": "Restore the missing universal quantifier in Lean.",
                },
            ],
        )

    def test_reconcile_can_keep_reviewer_only_blockers_as_authoritative_list(self):
        nl_verification = [{
            "check": "correspondence",
            "overall": "REJECT",
            "agent_results": [
                {
                    "agent": "A",
                    "correspondence": {
                        "decision": "FAIL",
                        "issues": [
                            {"node": "main_thm", "description": "The Lean statement drops a quantifier."},
                        ],
                    },
                },
            ],
        }]
        reviewer_rejections = [
            {
                "node": "main_thm",
                "phase": "correspondence",
                "reason": "Restore the missing universal quantifier in Lean.",
            },
            {
                "node": "new_blocker",
                "phase": "paper_faithfulness",
                "reason": "This newly identified blocker must be fixed before continuing.",
            },
        ]

        reconciled = _reconcile_theorem_stating_open_rejections(
            nl_verification,
            reviewer_rejections,
            include_preferred_extras=True,
        )

        self.assertEqual(
            reconciled,
            [
                {
                    "node": "main_thm",
                    "phase": "correspondence",
                    "reason": "Restore the missing universal quantifier in Lean.",
                },
                {
                    "node": "new_blocker",
                    "phase": "paper_faithfulness",
                    "reason": "This newly identified blocker must be fixed before continuing.",
                },
            ],
        )


class TestTheoremStatingOrphanCandidates(unittest.TestCase):

    def test_enforce_blocks_advance_phase_while_orphans_remain(self):
        decision = {
            "decision": "ADVANCE_PHASE",
            "reason": "Ready to move on.",
            "next_prompt": "",
            "orphan_resolutions": [
                {
                    "node": "is_downset",
                    "action": "remove",
                    "reason": "No downstream node imports or cites it.",
                    "suggested_parents": [],
                }
            ],
        }

        _enforce_theorem_stating_orphan_candidates(decision, ["is_downset"])

        self.assertEqual(decision["decision"], "CONTINUE")
        self.assertIn("Orphan node candidates remain", decision["reason"])
        self.assertEqual(
            decision["orphan_resolutions"],
            [
                {
                    "node": "is_downset",
                    "action": "remove",
                    "reason": "No downstream node imports or cites it.",
                    "suggested_parents": [],
                }
            ],
        )

    def test_enforce_sets_default_guidance_when_missing(self):
        decision = {
            "decision": "CONTINUE",
            "reason": "Needs more work.",
            "next_prompt": "",
        }

        _enforce_theorem_stating_orphan_candidates(decision, ["is_downset"])

        self.assertIn("Resolve the current orphan-node candidates", decision["next_prompt"])
        self.assertIn("is_downset", decision["next_prompt"])

    def test_prune_deleted_tablet_nodes_removes_missing_nodes(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "keep_me": TabletNode(name="keep_me", kind="helper_lemma", status="open"),
            "drop_me": TabletNode(name="drop_me", kind="helper_lemma", status="open"),
        }, active_node="drop_me")

        deleted = _prune_deleted_tablet_nodes(tablet, {"keep_me"})

        self.assertEqual(deleted, ["drop_me"])
        self.assertIn("keep_me", tablet.nodes)
        self.assertNotIn("drop_me", tablet.nodes)
        self.assertEqual(tablet.active_node, "")

    def test_enforce_blocks_advance_phase_until_list_is_empty(self):
        decision = {
            "decision": "ADVANCE_PHASE",
            "reason": "Ready to move on.",
            "next_prompt": "",
        }
        open_rejections = [
            {
                "node": "main_thm",
                "phase": "correspondence",
                "reason": "Restore the missing universal quantifier in Lean.",
            }
        ]

        _enforce_theorem_stating_open_rejections(decision, open_rejections)

        self.assertEqual(decision["decision"], "CONTINUE")
        self.assertIn("main_thm (correspondence)", decision["reason"])
        self.assertIn("Theorem-stating continues until the open-rejection list is empty", decision["next_prompt"])
        self.assertEqual(decision["open_rejections"], open_rejections)

    def test_reconcile_ignores_resolved_commentary_and_pass_phase_issues(self):
        nl_verification = [{
            "check": "correspondence",
            "overall": "REJECT",
            "agent_results": [
                {
                    "agent": "A",
                    "correspondence": {
                        "decision": "FAIL",
                        "issues": [
                            {
                                "node": "main_thm",
                                "description": "The Lean statement drops a quantifier.",
                            },
                            {
                                "node": "old_issue",
                                "description": "PREVIOUSLY FLAGGED — NOW FIXED after the latest rewrite.",
                            },
                        ],
                    },
                    "paper_faithfulness": {
                        "decision": "PASS",
                        "issues": [
                            {
                                "node": "paper_node",
                                "description": "This should not be ingested because the phase passed.",
                            },
                        ],
                    },
                },
            ],
        }]

        reconciled = _reconcile_theorem_stating_open_rejections(
            nl_verification,
            [],
        )

        self.assertEqual(
            reconciled,
            [
                {
                    "node": "main_thm",
                    "phase": "correspondence",
                    "reason": "The Lean statement drops a quantifier.",
                },
            ],
        )


class TestApplyVerificationToTablet(unittest.TestCase):

    def _write_node_files(self, repo: Path, name: str) -> str:
        tablet_dir = repo / "Tablet"
        tablet_dir.mkdir(parents=True, exist_ok=True)
        (tablet_dir / f"{name}.lean").write_text(
            f"theorem {name} : True := by\n  trivial\n",
            encoding="utf-8",
        )
        (tablet_dir / f"{name}.tex").write_text(
            f"\\begin{{theorem}}[{name}]\nTrue.\n\\end{{theorem}}\n",
            encoding="utf-8",
        )
        return _node_content_hash(repo, name)

    def test_checked_unchanged_node_is_promoted_from_fail_to_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            node_hash = self._write_node_files(repo, "hyperplane_coweight_count")
            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "hyperplane_coweight_count": TabletNode(
                    name="hyperplane_coweight_count",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="fail",
                    soundness_status="?",
                    verification_content_hash=node_hash,
                    verification_at_cycle=6,
                ),
            })

            verification_results = [
                {
                    "check": "correspondence",
                    "overall": "APPROVE",
                    "node_names": ["hyperplane_coweight_count"],
                    "agent_results": [
                        {
                            "correspondence": {"decision": "PASS", "issues": []},
                            "paper_faithfulness": {"decision": "PASS", "issues": []},
                        }
                    ],
                },
                {
                    "check": "nl_proof",
                    "overall": "APPROVE",
                    "node_names": ["hyperplane_coweight_count"],
                    "node_verdicts": [
                        {"node": "hyperplane_coweight_count", "overall": "APPROVE"}
                    ],
                },
            ]

            _apply_verification_to_tablet(tablet, verification_results, 7, repo_path=repo)

            node = tablet.nodes["hyperplane_coweight_count"]
            self.assertEqual(node.correspondence_status, "pass")
            self.assertEqual(node.soundness_status, "pass")
            self.assertEqual(node.verification_at_cycle, 7)
            self.assertEqual(node.verification_content_hash, node_hash)

    def test_unchecked_nodes_keep_their_existing_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            a_hash = self._write_node_files(repo, "checked_node")
            b_hash = self._write_node_files(repo, "unchecked_node")
            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "checked_node": TabletNode(
                    name="checked_node",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="fail",
                    soundness_status="?",
                    verification_content_hash=a_hash,
                    verification_at_cycle=6,
                ),
                "unchecked_node": TabletNode(
                    name="unchecked_node",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="fail",
                    soundness_status="fail",
                    verification_content_hash=b_hash,
                    verification_at_cycle=4,
                ),
            })

            verification_results = [
                {
                    "check": "correspondence",
                    "overall": "APPROVE",
                    "node_names": ["checked_node"],
                    "agent_results": [
                        {
                            "correspondence": {"decision": "PASS", "issues": []},
                            "paper_faithfulness": {"decision": "PASS", "issues": []},
                        }
                    ],
                },
                {
                    "check": "nl_proof",
                    "overall": "APPROVE",
                    "node_names": ["checked_node"],
                    "node_verdicts": [{"node": "checked_node", "overall": "APPROVE"}],
                },
            ]

            _apply_verification_to_tablet(tablet, verification_results, 7, repo_path=repo)

            checked = tablet.nodes["checked_node"]
            unchecked = tablet.nodes["unchecked_node"]
            self.assertEqual(checked.correspondence_status, "pass")
            self.assertEqual(checked.soundness_status, "pass")
            self.assertEqual(unchecked.correspondence_status, "fail")
            self.assertEqual(unchecked.soundness_status, "fail")
            self.assertEqual(unchecked.verification_at_cycle, 4)

    def test_correspondence_is_checkpointed_before_soundness(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "Tablet").mkdir()
            (repo / "Tablet" / "foo.lean").write_text(
                "theorem foo : True := by\n  sorry\n",
                encoding="utf-8",
            )
            (repo / "Tablet" / "foo.tex").write_text(
                "\\begin{theorem}[foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n",
                encoding="utf-8",
            )
            state_dir = repo / ".agent-supervisor"
            state_dir.mkdir()

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "foo": TabletNode(name="foo", kind="paper_intermediate", status="open"),
            })
            config = SimpleNamespace(
                repo_path=repo,
                state_dir=state_dir,
                workflow=SimpleNamespace(paper_tex_path=None),
                tmux=SimpleNamespace(session_name="test", burst_user="worker"),
                verification=SimpleNamespace(
                    provider="claude",
                    model="claude-opus-4-6",
                    extra_args=[],
                    correspondence_agents=[
                        SimpleNamespace(provider="claude", model="a", label="A"),
                        SimpleNamespace(provider="gemini", model="b", label="B"),
                    ],
                    soundness_agents=[
                        SimpleNamespace(provider="claude", model="a", label="A"),
                        SimpleNamespace(provider="gemini", model="b", label="B"),
                    ],
                ),
            )
            corr_result = {
                "check": "correspondence",
                "overall": "APPROVE",
                "node_names": ["foo"],
                "agent_results": [
                    {
                        "correspondence": {"decision": "PASS", "issues": []},
                        "paper_faithfulness": {"decision": "PASS", "issues": []},
                    }
                ],
            }

            def fake_soundness(*args, **kwargs):
                self.assertEqual(tablet.nodes["foo"].correspondence_status, "pass")
                self.assertEqual(tablet.nodes["foo"].verification_at_cycle, 9)
                self.assertEqual(mock_save_tablet.call_count, 1)
                return [{
                    "check": "nl_proof",
                    "overall": "APPROVE",
                    "node_names": ["foo"],
                    "node_verdicts": [{"node": "foo", "overall": "APPROVE"}],
                }]

            with patch("lagent_tablets.cycle._run_multi_correspondence", return_value=corr_result):
                with patch("lagent_tablets.cycle._run_per_node_soundness", side_effect=fake_soundness):
                    with patch("lagent_tablets.cycle.save_tablet") as mock_save_tablet:
                        results = _run_nl_verification(
                            config,
                            tablet,
                            ["foo"],
                            cycle=9,
                            log_dir=repo,
                            nl_cache=None,
                            human_input="",
                        )

            self.assertEqual(mock_save_tablet.call_count, 1)
            self.assertEqual(tablet.nodes["foo"].correspondence_status, "pass")
            self.assertEqual(results[0]["check"], "correspondence")
            self.assertEqual(results[1]["check"], "nl_proof")


if __name__ == "__main__":
    unittest.main()
