"""Tests for theorem-stating cycle helpers."""

from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lagent_tablets.artifacts import done_marker_path, raw_json_path
from lagent_tablets.config import Policy, VerificationPolicy
from lagent_tablets.cycle import (
    _apply_verification_to_tablet,
    _backfill_legacy_correspondence_hashes,
    _changed_tablet_nodes_since_snapshot,
    _correspondence_gate_open,
    _enforce_theorem_stating_orphan_candidates,
    _enforce_theorem_stating_open_rejections,
    _node_content_hash,
    _prune_deleted_tablet_nodes,
    _repair_stale_legacy_correspondence_failures,
    _reconcile_theorem_stating_open_rejections,
    _run_nl_verification,
    _run_per_node_soundness,
    _select_theorem_soundness_target,
    _snapshot_tablet_dir,
    _snapshot_tablet_node_hashes,
    _suspend_theorem_soundness_target,
    _theorem_stating_correspondence_frontier,
    _theorem_target_scope,
    _validate_easy_proof_repair_changes,
    _validate_theorem_target_repair_changes,
    _validate_theorem_target_edit_scope,
)
from lagent_tablets.nl_cache import correspondence_fingerprint, correspondence_text_fingerprint, soundness_fingerprint
from lagent_tablets.state import SupervisorState, TabletNode, TabletState


class TestTheoremStatingOpenRejections(unittest.TestCase):

    def test_correspondence_gate_open_only_when_correspondence_not_approved(self):
        self.assertFalse(_correspondence_gate_open([]))
        self.assertFalse(_correspondence_gate_open([
            {"check": "correspondence", "overall": "APPROVE"},
            {"check": "nl_proof", "overall": "REJECT"},
        ]))
        self.assertTrue(_correspondence_gate_open([
            {"check": "correspondence", "overall": "REJECT"},
        ]))
        self.assertTrue(_correspondence_gate_open([
            {"check": "correspondence", "overall": "DISAGREE"},
        ]))

    def test_suspend_theorem_soundness_target_clears_target(self):
        state = SupervisorState(
            cycle=9,
            phase="theorem_stating",
            theorem_soundness_target="binary_weight_extremal_main",
            theorem_target_edit_mode="restructure",
        )

        suspended = _suspend_theorem_soundness_target(state)

        self.assertEqual(suspended.theorem_soundness_target, "")
        self.assertEqual(suspended.theorem_target_edit_mode, "repair")
        self.assertTrue(suspended.theorem_correspondence_blocked)
        self.assertEqual(state.theorem_soundness_target, "binary_weight_extremal_main")

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
            corr_hash = correspondence_fingerprint(repo, "hyperplane_coweight_count")
            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "hyperplane_coweight_count": TabletNode(
                    name="hyperplane_coweight_count",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="fail",
                    soundness_status="?",
                    correspondence_content_hash=corr_hash or "",
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
            self.assertEqual(node.correspondence_content_hash, corr_hash)
            self.assertEqual(node.soundness_content_hash, node_hash)

    def test_unchecked_nodes_keep_their_existing_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            a_hash = self._write_node_files(repo, "checked_node")
            b_hash = self._write_node_files(repo, "unchecked_node")
            a_corr_hash = correspondence_fingerprint(repo, "checked_node")
            b_corr_hash = correspondence_fingerprint(repo, "unchecked_node")
            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "checked_node": TabletNode(
                    name="checked_node",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="fail",
                    soundness_status="?",
                    correspondence_content_hash=a_corr_hash or "",
                    verification_content_hash=a_hash,
                    verification_at_cycle=6,
                ),
                "unchecked_node": TabletNode(
                    name="unchecked_node",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="fail",
                    soundness_status="fail",
                    correspondence_content_hash=b_corr_hash or "",
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

    def test_tex_proof_change_does_not_invalidate_correspondence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "foo.lean").write_text(
                "theorem foo : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "foo.tex").write_text(
                "\\begin{theorem}[foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nOld proof.\n\\end{proof}\n",
                encoding="utf-8",
            )
            corr_hash = correspondence_fingerprint(repo, "foo")
            sound_hash = soundness_fingerprint(repo, "foo")
            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "foo": TabletNode(
                    name="foo",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    soundness_status="pass",
                    correspondence_content_hash=corr_hash or "",
                    soundness_content_hash=sound_hash or "",
                    verification_content_hash=sound_hash or "",
                    verification_at_cycle=9,
                ),
            })

            (tablet_dir / "foo.tex").write_text(
                "\\begin{theorem}[foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nNew proof details.\n\\end{proof}\n",
                encoding="utf-8",
            )
            new_corr_hash = correspondence_fingerprint(repo, "foo")
            new_sound_hash = soundness_fingerprint(repo, "foo")
            self.assertEqual(new_corr_hash, corr_hash)
            self.assertNotEqual(new_sound_hash, sound_hash)

            verification_results = [
                {
                    "check": "nl_proof",
                    "overall": "APPROVE",
                    "node_names": ["foo"],
                    "node_verdicts": [{"node": "foo", "overall": "APPROVE"}],
                },
            ]

            _apply_verification_to_tablet(tablet, verification_results, 10, repo_path=repo)

            node = tablet.nodes["foo"]
            self.assertEqual(node.correspondence_status, "pass")
            self.assertEqual(node.correspondence_content_hash, corr_hash)
            self.assertEqual(node.soundness_status, "pass")
            self.assertEqual(node.soundness_content_hash, new_sound_hash)
            self.assertEqual(node.verification_content_hash, new_sound_hash)

    def test_child_proof_change_does_not_change_parent_soundness_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "child.lean").write_text(
                "theorem child : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "child.tex").write_text(
                "\\begin{lemma}[child]\nTrue.\n\\end{lemma}\n\\begin{proof}\nOld child proof.\n\\end{proof}\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.lean").write_text(
                "import Tablet.child\n\ntheorem parent : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.tex").write_text(
                "\\begin{theorem}[parent]\nTrue.\n\\end{theorem}\n\\begin{proof}\nBy \\noderef{child}.\n\\end{proof}\n",
                encoding="utf-8",
            )
            old_fp = soundness_fingerprint(repo, "parent")
            (tablet_dir / "child.tex").write_text(
                "\\begin{lemma}[child]\nTrue.\n\\end{lemma}\n\\begin{proof}\nNew child proof details.\n\\end{proof}\n",
                encoding="utf-8",
            )
            new_fp = soundness_fingerprint(repo, "parent")
            self.assertEqual(new_fp, old_fp)

    def test_child_statement_change_does_change_parent_soundness_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "child.lean").write_text(
                "theorem child : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "child.tex").write_text(
                "\\begin{lemma}[child]\nTrue.\n\\end{lemma}\n\\begin{proof}\nChild proof.\n\\end{proof}\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.lean").write_text(
                "import Tablet.child\n\ntheorem parent : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.tex").write_text(
                "\\begin{theorem}[parent]\nTrue.\n\\end{theorem}\n\\begin{proof}\nBy \\noderef{child}.\n\\end{proof}\n",
                encoding="utf-8",
            )
            old_fp = soundness_fingerprint(repo, "parent")
            (tablet_dir / "child.tex").write_text(
                "\\begin{lemma}[child]\nFalse.\n\\end{lemma}\n\\begin{proof}\nChild proof.\n\\end{proof}\n",
                encoding="utf-8",
            )
            new_fp = soundness_fingerprint(repo, "parent")
            self.assertNotEqual(new_fp, old_fp)


class TestCorrespondenceHashMigration(unittest.TestCase):

    def test_legacy_correspondence_pass_hash_is_upgraded_without_frontier(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "foo": TabletNode(
                name="foo",
                kind="paper_intermediate",
                status="open",
                correspondence_status="pass",
                soundness_status="?",
                correspondence_content_hash="legacy-foo",
                verification_content_hash="legacy-foo",
                verification_at_cycle=8,
            ),
        })
        repo = Path("/tmp/unused")

        with patch("lagent_tablets.cycle._correspondence_text_hash", return_value="text-foo"), \
             patch("lagent_tablets.cycle.historical_correspondence_text_fingerprint", return_value="text-foo"), \
             patch("lagent_tablets.cycle.legacy_correspondence_text_fingerprint", return_value="legacy-text"), \
             patch("lagent_tablets.cycle.historical_legacy_correspondence_text_fingerprint", return_value="legacy-text"), \
             patch("lagent_tablets.cycle.prime_correspondence_fingerprints"), \
             patch("lagent_tablets.cycle._correspondence_content_hash", return_value="semantic-foo"), \
             patch("lagent_tablets.cycle.legacy_correspondence_fingerprint", return_value="legacy-foo"):
            changed = _backfill_legacy_correspondence_hashes(tablet, repo)
            frontier = _theorem_stating_correspondence_frontier(tablet, repo)

        self.assertTrue(changed)
        self.assertEqual(frontier, [])
        node = tablet.nodes["foo"]
        self.assertEqual(node.correspondence_text_hash, "text-foo")
        self.assertEqual(node.correspondence_content_hash, "semantic-foo")
        self.assertEqual(node.verification_content_hash, "semantic-foo")

    def test_backfill_does_not_overwrite_changed_text_hash(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "foo": TabletNode(
                name="foo",
                kind="paper_intermediate",
                status="open",
                correspondence_status="pass",
                soundness_status="?",
                correspondence_text_hash="old-text",
                correspondence_content_hash="semantic-foo",
                verification_content_hash="semantic-foo",
                verification_at_cycle=8,
            ),
        })
        repo = Path("/tmp/unused")

        with patch("lagent_tablets.cycle._correspondence_text_hash", return_value="new-text"), \
             patch("lagent_tablets.cycle._correspondence_content_hash", return_value="semantic-foo"), \
             patch("lagent_tablets.cycle.historical_correspondence_text_fingerprint", return_value="old-text"), \
             patch("lagent_tablets.cycle.legacy_correspondence_fingerprint", return_value=None):
            changed = _backfill_legacy_correspondence_hashes(tablet, repo)

        self.assertFalse(changed)
        self.assertEqual(tablet.nodes["foo"].correspondence_text_hash, "old-text")

    def test_backfill_migrates_previous_text_hash_scheme(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "foo": TabletNode(
                name="foo",
                kind="paper_intermediate",
                status="open",
                correspondence_status="pass",
                soundness_status="?",
                correspondence_text_hash="previous-text",
                correspondence_content_hash="semantic-foo",
                verification_content_hash="semantic-foo",
                verification_at_cycle=8,
            ),
        })
        repo = Path("/tmp/unused")

        with patch("lagent_tablets.cycle._correspondence_text_hash", return_value="new-text"), \
             patch("lagent_tablets.cycle._correspondence_content_hash", return_value="semantic-foo"), \
             patch("lagent_tablets.cycle.historical_correspondence_text_fingerprint", return_value=None), \
             patch("lagent_tablets.cycle.legacy_correspondence_text_fingerprint", return_value=None), \
             patch("lagent_tablets.cycle.previous_correspondence_text_fingerprint", return_value="previous-text"), \
             patch("lagent_tablets.cycle.legacy_correspondence_fingerprint", return_value=None):
            changed = _backfill_legacy_correspondence_hashes(tablet, repo)

        self.assertTrue(changed)
        self.assertEqual(tablet.nodes["foo"].correspondence_text_hash, "new-text")

    def test_text_hash_match_skips_semantic_frontier_check(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "foo": TabletNode(
                name="foo",
                kind="paper_intermediate",
                status="open",
                correspondence_status="pass",
                correspondence_text_hash="text-foo",
                correspondence_content_hash="semantic-foo",
                verification_content_hash="semantic-foo",
            ),
        })
        repo = Path("/tmp/unused")

        with patch("lagent_tablets.cycle._correspondence_text_hash", return_value="text-foo"), \
             patch("lagent_tablets.cycle.prime_correspondence_fingerprints") as prime_mock, \
             patch("lagent_tablets.cycle._correspondence_content_hash", return_value="semantic-foo") as semantic_mock:
            frontier = _theorem_stating_correspondence_frontier(tablet, repo)

        self.assertEqual(frontier, [])
        prime_mock.assert_called_once()
        semantic_mock.assert_called_once()

    def test_text_hash_mismatch_falls_back_to_semantic_check(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "foo": TabletNode(
                name="foo",
                kind="paper_intermediate",
                status="open",
                correspondence_status="pass",
                correspondence_text_hash="old-text",
                correspondence_content_hash="semantic-foo",
                verification_content_hash="semantic-foo",
            ),
        })
        repo = Path("/tmp/unused")

        with patch("lagent_tablets.cycle._correspondence_text_hash", return_value="new-text"), \
             patch("lagent_tablets.cycle.prime_correspondence_fingerprints"), \
             patch("lagent_tablets.cycle._correspondence_content_hash", return_value="semantic-foo"), \
             patch("lagent_tablets.cycle.legacy_correspondence_fingerprint", return_value=None):
            frontier = _theorem_stating_correspondence_frontier(tablet, repo)

        self.assertEqual(frontier, ["foo"])
        self.assertEqual(tablet.nodes["foo"].correspondence_text_hash, "old-text")

    def test_stale_legacy_failures_reopen_instead_of_auto_passing(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "foo": TabletNode(
                name="foo",
                kind="paper_intermediate",
                status="open",
                correspondence_status="fail",
                correspondence_text_hash="old-text",
                correspondence_content_hash="old-semantic",
                verification_content_hash="old-semantic",
                verification_at_cycle=8,
            ),
        })
        state = SimpleNamespace(open_rejections=[])

        changed = _repair_stale_legacy_correspondence_failures(tablet, state, Path("/tmp/unused"))

        self.assertTrue(changed)
        node = tablet.nodes["foo"]
        self.assertEqual(node.correspondence_status, "?")
        self.assertEqual(node.correspondence_text_hash, "")
        self.assertEqual(node.correspondence_content_hash, "")

    def test_missing_legacy_hashes_use_verified_tag_baseline_and_reopen_changed_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "Preamble.lean").write_text("", encoding="utf-8")
            (tablet_dir / "helper.lean").write_text(
                "noncomputable def helper : Nat := 0\n",
                encoding="utf-8",
            )
            (tablet_dir / "helper.tex").write_text(
                "\\begin{definition}[helper]\n$helper := 0$.\n\\end{definition}\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.lean").write_text(
                "import Tablet.helper\n\ntheorem parent : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.tex").write_text(
                "\\begin{theorem}[parent]\nTrue.\n\\end{theorem}\n\\begin{proof}\nBy \\noderef{helper}.\n\\end{proof}\n",
                encoding="utf-8",
            )

            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "cycle-8"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "tag", "cycle-8"], check=True, capture_output=True, text=True)

            (tablet_dir / "helper.lean").write_text(
                "theorem helper : Set.Finite (Set.univ : Set Nat) := by\n  simp\n",
                encoding="utf-8",
            )
            (tablet_dir / "helper.tex").write_text(
                "\\begin{lemma}[helper]\nThe set of naturals is finite.\n\\end{lemma}\n\\begin{proof}\nFalse proof.\n\\end{proof}\n",
                encoding="utf-8",
            )

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "helper": TabletNode(
                    name="helper",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    verification_at_cycle=8,
                ),
                "parent": TabletNode(
                    name="parent",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    verification_at_cycle=8,
                ),
            })

            changed = _backfill_legacy_correspondence_hashes(tablet, repo)
            frontier = _theorem_stating_correspondence_frontier(tablet, repo)

            self.assertTrue(changed)
            self.assertEqual(frontier, ["helper", "parent"])
            self.assertNotEqual(tablet.nodes["helper"].correspondence_text_hash, "")
            self.assertNotEqual(tablet.nodes["parent"].correspondence_text_hash, "")


class TestCorrespondenceFrontierPropagation(unittest.TestCase):

    def test_child_definition_statement_change_reopens_parent_correspondence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "Preamble.lean").write_text("", encoding="utf-8")
            (tablet_dir / "child.lean").write_text(
                "def child : Prop := True\n",
                encoding="utf-8",
            )
            (tablet_dir / "child.tex").write_text(
                "\\begin{definition}[child]\n$child := \\mathrm{True}$.\n\\end{definition}\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.lean").write_text(
                "import Tablet.child\n\ntheorem parent : child := by\n  exact True.intro\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.tex").write_text(
                "\\begin{theorem}[parent]\n$child$.\n\\end{theorem}\n\\begin{proof}\nBy definition of \\noderef{child}.\n\\end{proof}\n",
                encoding="utf-8",
            )

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "child": TabletNode(
                    name="child",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    correspondence_text_hash="",
                ),
                "parent": TabletNode(
                    name="parent",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    correspondence_text_hash="",
                ),
            })
            for name in ("child", "parent"):
                tablet.nodes[name].correspondence_text_hash = correspondence_text_fingerprint(repo, name) or ""
                tablet.nodes[name].correspondence_content_hash = correspondence_fingerprint(repo, name) or ""
                tablet.nodes[name].verification_content_hash = tablet.nodes[name].correspondence_content_hash

            (tablet_dir / "child.tex").write_text(
                "\\begin{definition}[child]\n$child := \\mathrm{False}$.\n\\end{definition}\n",
                encoding="utf-8",
            )

            frontier = _theorem_stating_correspondence_frontier(tablet, repo)
            self.assertEqual(frontier, ["child", "parent"])

    def test_child_theorem_statement_change_does_not_reopen_parent_correspondence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "Preamble.lean").write_text("", encoding="utf-8")
            (tablet_dir / "child.lean").write_text(
                "theorem child : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "child.tex").write_text(
                "\\begin{lemma}[child]\nTrue.\n\\end{lemma}\n\\begin{proof}\nChild proof.\n\\end{proof}\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.lean").write_text(
                "import Tablet.child\n\ntheorem parent : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.tex").write_text(
                "\\begin{theorem}[parent]\nTrue.\n\\end{theorem}\n\\begin{proof}\nBy \\noderef{child}.\n\\end{proof}\n",
                encoding="utf-8",
            )

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "child": TabletNode(
                    name="child",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    correspondence_text_hash="",
                ),
                "parent": TabletNode(
                    name="parent",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    correspondence_text_hash="",
                ),
            })
            for name in ("child", "parent"):
                tablet.nodes[name].correspondence_text_hash = correspondence_text_fingerprint(repo, name) or ""
                tablet.nodes[name].correspondence_content_hash = correspondence_fingerprint(repo, name) or ""
                tablet.nodes[name].verification_content_hash = tablet.nodes[name].correspondence_content_hash

            (tablet_dir / "child.tex").write_text(
                "\\begin{lemma}[child]\nFalse.\n\\end{lemma}\n\\begin{proof}\nChild proof.\n\\end{proof}\n",
                encoding="utf-8",
            )

            frontier = _theorem_stating_correspondence_frontier(tablet, repo)
            self.assertEqual(frontier, ["child"])

    def test_child_proof_only_change_does_not_reopen_parent_correspondence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "Preamble.lean").write_text("", encoding="utf-8")
            (tablet_dir / "child.lean").write_text(
                "theorem child : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "child.tex").write_text(
                "\\begin{lemma}[child]\nTrue.\n\\end{lemma}\n\\begin{proof}\nOld child proof.\n\\end{proof}\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.lean").write_text(
                "import Tablet.child\n\ntheorem parent : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "parent.tex").write_text(
                "\\begin{theorem}[parent]\nTrue.\n\\end{theorem}\n\\begin{proof}\nBy \\noderef{child}.\n\\end{proof}\n",
                encoding="utf-8",
            )

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "child": TabletNode(
                    name="child",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    correspondence_text_hash="",
                ),
                "parent": TabletNode(
                    name="parent",
                    kind="paper_intermediate",
                    status="open",
                    correspondence_status="pass",
                    correspondence_text_hash="",
                ),
            })
            for name in ("child", "parent"):
                tablet.nodes[name].correspondence_text_hash = correspondence_text_fingerprint(repo, name) or ""
                tablet.nodes[name].correspondence_content_hash = correspondence_fingerprint(repo, name) or ""
                tablet.nodes[name].verification_content_hash = tablet.nodes[name].correspondence_content_hash

            (tablet_dir / "child.tex").write_text(
                "\\begin{lemma}[child]\nTrue.\n\\end{lemma}\n\\begin{proof}\nNew child proof details.\n\\end{proof}\n",
                encoding="utf-8",
            )

            frontier = _theorem_stating_correspondence_frontier(tablet, repo)
            self.assertEqual(frontier, [])


class TestTheoremTargetScope(unittest.TestCase):

    def test_target_scope_allows_prerequisite_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "helper.lean").write_text(
                "theorem helper : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "helper.tex").write_text(
                "\\begin{lemma}[helper]\nTrue.\n\\end{lemma}\n\\begin{proof}\nOld.\n\\end{proof}\n",
                encoding="utf-8",
            )
            (tablet_dir / "target.lean").write_text(
                "import Tablet.helper\n\ntheorem target : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "target.tex").write_text(
                "\\begin{theorem}[target]\nTrue.\n\\end{theorem}\n\\begin{proof}\nBy \\noderef{helper}.\n\\end{proof}\n",
                encoding="utf-8",
            )

            before = _snapshot_tablet_node_hashes(repo)
            initial_scope = _theorem_target_scope(repo, "target")
            (tablet_dir / "helper.tex").write_text(
                "\\begin{lemma}[helper]\nTrue.\n\\end{lemma}\n\\begin{proof}\nNew prerequisite proof.\n\\end{proof}\n",
                encoding="utf-8",
            )

            error = _validate_theorem_target_edit_scope(
                repo, "target", before, initial_scope=initial_scope
            )
            self.assertIsNone(error)

    def test_target_scope_rejects_unrelated_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            (tablet_dir / "helper.lean").write_text(
                "theorem helper : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "helper.tex").write_text(
                "\\begin{lemma}[helper]\nTrue.\n\\end{lemma}\n\\begin{proof}\nOld.\n\\end{proof}\n",
                encoding="utf-8",
            )
            (tablet_dir / "target.lean").write_text(
                "import Tablet.helper\n\ntheorem target : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "target.tex").write_text(
                "\\begin{theorem}[target]\nTrue.\n\\end{theorem}\n\\begin{proof}\nBy \\noderef{helper}.\n\\end{proof}\n",
                encoding="utf-8",
            )
            (tablet_dir / "unrelated.lean").write_text(
                "theorem unrelated : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (tablet_dir / "unrelated.tex").write_text(
                "\\begin{lemma}[unrelated]\nTrue.\n\\end{lemma}\n\\begin{proof}\nOld.\n\\end{proof}\n",
                encoding="utf-8",
            )

            before = _snapshot_tablet_node_hashes(repo)
            initial_scope = _theorem_target_scope(repo, "target")
            (tablet_dir / "unrelated.tex").write_text(
                "\\begin{lemma}[unrelated]\nTrue.\n\\end{lemma}\n\\begin{proof}\nUnrelated rewrite.\n\\end{proof}\n",
                encoding="utf-8",
            )

            changed = _changed_tablet_nodes_since_snapshot(repo, before)
            self.assertEqual(changed, ["unrelated"])
            error = _validate_theorem_target_edit_scope(
                repo, "target", before, initial_scope=initial_scope
            )
            self.assertIsNotNone(error)
            self.assertIn("unrelated", error)
            self.assertIn("target", error)

    def test_changed_nodes_since_snapshot_only_lists_actual_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir(parents=True, exist_ok=True)
            for name in ("a", "b", "c"):
                (tablet_dir / f"{name}.lean").write_text(
                    f"theorem {name} : True := by\n  trivial\n",
                    encoding="utf-8",
                )
                (tablet_dir / f"{name}.tex").write_text(
                    f"\\begin{{theorem}}[{name}]\nTrue.\n\\end{{theorem}}\n\\begin{{proof}}\nProof.\n\\end{{proof}}\n",
                    encoding="utf-8",
                )

            before = _snapshot_tablet_node_hashes(repo)
            (tablet_dir / "b.tex").write_text(
                "\\begin{theorem}[b]\nTrue.\n\\end{theorem}\n\\begin{proof}\nUpdated.\n\\end{proof}\n",
                encoding="utf-8",
            )

            self.assertEqual(_changed_tablet_nodes_since_snapshot(repo, before), ["b"])

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
                            Policy(),
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

    def test_per_node_soundness_orders_deepest_first_by_dag_not_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".agent-supervisor"
            (state_dir / "staging").mkdir(parents=True)
            (repo / "Tablet").mkdir()

            def write_node(name: str, *, imports: list[str] | None = None) -> None:
                import_lines = "".join(f"import Tablet.{dep}\n" for dep in (imports or []))
                (repo / "Tablet" / f"{name}.lean").write_text(
                    f"{import_lines}theorem {name} : True := by\n  trivial\n",
                    encoding="utf-8",
                )
                (repo / "Tablet" / f"{name}.tex").write_text(
                    f"\\begin{{theorem}}[{name}]\nTrue.\n\\end{{theorem}}\n\\begin{{proof}}\nProof.\n\\end{{proof}}\n",
                    encoding="utf-8",
                )

            write_node("main_thm", imports=["intermediate"])
            write_node("intermediate", imports=["helper"])
            write_node("helper")

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "main_thm": TabletNode(name="main_thm", kind="helper_lemma", status="open"),
                "intermediate": TabletNode(name="intermediate", kind="definition", status="open"),
                "helper": TabletNode(name="helper", kind="paper_main_result", status="open"),
            })
            config = SimpleNamespace(
                repo_path=repo,
                state_dir=state_dir,
                tmux=SimpleNamespace(session_name="test", burst_user="worker"),
            )
            agents = [SimpleNamespace(provider="codex", model="gpt-5.4", label="A")]
            seen_order: list[str] = []

            def fake_single(*args, **kwargs):
                node_name = kwargs["node_name"] if "node_name" in kwargs else args[2]
                seen_order.append(node_name)
                return {
                    "agent": "A",
                    "node": node_name,
                    "index": 0,
                    "ok": True,
                    "overall": "APPROVE",
                    "summary": "ok",
                    "soundness": {"decision": "SOUND", "explanation": "ok"},
                }

            with patch("lagent_tablets.cycle._run_single_node_soundness", side_effect=fake_single):
                _run_per_node_soundness(
                    config,
                    tablet,
                    ["helper", "intermediate", "main_thm"],
                    agents,
                    paper_tex="",
                    human_input="",
                    log_dir=repo,
                )

            self.assertEqual(seen_order, ["helper", "intermediate", "main_thm"])

    def test_per_node_soundness_reuses_existing_result_and_backfills_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".agent-supervisor"
            (state_dir / "staging").mkdir(parents=True)
            (repo / "Tablet").mkdir()
            (repo / "Tablet" / "foo.lean").write_text(
                "theorem foo : True := by\n  trivial\n",
                encoding="utf-8",
            )
            (repo / "Tablet" / "foo.tex").write_text(
                "\\begin{theorem}[foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n",
                encoding="utf-8",
            )
            canonical = repo / "nl_proof_foo_0.json"
            raw = raw_json_path(state_dir, "nl_proof_foo_0.json")
            done = done_marker_path(state_dir, "nl_proof_foo_0.json")
            payload = {
                "node": "foo",
                "soundness": {"decision": "SOUND", "explanation": "ok"},
                "overall": "APPROVE",
                "summary": "ok",
            }
            canonical.write_text(json.dumps(payload), encoding="utf-8")
            raw.write_text(json.dumps(payload), encoding="utf-8")
            done.write_text("", encoding="utf-8")

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "foo": TabletNode(name="foo", kind="paper_intermediate", status="open"),
            })
            config = SimpleNamespace(
                repo_path=repo,
                state_dir=state_dir,
                tmux=SimpleNamespace(session_name="test", burst_user="worker"),
            )
            agents = [SimpleNamespace(provider="codex", model="gpt-5.4", label="A")]

            with patch("lagent_tablets.cycle._run_single_node_soundness") as mock_single:
                results = _run_per_node_soundness(
                    config,
                    tablet,
                    ["foo"],
                    agents,
                    paper_tex="",
                    human_input="",
                    log_dir=repo,
                )

            mock_single.assert_not_called()
            verdict = results[0]["node_verdicts"][0]
            self.assertEqual(verdict["node"], "foo")
            self.assertEqual(verdict["overall"], "APPROVE")
            saved = json.loads(canonical.read_text(encoding="utf-8"))
            self.assertIn("_supervisor_meta", saved)
            self.assertIn("soundness_fingerprint", saved["_supervisor_meta"])

    def test_nl_verification_records_soundness_cache_per_approved_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".agent-supervisor"
            state_dir.mkdir()
            (repo / "Tablet").mkdir()
            for name in ("approved_node", "rejected_node"):
                (repo / "Tablet" / f"{name}.lean").write_text(
                    f"theorem {name} : True := by\n  sorry\n",
                    encoding="utf-8",
                )
                (repo / "Tablet" / f"{name}.tex").write_text(
                    f"\\begin{{theorem}}[{name}]\nTrue.\n\\end{{theorem}}\n\\begin{{proof}}\nProof.\n\\end{{proof}}\n",
                    encoding="utf-8",
                )

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "approved_node": TabletNode(name="approved_node", kind="paper_intermediate", status="open"),
                "rejected_node": TabletNode(name="rejected_node", kind="paper_intermediate", status="open"),
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
                "node_names": ["approved_node", "rejected_node"],
                "agent_results": [
                    {
                        "correspondence": {"decision": "PASS", "issues": []},
                        "paper_faithfulness": {"decision": "PASS", "issues": []},
                    }
                ],
            }
            sound_result = [{
                "check": "nl_proof",
                "overall": "REJECT",
                "node_names": ["approved_node", "rejected_node"],
                "node_verdicts": [
                    {"node": "approved_node", "overall": "APPROVE"},
                    {"node": "rejected_node", "overall": "REJECT"},
                ],
            }]

            from lagent_tablets.nl_cache import NLCache
            nl_cache = NLCache(state_dir / "nl_cache.json")

            with patch("lagent_tablets.cycle._run_multi_correspondence", return_value=corr_result):
                with patch("lagent_tablets.cycle._run_per_node_soundness", return_value=sound_result):
                    with patch("lagent_tablets.cycle.save_tablet"):
                        _run_nl_verification(
                            config,
                            Policy(),
                            tablet,
                            ["approved_node", "rejected_node"],
                            cycle=9,
                            log_dir=repo,
                            nl_cache=nl_cache,
                            human_input="",
                        )

            self.assertTrue(nl_cache.is_soundness_cached(repo, "approved_node"))
            self.assertFalse(nl_cache.is_soundness_cached(repo, "rejected_node"))

    def test_select_theorem_soundness_target_keeps_unresolved_preferred_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".agent-supervisor"
            (state_dir / "staging").mkdir(parents=True)
            (repo / "Tablet").mkdir()

            def write_node(name: str, *, imports: list[str] | None = None) -> None:
                import_lines = "".join(f"import Tablet.{dep}\n" for dep in (imports or []))
                (repo / "Tablet" / f"{name}.lean").write_text(
                    f"{import_lines}theorem {name} : True := by\n  sorry\n",
                    encoding="utf-8",
                )
                (repo / "Tablet" / f"{name}.tex").write_text(
                    f"\\begin{{theorem}}[{name}]\nTrue.\n\\end{{theorem}}\n\\begin{{proof}}\nProof.\n\\end{{proof}}\n",
                    encoding="utf-8",
                )

            write_node("top", imports=["mid"])
            write_node("mid", imports=["leaf"])
            write_node("leaf")

            for i in range(3):
                payload = {
                    "node": "mid",
                    "soundness": {"decision": "SOUND", "explanation": "ok"},
                    "overall": "APPROVE",
                    "summary": "ok",
                }
                canonical = repo / f"nl_proof_mid_{i}.json"
                raw = raw_json_path(state_dir, f"nl_proof_mid_{i}.json")
                done = done_marker_path(state_dir, f"nl_proof_mid_{i}.json")
                canonical.write_text(json.dumps(payload), encoding="utf-8")
                raw.write_text(json.dumps(payload), encoding="utf-8")
                done.write_text("", encoding="utf-8")

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "top": TabletNode(name="top", kind="paper_intermediate", status="open"),
                "mid": TabletNode(name="mid", kind="paper_intermediate", status="open"),
                "leaf": TabletNode(name="leaf", kind="paper_intermediate", status="open"),
            })
            config = SimpleNamespace(
                repo_path=repo,
                state_dir=state_dir,
                verification=SimpleNamespace(
                    soundness_agents=[SimpleNamespace(provider="claude", model="a", label="A") for _ in range(3)],
                ),
            )

            target = _select_theorem_soundness_target(
                config,
                tablet,
                ["top", "mid", "leaf"],
                soundness_agents=config.verification.soundness_agents,
                disagree_bias="reject",
                preferred="leaf",
            )

        self.assertEqual(target, "leaf")

    def test_validate_theorem_target_repair_changes_only_allows_target_tex(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir()
            (tablet_dir / "target.lean").write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")
            (tablet_dir / "target.tex").write_text("\\begin{theorem}True\\end{theorem}\n", encoding="utf-8")
            (tablet_dir / "child.tex").write_text("\\begin{lemma}True\\end{lemma}\n", encoding="utf-8")

            from lagent_tablets.cycle import _snapshot_tablet_dir

            snapshot = _snapshot_tablet_dir(repo)
            (tablet_dir / "target.tex").write_text("\\begin{theorem}Still true\\end{theorem}\n", encoding="utf-8")
            self.assertIsNone(_validate_theorem_target_repair_changes(repo, "target", snapshot))

            snapshot = _snapshot_tablet_dir(repo)
            (tablet_dir / "child.tex").write_text("\\begin{lemma}Changed\\end{lemma}\n", encoding="utf-8")
            error = _validate_theorem_target_repair_changes(repo, "target", snapshot)
            self.assertIsNotNone(error)
            self.assertIn("only allows editing `target.tex`", error)

    def test_validate_easy_proof_repair_changes_only_allows_active_lean(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tablet_dir = repo / "Tablet"
            tablet_dir.mkdir()
            (tablet_dir / "target.lean").write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")
            (tablet_dir / "target.tex").write_text("\\begin{theorem}True\\end{theorem}\n", encoding="utf-8")
            (tablet_dir / "child.lean").write_text("theorem child : True := by\n  sorry\n", encoding="utf-8")
            (tablet_dir / "child.tex").write_text("\\begin{lemma}True\\end{lemma}\n", encoding="utf-8")

            snapshot = _snapshot_tablet_dir(repo)
            (tablet_dir / "target.lean").write_text("theorem target : True := by\n  trivial\n", encoding="utf-8")
            error, created = _validate_easy_proof_repair_changes(repo, "target", snapshot)
            self.assertIsNone(error)
            self.assertEqual(created, [])

            snapshot = _snapshot_tablet_dir(repo)
            (tablet_dir / "target.tex").write_text("\\begin{theorem}Changed\\end{theorem}\n", encoding="utf-8")
            error, created = _validate_easy_proof_repair_changes(repo, "target", snapshot)
            self.assertIsNotNone(error)
            self.assertEqual(created, [])
            self.assertIn("only allows editing `target.lean`", error)

            snapshot = _snapshot_tablet_dir(repo)
            (tablet_dir / "extra.lean").write_text("theorem extra : True := by\n  trivial\n", encoding="utf-8")
            error, created = _validate_easy_proof_repair_changes(repo, "target", snapshot)
            self.assertIsNotNone(error)
            self.assertEqual(created, ["extra.lean"])

    def test_run_nl_verification_limits_soundness_to_current_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".agent-supervisor"
            state_dir.mkdir()
            (repo / "Tablet").mkdir()
            for name in ("top", "mid"):
                (repo / "Tablet" / f"{name}.lean").write_text(
                    f"theorem {name} : True := by\n  sorry\n",
                    encoding="utf-8",
                )
                (repo / "Tablet" / f"{name}.tex").write_text(
                    f"\\begin{{theorem}}[{name}]\nTrue.\n\\end{{theorem}}\n\\begin{{proof}}\nProof.\n\\end{{proof}}\n",
                    encoding="utf-8",
                )

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "top": TabletNode(name="top", kind="paper_intermediate", status="open"),
                "mid": TabletNode(name="mid", kind="paper_intermediate", status="open"),
            })
            config = SimpleNamespace(
                repo_path=repo,
                state_dir=state_dir,
                workflow=SimpleNamespace(paper_tex_path=None),
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
                tmux=SimpleNamespace(session_name="test", burst_user="worker"),
            )
            corr_result = {
                "check": "correspondence",
                "overall": "APPROVE",
                "node_names": ["top", "mid"],
                "agent_results": [
                    {
                        "correspondence": {"decision": "PASS", "issues": []},
                        "paper_faithfulness": {"decision": "PASS", "issues": []},
                    }
                ],
            }

            observed: dict[str, Any] = {}

            def fake_soundness(*args, **kwargs):
                observed["node_names"] = list(args[2])
                return [{
                    "check": "nl_proof",
                    "overall": "APPROVE",
                    "node_names": list(args[2]),
                    "node_verdicts": [
                        {"node": list(args[2])[0], "overall": "APPROVE"},
                    ],
                }]

            with patch("lagent_tablets.cycle._run_multi_correspondence", return_value=corr_result):
                with patch("lagent_tablets.cycle._run_per_node_soundness", side_effect=fake_soundness):
                    _run_nl_verification(
                        config,
                        Policy(),
                        tablet,
                        ["top", "mid"],
                        cycle=9,
                        log_dir=repo,
                        nl_cache=None,
                        human_input="",
                        soundness_target_node="mid",
                    )

            self.assertEqual(observed["node_names"], ["mid"])

    def test_run_nl_verification_uses_policy_selected_soundness_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".agent-supervisor"
            state_dir.mkdir()
            (repo / "Tablet").mkdir()
            for name in ("top",):
                (repo / "Tablet" / f"{name}.lean").write_text(
                    f"theorem {name} : True := by\n  sorry\n",
                    encoding="utf-8",
                )
                (repo / "Tablet" / f"{name}.tex").write_text(
                    f"\\begin{{theorem}}[{name}]\nTrue.\n\\end{{theorem}}\n\\begin{{proof}}\nProof.\n\\end{{proof}}\n",
                    encoding="utf-8",
                )

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "top": TabletNode(name="top", kind="paper_intermediate", status="open"),
            })
            config = SimpleNamespace(
                repo_path=repo,
                state_dir=state_dir,
                workflow=SimpleNamespace(paper_tex_path=None),
                verification=SimpleNamespace(
                    provider="claude",
                    model="claude-opus-4-6",
                    extra_args=[],
                    correspondence_agents=[
                        SimpleNamespace(provider="claude", model="a", label="Claude"),
                        SimpleNamespace(provider="gemini", model="b", label="Gemini"),
                        SimpleNamespace(provider="codex", model="c", label="Codex"),
                    ],
                    soundness_agents=[
                        SimpleNamespace(provider="claude", model="a", label="Claude"),
                        SimpleNamespace(provider="gemini", model="b", label="Gemini"),
                        SimpleNamespace(provider="codex", model="c", label="Codex"),
                    ],
                ),
                tmux=SimpleNamespace(session_name="test", burst_user="worker"),
            )
            corr_result = {
                "check": "correspondence",
                "overall": "APPROVE",
                "node_names": ["top"],
                "agent_results": [
                    {
                        "correspondence": {"decision": "PASS", "issues": []},
                        "paper_faithfulness": {"decision": "PASS", "issues": []},
                    }
                ],
            }
            seen = {}

            def fake_soundness(*args, **kwargs):
                seen["labels"] = [a.label for a in args[3]]
                return [{
                    "check": "nl_proof",
                    "overall": "APPROVE",
                    "node_names": ["top"],
                    "node_verdicts": [{"node": "top", "overall": "APPROVE"}],
                }]

            with patch("lagent_tablets.cycle._run_multi_correspondence", return_value=corr_result):
                with patch("lagent_tablets.cycle._run_per_node_soundness", side_effect=fake_soundness):
                    _run_nl_verification(
                        config,
                        Policy(verification=VerificationPolicy(soundness_agent_selectors=("gemini", "codex"), soundness_disagree_bias="reject")),
                        tablet,
                        ["top"],
                        cycle=9,
                        log_dir=repo,
                        nl_cache=None,
                        human_input="",
                    )

            self.assertEqual(seen["labels"], ["Gemini", "Codex"])

    def test_per_node_soundness_two_agent_split_defaults_to_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            state_dir = repo / ".agent-supervisor"
            (state_dir / "staging").mkdir(parents=True)
            (repo / "Tablet").mkdir()
            (repo / "Tablet" / "foo.lean").write_text(
                "theorem foo : True := by\n  sorry\n",
                encoding="utf-8",
            )
            (repo / "Tablet" / "foo.tex").write_text(
                "\\begin{theorem}[foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n",
                encoding="utf-8",
            )

            tablet = TabletState(nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "foo": TabletNode(name="foo", kind="paper_intermediate", status="open"),
            })
            config = SimpleNamespace(
                repo_path=repo,
                state_dir=state_dir,
                tmux=SimpleNamespace(session_name="test", burst_user="worker"),
            )
            agents = [
                SimpleNamespace(provider="gemini", model="g", label="Gemini"),
                SimpleNamespace(provider="codex", model="c", label="Codex"),
            ]
            returned = [
                {
                    "agent": "Gemini",
                    "node": "foo",
                    "index": 0,
                    "ok": True,
                    "overall": "APPROVE",
                    "summary": "ok",
                    "soundness": {"decision": "SOUND", "explanation": "ok"},
                },
                {
                    "agent": "Codex",
                    "node": "foo",
                    "index": 1,
                    "ok": True,
                    "overall": "REJECT",
                    "summary": "gap",
                    "soundness": {"decision": "UNSOUND", "explanation": "gap"},
                },
            ]

            with patch("lagent_tablets.cycle._run_single_node_soundness", side_effect=returned):
                results = _run_per_node_soundness(
                    config,
                    tablet,
                    ["foo"],
                    agents,
                    disagree_bias="reject",
                    paper_tex="",
                    human_input="",
                    log_dir=repo,
                )

            verdict = results[0]["node_verdicts"][0]
            self.assertEqual(verdict["overall"], "REJECT")
            self.assertTrue(verdict["panel_split"])
            self.assertEqual(verdict["disagree_bias"], "reject")


if __name__ == "__main__":
    unittest.main()
