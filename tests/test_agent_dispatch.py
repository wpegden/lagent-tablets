"""Regression tests for agent dispatch, done_file routing, and timeout handling.

These tests verify that each agent type (codex, claude, gemini) gets the
correct done_file, timeout, and prompt handling through the dispatch chain.
No actual agents are launched — we mock the backends and verify the arguments.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from lagent_tablets.adapters import BurstResult, ProviderConfig


def _fake_result(**kwargs):
    return BurstResult(ok=True, exit_code=0, captured_output="ok",
                       duration_seconds=1.0, **kwargs)


class TestReviewerBurstDoneFile(unittest.TestCase):
    """Verify done_file is passed correctly through run_reviewer_burst."""

    def _call_reviewer_burst(self, provider, done_file=None):
        config = ProviderConfig(provider=provider, model="test")
        tmpdir = Path(tempfile.mkdtemp())

        with patch("lagent_tablets.burst.run_with_retry") as mock_retry:
            mock_retry.return_value = _fake_result()
            from lagent_tablets.burst import run_reviewer_burst
            run_reviewer_burst(
                config, "test prompt",
                session_name="test", work_dir=tmpdir,
                done_file=done_file,
            )
            # Get the _run closure and call it to see what args go to the backend
            return mock_retry, tmpdir

    def test_claude_default_done_file(self):
        """Claude without explicit done_file gets reviewer_decision.json."""
        config = ProviderConfig(provider="claude", model="test")
        tmpdir = Path(tempfile.mkdtemp())

        with patch("lagent_tablets.agents.agentapi_backend.run") as mock_run:
            mock_run.return_value = _fake_result()
            from lagent_tablets.burst import run_reviewer_burst
            # Bypass retry wrapper to test the inner dispatch directly
            with patch("lagent_tablets.burst.run_with_retry", side_effect=lambda fn, **kw: fn()):
                run_reviewer_burst(config, "test", session_name="t", work_dir=tmpdir)
            args = mock_run.call_args
            self.assertEqual(args.kwargs["done_file"], tmpdir / "reviewer_decision.json")

    def test_claude_custom_done_file(self):
        """Claude with explicit done_file passes it through."""
        config = ProviderConfig(provider="claude", model="test")
        tmpdir = Path(tempfile.mkdtemp())
        custom = tmpdir / "correspondence_result_0.json"

        with patch("lagent_tablets.agents.agentapi_backend.run") as mock_run:
            mock_run.return_value = _fake_result()
            from lagent_tablets.burst import run_reviewer_burst
            with patch("lagent_tablets.burst.run_with_retry", side_effect=lambda fn, **kw: fn()):
                run_reviewer_burst(config, "test", session_name="t", work_dir=tmpdir, done_file=custom)
            args = mock_run.call_args
            self.assertEqual(args.kwargs["done_file"], custom)

    def test_gemini_custom_done_file(self):
        """Gemini with explicit done_file passes it through."""
        config = ProviderConfig(provider="gemini", model="test")
        tmpdir = Path(tempfile.mkdtemp())
        custom = tmpdir / "correspondence_result_1.json"

        with patch("lagent_tablets.agents.agentapi_backend.run") as mock_run:
            mock_run.return_value = _fake_result()
            from lagent_tablets.burst import run_reviewer_burst
            with patch("lagent_tablets.burst.run_with_retry", side_effect=lambda fn, **kw: fn()):
                run_reviewer_burst(config, "test", session_name="t", work_dir=tmpdir, done_file=custom)
            args = mock_run.call_args
            self.assertEqual(args.kwargs["done_file"], custom)

    def test_codex_does_not_use_done_file(self):
        """Codex uses codex_headless which has its own marker file system."""
        config = ProviderConfig(provider="codex", model="test")
        tmpdir = Path(tempfile.mkdtemp())

        with patch("lagent_tablets.agents.codex_headless.run") as mock_run:
            mock_run.return_value = _fake_result()
            from lagent_tablets.burst import run_reviewer_burst
            with patch("lagent_tablets.burst.run_with_retry", side_effect=lambda fn, **kw: fn()):
                run_reviewer_burst(config, "test", session_name="t", work_dir=tmpdir,
                                   done_file=tmpdir / "should_be_ignored.json")
            args = mock_run.call_args
            # codex_headless.run() should NOT receive done_file
            self.assertNotIn("done_file", args.kwargs)


class TestCodexNoHardTimeout(unittest.TestCase):
    """Verify codex_headless doesn't wrap with `timeout` command."""

    def test_no_timeout_in_script(self):
        from lagent_tablets.agents.codex_headless import build_script
        config = ProviderConfig(provider="codex", model="gpt-5.4")
        tmpdir = Path(tempfile.mkdtemp())
        script = build_script(
            config,
            prompt_file=tmpdir / "prompt.txt",
            start_file=tmpdir / "start",
            exit_file=tmpdir / "exit",
            work_dir=tmpdir,
        )
        content = script.read_text()
        self.assertNotIn("timeout --signal", content,
                         "codex script should NOT have hard timeout wrapper")
        self.assertIn('"${real_cmd[@]}"', content,
                      "codex script should run command directly")


class TestCorrespondenceAgentDoneFiles(unittest.TestCase):
    """Verify each correspondence/soundness agent gets the correct done_file."""

    def test_correspondence_agent_done_file(self):
        """_run_single_correspondence_agent passes result_file as done_file."""
        from lagent_tablets.cycle import _run_single_correspondence_agent
        from lagent_tablets.config import CorrespondenceAgentConfig, load_config
        from lagent_tablets.state import TabletNode, TabletState

        tmpdir = Path(tempfile.mkdtemp())
        repo = tmpdir / "repo"
        repo.mkdir()
        (repo / "Tablet").mkdir()
        (repo / "Tablet" / "test_node.lean").write_text("theorem test := sorry")
        (repo / "Tablet" / "test_node.tex").write_text("\\begin{theorem}...\\end{theorem}")
        (repo / ".agent-supervisor" / "logs").mkdir(parents=True)

        tablet = TabletState(nodes={
            "test_node": TabletNode(name="test_node", kind="helper_lemma", status="open"),
        })

        config_mock = MagicMock()
        config_mock.repo_path = repo
        config_mock.goal_file = repo / "GOAL.md"
        config_mock.workflow.paper_tex_path = None
        config_mock.tmux.session_name = "test"
        config_mock.tmux.burst_user = "testuser"

        agent = CorrespondenceAgentConfig(provider="claude", model="test", label="Test")

        with patch("lagent_tablets.cycle.run_reviewer_burst") as mock_burst:
            mock_burst.return_value = _fake_result()
            _run_single_correspondence_agent(
                config_mock, tablet, ["test_node"], agent,
                paper_tex="", human_input="", log_dir=repo / ".agent-supervisor" / "logs",
                agent_index=0,
            )
            args = mock_burst.call_args
            done_file = args.kwargs.get("done_file")
            self.assertIsNotNone(done_file, "done_file must be passed")
            self.assertEqual(done_file.name, "correspondence_result_0.json")

    def test_soundness_agent_done_file(self):
        """_run_single_node_soundness passes result_file as done_file."""
        from lagent_tablets.cycle import _run_single_node_soundness
        from lagent_tablets.config import CorrespondenceAgentConfig
        from lagent_tablets.state import TabletNode, TabletState

        tmpdir = Path(tempfile.mkdtemp())
        repo = tmpdir / "repo"
        repo.mkdir()
        (repo / "Tablet").mkdir()
        (repo / "Tablet" / "test_node.lean").write_text("theorem test := sorry")
        (repo / "Tablet" / "test_node.tex").write_text("\\begin{theorem}...\\end{theorem}")
        (repo / ".agent-supervisor" / "logs").mkdir(parents=True)

        tablet = TabletState(nodes={
            "test_node": TabletNode(name="test_node", kind="helper_lemma", status="open"),
        })

        config_mock = MagicMock()
        config_mock.repo_path = repo
        config_mock.goal_file = repo / "GOAL.md"
        config_mock.workflow.paper_tex_path = None
        config_mock.tmux.session_name = "test"
        config_mock.tmux.burst_user = "testuser"

        agent = CorrespondenceAgentConfig(provider="codex", model="test", label="Test")

        with patch("lagent_tablets.cycle.run_reviewer_burst") as mock_burst:
            mock_burst.return_value = _fake_result()
            _run_single_node_soundness(
                config_mock, tablet, "test_node", agent,
                paper_tex="", human_input="", log_dir=repo / ".agent-supervisor" / "logs",
                agent_index=0, node_index=0,
            )
            args = mock_burst.call_args
            done_file = args.kwargs.get("done_file")
            self.assertIsNotNone(done_file, "done_file must be passed")
            self.assertEqual(done_file.name, "nl_proof_test_node_0.json")


class TestPromptNoInlineContent(unittest.TestCase):
    """Verify correspondence prompt doesn't inline node content."""

    def test_prompt_references_files_not_content(self):
        from lagent_tablets.prompts import build_correspondence_prompt
        from lagent_tablets.state import TabletNode, TabletState

        tmpdir = Path(tempfile.mkdtemp())
        repo = tmpdir / "repo"
        repo.mkdir()
        (repo / "Tablet").mkdir()
        (repo / "Tablet" / "node_a.lean").write_text("import Tablet.Preamble\ntheorem node_a : True := sorry")
        (repo / "Tablet" / "node_a.tex").write_text("UNIQUE_TEX_CONTENT_12345")

        config_mock = MagicMock()
        config_mock.repo_path = repo
        config_mock.goal_file = repo / "GOAL.md"
        config_mock.workflow.paper_tex_path = repo / "paper.tex"

        tablet = TabletState(nodes={
            "node_a": TabletNode(name="node_a", kind="helper_lemma", status="open"),
        })

        prompt = build_correspondence_prompt(config_mock, tablet, node_names=["node_a"])

        # Prompt should NOT contain inline .tex content
        self.assertNotIn("UNIQUE_TEX_CONTENT_12345", prompt,
                         "Prompt should reference files, not inline content")
        # Prompt should tell agent to read files
        self.assertIn("read", prompt.lower())
        self.assertIn("Tablet/{name}.lean", prompt)


class TestWorkerBurstDoneFile(unittest.TestCase):
    """Worker burst always uses worker_handoff.json."""

    def test_worker_done_file(self):
        config = ProviderConfig(provider="claude", model="test")
        tmpdir = Path(tempfile.mkdtemp())

        with patch("lagent_tablets.agents.agentapi_backend.run") as mock_run:
            mock_run.return_value = _fake_result()
            from lagent_tablets.burst import run_worker_burst
            with patch("lagent_tablets.burst.run_with_retry", side_effect=lambda fn, **kw: fn()):
                run_worker_burst(config, "test", session_name="t", work_dir=tmpdir)
            args = mock_run.call_args
            self.assertEqual(args.kwargs["done_file"], tmpdir / "worker_handoff.json")


if __name__ == "__main__":
    unittest.main()
