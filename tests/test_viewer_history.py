"""Regression tests for historical viewer snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "node is required for viewer tests")
class TestViewerHistoricalState(unittest.TestCase):
    """Ensure historical viewer state comes from the tagged snapshot."""

    def test_historical_snapshots_use_tagged_files_and_statuses(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")

        old_lean = "import Tablet.dep_old\n\ntheorem foo : True := by\n  trivial\n"
        old_tex = "\\begin{theorem}[Old Title]\nOld statement.\n\\end{theorem}\n"
        old_hash = hashlib.sha256(old_lean.encode("utf-8") + old_tex.encode("utf-8")).hexdigest()[:16]

        (repo / "Tablet" / "foo.lean").write_text(old_lean, encoding="utf-8")
        (repo / "Tablet" / "foo.tex").write_text(old_tex, encoding="utf-8")
        (repo / ".agent-supervisor" / "tablet.json").write_text(json.dumps({
            "nodes": {
                "foo": {
                    "kind": "helper_lemma",
                    "status": "open",
                    "correspondence_status": "pass",
                    "soundness_status": "pass",
                    "verification_content_hash": old_hash,
                }
            }
        }), encoding="utf-8")
        (repo / ".agent-supervisor" / "state.json").write_text(json.dumps({"cycle": 1}), encoding="utf-8")

        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run([
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-m", "cycle 1",
        ], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "tag", "cycle-1"], cwd=repo, check=True, capture_output=True, text=True)

        new_lean = "import Tablet.dep_new\n\ntheorem foo : False := by\n  sorry\n"
        new_tex = "\\begin{theorem}[New Title]\nNew statement.\n\\end{theorem}\n"
        (repo / "Tablet" / "foo.lean").write_text(new_lean, encoding="utf-8")
        (repo / "Tablet" / "foo.tex").write_text(new_tex, encoding="utf-8")

        script = """
const { buildNodes, createFsSnapshot, createGitSnapshot, getVerificationStatus } = require('./viewer/state.js');
const { execFileSync } = require('child_process');
const repo = process.env.REPO_PATH;
const tablet = JSON.parse(execFileSync('git', ['show', 'cycle-1:.agent-supervisor/tablet.json'], {
  cwd: repo,
  encoding: 'utf8',
}));
const live = getVerificationStatus(tablet, createFsSnapshot(repo));
const histSnapshot = createGitSnapshot(repo, 'cycle-1');
const hist = getVerificationStatus(tablet, histSnapshot);
const histNodes = buildNodes(tablet, histSnapshot);
process.stdout.write(JSON.stringify({
  live: live.foo,
  hist: hist.foo,
  preamble: histNodes.Preamble,
  histNode: {
    title: histNodes.foo.title,
    imports: histNodes.foo.imports,
    declaration: histNodes.foo.declaration,
    texContent: histNodes.foo.texContent,
  },
}));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "REPO_PATH": str(repo)},
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(payload["live"]["correspondence"], "?")
        self.assertEqual(payload["live"]["nl_proof"], "?")
        self.assertEqual(payload["hist"]["correspondence"], "pass")
        self.assertEqual(payload["hist"]["nl_proof"], "pass")
        self.assertEqual(payload["preamble"]["title"], "Preamble")
        self.assertEqual(payload["histNode"]["title"], "Old Title")
        self.assertEqual(payload["histNode"]["imports"], ["dep_old"])
        self.assertIn("theorem foo : True := by", payload["histNode"]["declaration"])
        self.assertEqual(payload["histNode"]["texContent"], old_tex)

    def test_build_nodes_parses_corollary_environment(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "Tablet").mkdir()
        (repo / ".agent-supervisor").mkdir()
        (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Fin.Basic\n", encoding="utf-8")
        (repo / "Tablet" / "bar.lean").write_text(
            "theorem bar : True := by\n  trivial\n",
            encoding="utf-8",
        )
        (repo / "Tablet" / "bar.tex").write_text(
            "\\begin{corollary}[Cor Title]\nStatement.\n\\end{corollary}\n",
            encoding="utf-8",
        )
        (repo / ".agent-supervisor" / "tablet.json").write_text(json.dumps({
            "nodes": {
                "bar": {
                    "kind": "paper_intermediate",
                    "status": "open",
                }
            }
        }), encoding="utf-8")

        script = """
const { buildNodes, createFsSnapshot } = require('./viewer/state.js');
const fs = require('fs');
const repo = process.env.REPO_PATH;
const tablet = JSON.parse(fs.readFileSync(repo + '/.agent-supervisor/tablet.json', 'utf8'));
const nodes = buildNodes(tablet, createFsSnapshot(repo));
process.stdout.write(JSON.stringify(nodes.bar));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "REPO_PATH": str(repo)},
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(payload["texEnv"], "corollary")
        self.assertEqual(payload["title"], "Cor Title")
