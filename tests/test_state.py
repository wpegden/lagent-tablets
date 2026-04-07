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
        )
        d = node.to_dict()
        restored = TabletNode.from_dict("compactness_of_K", d)
        self.assertEqual(restored.name, "compactness_of_K")
        self.assertEqual(restored.kind, "paper_intermediate")
        self.assertEqual(restored.status, "closed")
        self.assertEqual(restored.title, "Compactness of K")
        self.assertEqual(restored.paper_provenance, "Lemma 2.1")
        self.assertEqual(restored.closed_at_cycle, 23)

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
        self.assertFalse(state.awaiting_human_input)

    def test_round_trip(self):
        state = SupervisorState(
            cycle=42,
            phase="proof_formalization",
            active_node="uniqueness_thm",
            last_review={"decision": "CONTINUE", "reason": "making progress"},
            review_log=[{"cycle": 41, "decision": "CONTINUE"}],
        )
        d = state.to_dict()
        restored = SupervisorState.from_dict(d)
        self.assertEqual(restored.cycle, 42)
        self.assertEqual(restored.phase, "proof_formalization")
        self.assertEqual(restored.active_node, "uniqueness_thm")
        self.assertEqual(restored.last_review["decision"], "CONTINUE")
        self.assertEqual(len(restored.review_log), 1)

    def test_save_and_load(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "state.json"
        state = SupervisorState(cycle=10, phase="planning")
        save_state(path, state)
        loaded = load_state(path)
        self.assertEqual(loaded.cycle, 10)
        self.assertEqual(loaded.phase, "planning")


class TestTimestamp(unittest.TestCase):

    def test_returns_iso_format(self):
        ts = timestamp_now()
        self.assertIn("T", ts)
        self.assertTrue(ts.endswith("+00:00") or ts.endswith("Z"))


if __name__ == "__main__":
    unittest.main()
