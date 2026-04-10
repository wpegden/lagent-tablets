"""Tests for canonical viewer snapshots and legacy backfill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lagent_tablets.nl_cache import correspondence_fingerprint, soundness_fingerprint
from lagent_tablets.state import SupervisorState, TabletState
from lagent_tablets.viewer_state import (
    backfill_cache_dir,
    build_legacy_backfill_viewer_state,
    build_live_viewer_state,
    repo_cache_slug,
    write_cycle_viewer_state,
    write_live_viewer_state,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _commit(repo: Path, message: str, tag: str) -> None:
    _git(repo, "add", ".")
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "tag", tag)


@unittest.skipUnless(shutil.which("node"), "node is required for viewer server tests")
class TestViewerSnapshots(unittest.TestCase):
    def test_live_snapshot_keeps_correspondence_on_proof_only_change(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "foo.lean").write_text(
            "theorem foo : True := by\n  trivial\n",
            encoding="utf-8",
        )
        (repo / "Tablet" / "foo.tex").write_text(
            "\\begin{theorem}[Foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nOld proof.\n\\end{proof}\n",
            encoding="utf-8",
        )

        tablet = TabletState.from_dict({
            "nodes": {
                "foo": {
                    "kind": "helper_lemma",
                    "status": "open",
                    "correspondence_status": "pass",
                    "soundness_status": "pass",
                    "correspondence_content_hash": correspondence_fingerprint(repo, "foo"),
                    "soundness_content_hash": soundness_fingerprint(repo, "foo"),
                }
            }
        })
        state = SupervisorState(cycle=1, phase="theorem_stating")

        (repo / "Tablet" / "foo.tex").write_text(
            "\\begin{theorem}[Foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nNew proof details.\n\\end{proof}\n",
            encoding="utf-8",
        )

        snapshot = build_live_viewer_state(repo, tablet, state)
        self.assertEqual(snapshot["nodes"]["foo"]["verification"]["correspondence"], "pass")
        self.assertEqual(snapshot["nodes"]["foo"]["verification"]["nl_proof"], "?")

    def test_live_snapshot_keeps_parent_soundness_on_child_proof_only_change(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "child.lean").write_text("theorem child : True := by\n  trivial\n", encoding="utf-8")
        (repo / "Tablet" / "child.tex").write_text(
            "\\begin{lemma}[child]\nTrue.\n\\end{lemma}\n\\begin{proof}\nOld.\n\\end{proof}\n",
            encoding="utf-8",
        )
        (repo / "Tablet" / "parent.lean").write_text(
            "import Tablet.child\n\ntheorem parent : True := by\n  trivial\n",
            encoding="utf-8",
        )
        (repo / "Tablet" / "parent.tex").write_text(
            "\\begin{theorem}[parent]\nTrue.\n\\end{theorem}\n\\begin{proof}\nBy \\noderef{child}.\n\\end{proof}\n",
            encoding="utf-8",
        )

        tablet = TabletState.from_dict({
            "nodes": {
                "child": {
                    "kind": "helper_lemma",
                    "status": "open",
                    "correspondence_status": "pass",
                    "soundness_status": "pass",
                    "correspondence_content_hash": correspondence_fingerprint(repo, "child"),
                    "soundness_content_hash": soundness_fingerprint(repo, "child"),
                },
                "parent": {
                    "kind": "helper_lemma",
                    "status": "open",
                    "correspondence_status": "pass",
                    "soundness_status": "pass",
                    "correspondence_content_hash": correspondence_fingerprint(repo, "parent"),
                    "soundness_content_hash": soundness_fingerprint(repo, "parent"),
                },
            }
        })
        state = SupervisorState(cycle=1, phase="theorem_stating")

        (repo / "Tablet" / "child.tex").write_text(
            "\\begin{lemma}[child]\nTrue.\n\\end{lemma}\n\\begin{proof}\nNew child proof details.\n\\end{proof}\n",
            encoding="utf-8",
        )

        snapshot = build_live_viewer_state(repo, tablet, state)
        self.assertEqual(snapshot["nodes"]["parent"]["verification"]["correspondence"], "pass")
        self.assertEqual(snapshot["nodes"]["parent"]["verification"]["nl_proof"], "pass")

    def test_fast_live_snapshot_skips_semantic_verification_refresh(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "foo.lean").write_text("theorem foo : True := by\n  trivial\n", encoding="utf-8")
        (repo / "Tablet" / "foo.tex").write_text("\\begin{theorem}[Foo]\nTrue.\n\\end{theorem}\n", encoding="utf-8")

        tablet = TabletState.from_dict({
            "nodes": {
                "foo": {
                    "kind": "helper_lemma",
                    "status": "open",
                    "correspondence_status": "pass",
                    "soundness_status": "fail",
                }
            }
        })
        state = SupervisorState(cycle=9, phase="theorem_stating")

        with patch("lagent_tablets.viewer_state._live_verification_statuses", side_effect=AssertionError("should not run")):
            snapshot = build_live_viewer_state(repo, tablet, state, fast=True)

        self.assertEqual(snapshot["nodes"]["foo"]["verification"]["correspondence"], "pass")
        self.assertEqual(snapshot["nodes"]["foo"]["verification"]["nl_proof"], "fail")

    def test_write_live_snapshot_mirrors_project_static_files(self) -> None:
        base = Path(tempfile.mkdtemp())
        repo = base / "extremal_tablets"
        static_out = base / "static"
        (repo / "Tablet").mkdir(parents=True)
        (repo / ".agent-supervisor").mkdir()
        (static_out).mkdir()
        (static_out / "index.html").write_text("<html>viewer</html>\n", encoding="utf-8")
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "foo.lean").write_text("theorem foo : True := by\n  trivial\n", encoding="utf-8")
        (repo / "Tablet" / "foo.tex").write_text("\\begin{theorem}[Foo]\nTrue.\n\\end{theorem}\n", encoding="utf-8")

        tablet = TabletState.from_dict({
            "nodes": {
                "foo": {
                    "kind": "helper_lemma",
                    "status": "open",
                    "correspondence_status": "pass",
                    "soundness_status": "fail",
                }
            }
        })
        state = SupervisorState(cycle=0, phase="theorem_stating")

        with patch.dict(os.environ, {"LAGENT_VIEWER_STATIC_OUT": str(static_out)}, clear=False):
            write_live_viewer_state(repo / ".agent-supervisor" / "viewer_state.json", repo, tablet, state, fast=True)

        root_live = json.loads((static_out / "api" / "viewer-state.json").read_text(encoding="utf-8"))
        project_live = json.loads((static_out / "extremal" / "api" / "viewer-state.json").read_text(encoding="utf-8"))
        project_cycles = json.loads((static_out / "extremal" / "api" / "cycles.json").read_text(encoding="utf-8"))
        self.assertEqual(root_live["state"]["cycle"], 0)
        self.assertEqual(project_live["state"]["cycle"], 0)
        self.assertEqual(project_cycles, [])
        self.assertTrue((static_out / "extremal" / "index.html").exists())

    def test_node_payload_keeps_preamble_import_for_layout(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "foo.lean").write_text(
            "import Tablet.Preamble\n\ntheorem foo : True := by\n  trivial\n",
            encoding="utf-8",
        )
        (repo / "Tablet" / "foo.tex").write_text("\\begin{theorem}[Foo]\nTrue.\n\\end{theorem}\n", encoding="utf-8")
        tablet = TabletState.from_dict({
            "nodes": {"foo": {"kind": "helper_lemma", "status": "open"}}
        })
        state = SupervisorState(cycle=1, phase="theorem_stating")

        snapshot = build_live_viewer_state(repo, tablet, state)
        self.assertEqual(snapshot["nodes"]["foo"]["imports"], ["Preamble"])

    def test_legacy_backfill_marks_early_correspondence_cycles_as_worked(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "foo.lean").write_text("theorem foo : True := by\n  trivial\n", encoding="utf-8")
        (repo / "Tablet" / "foo.tex").write_text("\\begin{theorem}[Foo]\nTrue.\n\\end{theorem}\n", encoding="utf-8")
        (repo / ".agent-supervisor" / "tablet.json").write_text(json.dumps({
            "nodes": {"foo": {"kind": "helper_lemma", "status": "open", "correspondence_status": "fail"}}
        }), encoding="utf-8")
        (repo / ".agent-supervisor" / "state.json").write_text(json.dumps({
            "cycle": 1, "phase": "theorem_stating"
        }), encoding="utf-8")
        (repo / ".agent-supervisor" / "cycle_meta.json").write_text(json.dumps({
            "cycle": 1,
            "phase": "theorem_stating",
            "verification_results": [{"check": "correspondence", "overall": "REJECT"}],
        }), encoding="utf-8")
        _git(repo, "init")
        _commit(repo, "cycle 1", "cycle-1")

        payload = build_legacy_backfill_viewer_state(repo, 1)
        self.assertTrue(payload["nodes"]["foo"]["activity"]["correspondence"])
        self.assertFalse(payload["nodes"]["foo"]["activity"]["soundness"])

    def test_legacy_backfill_uses_soundness_raw_results(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "foo.lean").write_text("theorem foo : True := by\n  trivial\n", encoding="utf-8")
        (repo / "Tablet" / "foo.tex").write_text(
            "\\begin{theorem}[Foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n",
            encoding="utf-8",
        )
        (repo / ".agent-supervisor" / "tablet.json").write_text(json.dumps({
            "nodes": {"foo": {"kind": "helper_lemma", "status": "open", "correspondence_status": "pass"}}
        }), encoding="utf-8")
        (repo / ".agent-supervisor" / "state.json").write_text(json.dumps({
            "cycle": 2, "phase": "theorem_stating", "theorem_soundness_target": "foo"
        }), encoding="utf-8")
        (repo / ".agent-supervisor" / "cycle_meta.json").write_text(json.dumps({
            "cycle": 2,
            "phase": "theorem_stating",
            "verification_results": [{"check": "nl_proof", "overall": "REJECT", "node_names": ["foo"]}],
        }), encoding="utf-8")
        (repo / "nl_proof_foo_0.json").write_text(json.dumps({
            "overall": "APPROVE",
            "soundness": {"decision": "SOUND"},
        }), encoding="utf-8")
        (repo / "nl_proof_foo_1.json").write_text(json.dumps({
            "overall": "REJECT",
            "soundness": {"decision": "STRUCTURAL"},
        }), encoding="utf-8")
        _git(repo, "init")
        _commit(repo, "cycle 2", "cycle-2")

        payload = build_legacy_backfill_viewer_state(repo, 2)
        self.assertTrue(payload["nodes"]["foo"]["activity"]["soundness"])
        self.assertEqual(payload["nodes"]["foo"]["verification"]["nl_proof"], "structural")

    def test_server_write_static_prefers_tagged_viewer_state_then_backfill(self) -> None:
        repo = Path(tempfile.mkdtemp())
        static_out = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / ".agent-supervisor" / "state.json").write_text(json.dumps({"cycle": 2}), encoding="utf-8")
        (repo / ".agent-supervisor" / "tablet.json").write_text(json.dumps({"nodes": {}}), encoding="utf-8")
        (repo / ".agent-supervisor" / "viewer_state.json").write_text(json.dumps({
            "state": {"cycle": 2},
            "tablet": {"nodes": {}},
            "nodes": {},
            "meta": {"source": "live"},
        }), encoding="utf-8")
        _git(repo, "init")

        # cycle-1: legacy backfill only
        (repo / ".agent-supervisor" / "viewer_state.json").unlink()
        (repo / ".agent-supervisor" / "cycle_meta.json").write_text(json.dumps({"cycle": 1}), encoding="utf-8")
        _commit(repo, "cycle 1", "cycle-1")
        cache_dir = backfill_cache_dir(static_out, repo)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "1.json").write_text(json.dumps({
            "state": {"cycle": 1},
            "tablet": {"nodes": {}},
            "nodes": {"foo": {"activity": {"correspondence": True, "soundness": False, "worker": False, "reviewer": False}}},
            "meta": {"source": "backfill"},
        }), encoding="utf-8")

        # cycle-2: committed viewer_state
        (repo / ".agent-supervisor" / "viewer_state.json").write_text(json.dumps({
            "state": {"cycle": 2},
            "tablet": {"nodes": {}},
            "nodes": {"bar": {"activity": {"correspondence": False, "soundness": True, "worker": False, "reviewer": False}}},
            "meta": {"source": "git"},
        }), encoding="utf-8")
        (repo / ".agent-supervisor" / "cycle_meta.json").write_text(json.dumps({"cycle": 2}), encoding="utf-8")
        _commit(repo, "cycle 2", "cycle-2")

        script = """
const { writeStatic } = require('./viewer/server.js');
writeStatic();
"""
        subprocess.run(
            ["node", "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            env={
                **os.environ,
                "REPO_PATH": str(repo),
                "STATIC_OUT": str(static_out),
                "BASE_PATH": "/lagent-tablets",
                "VIEWER_PROJECTS_FILE": str(repo / "viewer-projects.json"),
            },
            check=True,
            capture_output=True,
            text=True,
        )

        cycle1 = json.loads((static_out / "api" / "state-at" / "1.json").read_text(encoding="utf-8"))
        cycle2 = json.loads((static_out / "api" / "state-at" / "2.json").read_text(encoding="utf-8"))
        self.assertEqual(cycle1["meta"]["source"], "backfill")
        self.assertEqual(cycle2["meta"]["source"], "git")

    def test_cycle_writer_emits_committed_viewer_state(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "foo.lean").write_text("theorem foo : True := by\n  trivial\n", encoding="utf-8")
        (repo / "Tablet" / "foo.tex").write_text(
            "\\begin{theorem}[Foo]\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n",
            encoding="utf-8",
        )
        tablet = TabletState.from_dict({
            "nodes": {
                "foo": {
                    "kind": "helper_lemma",
                    "status": "open",
                    "correspondence_status": "pass",
                }
            }
        })
        state = SupervisorState(cycle=3, phase="theorem_stating")
        out = repo / ".agent-supervisor" / "viewer_state.json"

        write_cycle_viewer_state(
            out,
            repo,
            tablet,
            state,
            verification_results=[{"check": "nl_proof", "overall": "APPROVE", "node_names": ["foo"]}],
        )
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(payload["nodes"]["foo"]["activity"]["soundness"])
        self.assertEqual(payload["nodes"]["foo"]["verification"]["nl_proof"], "pass")
