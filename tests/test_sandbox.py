from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lagent_tablets.config import SandboxConfig
from lagent_tablets.sandbox import probe_sandbox, wrap_command


class TestSandboxCommand(unittest.TestCase):
    def test_wrap_command_passthrough_when_disabled(self):
        repo = Path(tempfile.mkdtemp())
        cmd = wrap_command(
            ["bash", "-lc", "pwd"],
            sandbox=SandboxConfig(enabled=False, backend="bwrap"),
            work_dir=repo,
            burst_user=None,
            burst_home=repo / "home",
        )
        self.assertEqual(cmd, ["bash", "-lc", "pwd"])

    def test_wrap_command_uses_bwrap_with_expected_binds(self):
        repo = Path(tempfile.mkdtemp())
        home = Path(tempfile.mkdtemp())
        with patch("lagent_tablets.sandbox.bwrap_available", return_value=True):
            cmd = wrap_command(
                ["bash", "-lc", "pwd"],
                sandbox=SandboxConfig(enabled=True, backend="bwrap"),
                work_dir=repo,
                burst_user=None,
                burst_home=home,
            )
        rendered = " ".join(cmd)
        self.assertEqual(cmd[0], "bwrap")
        self.assertIn("--bind", rendered)
        self.assertIn(str(repo), rendered)
        self.assertIn(str(home), rendered)
        self.assertNotIn("extremal_vectors_tablets", rendered)

    def test_real_bwrap_hides_unmounted_sibling_path(self):
        if shutil.which("bwrap") is None:
            self.skipTest("bwrap not installed")
        repo = Path(tempfile.mkdtemp())
        sibling = Path(tempfile.mkdtemp())
        home = Path(tempfile.mkdtemp())
        ok, detail = probe_sandbox(
            sandbox=SandboxConfig(enabled=True, backend="bwrap"),
            work_dir=repo,
            burst_user=None,
            burst_home=home,
        )
        if not ok:
            self.skipTest(f"bwrap unusable on this host: {detail}")
        marker = repo / "marker.txt"
        hidden = sibling / "hidden.txt"
        marker.write_text("project\n", encoding="utf-8")
        hidden.write_text("sibling\n", encoding="utf-8")
        cmd = wrap_command(
            [
                "bash",
                "-lc",
                f"test -r {marker} && cat {marker} >/dev/null && test ! -e {hidden}",
            ],
            sandbox=SandboxConfig(enabled=True, backend="bwrap"),
            work_dir=repo,
            burst_user=None,
            burst_home=home,
        )
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
