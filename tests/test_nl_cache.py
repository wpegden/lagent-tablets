from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from lagent_tablets import nl_cache
from lagent_tablets.nl_cache import correspondence_fingerprint


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_minimal_lake_project(repo: Path) -> None:
    _write(
        repo / "lakefile.lean",
        "import Lake\n"
        "open Lake DSL\n\n"
        "package «tmp» where\n\n"
        "lean_lib Tablet where\n",
    )
    (repo / "Tablet").mkdir(parents=True, exist_ok=True)
    _write(repo / "Tablet" / "Preamble.lean", "")


def _lake_build(repo: Path, target: str) -> None:
    subprocess.run(
        ["lake", "build", target],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@unittest.skipUnless(shutil.which("lake"), "lake is required for Lean semantic fingerprint tests")
class TestLeanAwareCorrespondenceFingerprint(unittest.TestCase):
    def test_imported_theorem_change_does_not_invalidate_parent_correspondence(self) -> None:
        repo = Path(tempfile.mkdtemp())
        _init_minimal_lake_project(repo)
        _write(
            repo / "Tablet" / "helper.lean",
            "import Tablet.Preamble\n\n"
            "theorem helper : True := by\n"
            "  trivial\n",
        )
        _write(
            repo / "Tablet" / "helper.tex",
            "\\begin{lemma}[helper]\nTrue.\n\\end{lemma}\n"
            "\\begin{proof}\nTrivial.\n\\end{proof}\n",
        )
        _write(
            repo / "Tablet" / "parent.lean",
            "import Tablet.Preamble\n"
            "import Tablet.helper\n\n"
            "theorem parent : True := by\n"
            "  trivial\n",
        )
        _write(
            repo / "Tablet" / "parent.tex",
            "\\begin{theorem}[parent]\nTrue.\n\\end{theorem}\n"
            "\\begin{proof}\nTrivial.\n\\end{proof}\n",
        )

        _lake_build(repo, "Tablet.parent")
        before = correspondence_fingerprint(repo, "parent")

        _write(
            repo / "Tablet" / "helper.lean",
            "import Tablet.Preamble\n\n"
            "theorem helper : 1 = 1 := by\n"
            "  rfl\n",
        )
        _write(
            repo / "Tablet" / "helper.tex",
            "\\begin{lemma}[helper]\n$1 = 1$.\n\\end{lemma}\n"
            "\\begin{proof}\nTrivial.\n\\end{proof}\n",
        )
        _lake_build(repo, "Tablet.parent")
        after = correspondence_fingerprint(repo, "parent")

        self.assertEqual(before, after)

    def test_imported_definition_body_change_does_invalidate_correspondence(self) -> None:
        repo = Path(tempfile.mkdtemp())
        _init_minimal_lake_project(repo)
        _write(
            repo / "Tablet" / "meaning.lean",
            "import Tablet.Preamble\n\n"
            "def meaning : Nat := 1\n",
        )
        _write(
            repo / "Tablet" / "meaning.tex",
            "\\begin{definition}[meaning]\n$meaning := 1$.\n\\end{definition}\n",
        )
        _write(
            repo / "Tablet" / "main.lean",
            "import Tablet.Preamble\n"
            "import Tablet.meaning\n\n"
            "theorem main : meaning = 1 := by\n"
            "  rfl\n",
        )
        _write(
            repo / "Tablet" / "main.tex",
            "\\begin{theorem}[main]\n$meaning = 1$.\n\\end{theorem}\n"
            "\\begin{proof}\nTrivial.\n\\end{proof}\n",
        )

        _lake_build(repo, "Tablet.main")
        before = correspondence_fingerprint(repo, "main")

        _write(
            repo / "Tablet" / "meaning.lean",
            "import Tablet.Preamble\n\n"
            "def meaning : Nat := Nat.succ 0\n",
        )
        _lake_build(repo, "Tablet.main")
        after = correspondence_fingerprint(repo, "main")

        self.assertNotEqual(before, after)


class TestPrimeCorrespondenceFingerprints(unittest.TestCase):
    def test_prime_uses_bounded_batches(self) -> None:
        repo = Path("/tmp/fake-repo")
        snapshot = (("Tablet/A.lean", "h"),)
        calls = []

        def fake_run(_repo: Path, node_names: list[str]) -> dict[str, str]:
            calls.append(list(node_names))
            return {name: f"payload:{name}" for name in node_names}

        with mock.patch.object(nl_cache, "_has_lake_project", return_value=True), \
            mock.patch.object(nl_cache, "_lean_project_snapshot_key", return_value=snapshot), \
            mock.patch.object(nl_cache, "_run_lean_correspondence_payloads", side_effect=fake_run), \
            mock.patch.object(nl_cache, "_LEAN_FINGERPRINT_PRIME_BATCH_SIZE", 1), \
            mock.patch.dict(nl_cache._LEAN_CORRESPONDENCE_CACHE, {}, clear=True):
            nl_cache.prime_correspondence_fingerprints(repo, ["a", "b", "c"])

        self.assertEqual(calls, [["a"], ["b"], ["c"]])
