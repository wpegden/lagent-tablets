"""Tests for state persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lagent_tablets.state import (
    SupervisorState,
    TabletNode,
    TabletState,
    append_jsonl,
    load_json,
    load_state,
    load_tablet,
    normalize_orphan_resolutions,
    save_json,
    save_state,
    save_tablet,
    timestamp_now,
)


class TestAtomicJson(unittest.TestCase):

    def test_save_and_load_round_trip(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "test.json"
        data = {"key": "value", "number": 42, "nested": {"a": [1, 2, 3]}}
        save_json(path, data)
        loaded = load_json(path)
        self.assertEqual(loaded, data)

    def test_load_missing_returns_default(self):
        path = Path(tempfile.mkdtemp()) / "missing.json"
        self.assertIsNone(load_json(path))
        self.assertEqual(load_json(path, {"default": True}), {"default": True})

    def test_save_creates_parent_dirs(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "sub" / "dir" / "test.json"
        save_json(path, {"ok": True})
        self.assertTrue(path.exists())
        self.assertEqual(load_json(path), {"ok": True})

    def test_save_with_mode(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "test.json"
        save_json(path, {"ok": True}, mode=0o640)
        actual_mode = path.stat().st_mode & 0o777
        self.assertEqual(actual_mode, 0o640)

    def test_append_jsonl(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "log.jsonl"
        append_jsonl(path, {"event": "first"})
        append_jsonl(path, {"event": "second"})
        lines = path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["event"], "first")
        self.assertEqual(json.loads(lines[1])["event"], "second")


class TestTabletNode(unittest.TestCase):

    def test_round_trip(self):
        node = TabletNode(
            name="compactness_of_K",
            kind="paper_intermediate",
            status="closed",
            title="Compactness of K",
            paper_provenance="Lemma 2.1",
            lean_statement_hash="sha256:abc",
            closed_at_cycle=23,
            correspondence_content_hash="corr123",
            soundness_content_hash="sound456",
        )
        d = node.to_dict()
        restored = TabletNode.from_dict("compactness_of_K", d)
        self.assertEqual(restored.name, "compactness_of_K")
        self.assertEqual(restored.kind, "paper_intermediate")
        self.assertEqual(restored.status, "closed")
        self.assertEqual(restored.title, "Compactness of K")
        self.assertEqual(restored.paper_provenance, "Lemma 2.1")
        self.assertEqual(restored.closed_at_cycle, 23)
        self.assertEqual(restored.correspondence_content_hash, "corr123")
        self.assertEqual(restored.soundness_content_hash, "sound456")

    def test_legacy_verification_hash_populates_split_hashes(self):
        restored = TabletNode.from_dict("foo", {
            "kind": "helper_lemma",
            "status": "open",
            "verification_content_hash": "legacyhash",
        })
        self.assertEqual(restored.correspondence_content_hash, "legacyhash")
        self.assertEqual(restored.soundness_content_hash, "legacyhash")

    def test_minimal_node(self):
        node = TabletNode(name="helper", kind="helper_lemma", status="open")
        d = node.to_dict()
        self.assertNotIn("paper_provenance", d)
        self.assertNotIn("closed_at_cycle", d)


class TestTabletState(unittest.TestCase):

    def test_empty_tablet(self):
        tablet = TabletState()
        self.assertEqual(tablet.total_nodes, 0)
        self.assertEqual(tablet.closed_nodes, 0)
        self.assertEqual(tablet.open_nodes, 0)

    def test_metrics(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "thm_a": TabletNode(name="thm_a", kind="paper_main_result", status="closed"),
            "thm_b": TabletNode(name="thm_b", kind="paper_main_result", status="open"),
            "helper": TabletNode(name="helper", kind="helper_lemma", status="open"),
        })
        self.assertEqual(tablet.total_nodes, 3)  # excludes preamble
        self.assertEqual(tablet.closed_nodes, 1)
        self.assertEqual(tablet.open_nodes, 2)

    def test_round_trip(self):
        tablet = TabletState(
            nodes={
                "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
                "thm_a": TabletNode(name="thm_a", kind="paper_main_result", status="open", paper_provenance="Thm 1"),
            },
            active_node="thm_a",
            seeded_at_cycle=5,
            last_modified_at_cycle=10,
        )
        d = tablet.to_dict()
        restored = TabletState.from_dict(d)
        self.assertEqual(restored.active_node, "thm_a")
        self.assertEqual(restored.seeded_at_cycle, 5)
        self.assertEqual(len(restored.nodes), 2)
        self.assertEqual(restored.nodes["thm_a"].paper_provenance, "Thm 1")

    def test_save_and_load(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "tablet.json"
        tablet = TabletState(
            nodes={"Preamble": TabletNode(name="Preamble", kind="preamble", status="closed")},
            active_node="",
            seeded_at_cycle=1,
        )
        save_tablet(path, tablet)
        loaded = load_tablet(path)
        self.assertEqual(loaded.seeded_at_cycle, 1)
        self.assertIn("Preamble", loaded.nodes)


class TestSupervisorState(unittest.TestCase):

    def test_defaults(self):
        state = SupervisorState()
        self.assertEqual(state.cycle, 0)
        self.assertEqual(state.phase, "paper_check")
        self.assertEqual(state.active_node, "")
        self.assertEqual(state.theorem_soundness_target, "")
        self.assertEqual(state.theorem_target_edit_mode, "repair")
        self.assertFalse(state.theorem_correspondence_blocked)
        self.assertEqual(state.open_rejections, [])
        self.assertFalse(state.awaiting_human_input)

    def test_round_trip(self):
        state = SupervisorState(
            cycle=42,
            phase="proof_formalization",
            active_node="uniqueness_thm",
            theorem_soundness_target="paper_main",
            theorem_target_edit_mode="restructure",
            theorem_correspondence_blocked=True,
            last_review={"decision": "CONTINUE", "reason": "making progress"},
            open_blockers=[{"node": "foo", "phase": "correspondence", "reason": "statement mismatch"}],
            review_log=[{"cycle": 41, "decision": "CONTINUE"}],
            trusted_main_result_hashes={"main_thm": "fp-main"},
        )
        d = state.to_dict()
        restored = SupervisorState.from_dict(d)
        self.assertEqual(restored.cycle, 42)
        self.assertEqual(restored.phase, "proof_formalization")
        self.assertEqual(restored.active_node, "uniqueness_thm")
        self.assertEqual(restored.theorem_soundness_target, "paper_main")
        self.assertEqual(restored.theorem_target_edit_mode, "restructure")
        self.assertTrue(restored.theorem_correspondence_blocked)
        self.assertEqual(restored.last_review["decision"], "CONTINUE")
        self.assertEqual(restored.open_rejections[0]["node"], "foo")
        self.assertEqual(len(restored.review_log), 1)
        self.assertEqual(restored.trusted_main_result_hashes, {"main_thm": "fp-main"})

    def test_save_and_load(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "state.json"
        state = SupervisorState(cycle=10, phase="planning")
        save_state(path, state)
        loaded = load_state(path)
        self.assertEqual(loaded.cycle, 10)
        self.assertEqual(loaded.phase, "planning")


class TestNormalizeOrphanResolutions(unittest.TestCase):

    def test_filters_invalid_entries_and_deduplicates(self):
        normalized = normalize_orphan_resolutions(
            [
                {
                    "node": "orphan_a",
                    "action": "remove",
                    "reason": "No downstream node needs it.",
                },
                {
                    "node": "orphan_b",
                    "action": "keep_and_add_dependency",
                    "reason": "Needed by the main theorem.",
                    "suggested_parents": ["main_thm", "main_thm", "orphan_b", ""],
                },
                {
                    "node": "orphan_b",
                    "action": "remove",
                    "reason": "duplicate should be ignored",
                },
                {
                    "node": "orphan_c",
                    "action": "keep",
                    "reason": "invalid action",
                },
            ],
            allowed_nodes={"orphan_a", "orphan_b"},
        )

        self.assertEqual(
            normalized,
            [
                {
                    "node": "orphan_a",
                    "action": "remove",
                    "reason": "No downstream node needs it.",
                    "suggested_parents": [],
                },
                {
                    "node": "orphan_b",
                    "action": "keep_and_add_dependency",
                    "reason": "Needed by the main theorem.",
                    "suggested_parents": ["main_thm"],
                },
            ],
        )


class TestTimestamp(unittest.TestCase):

    def test_returns_iso_format(self):
        ts = timestamp_now()
        self.assertIn("T", ts)
        self.assertTrue(ts.endswith("+00:00") or ts.endswith("Z"))


if __name__ == "__main__":
    unittest.main()
