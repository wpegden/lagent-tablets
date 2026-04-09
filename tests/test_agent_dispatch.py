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
        self.assertIn('"${cmd[@]}" < "$PROMPT_FILE"', content,
                      "codex script should pass the prompt on stdin")
        self.assertIn('setsid "${cmd[@]}"', content,
                      "codex script should launch the agent in a dedicated session")
        self.assertIn('kill -- -"$AGENT_PID"', content,
                      "codex cleanup should kill the whole process group")
        self.assertNotIn("PROMPT_CONTENT=$(cat \"$PROMPT_FILE\")", content,
                         "codex script should not materialize the prompt into argv")

    def test_script_headless_codex_uses_stdin(self):
        from lagent_tablets.agents.script_headless import build_script
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
        self.assertIn('"${cmd[@]}" < "$PROMPT_FILE"', content,
                      "fallback codex script should pass the prompt on stdin")
        self.assertIn('setsid "${cmd[@]}"', content,
                      "fallback codex script should launch the agent in a dedicated session")
        self.assertIn('kill -- -"$AGENT_PID"', content,
                      "fallback codex cleanup should kill the whole process group")
        self.assertNotIn("PROMPT_CONTENT=$(cat \"$PROMPT_FILE\")", content,
                         "fallback codex script should not materialize the prompt into argv")

    def test_script_headless_noncodex_uses_watchdog_and_process_group_cleanup(self):
        from lagent_tablets.agents.script_headless import build_script
        config = ProviderConfig(provider="claude", model="claude-opus-4-6")
        tmpdir = Path(tempfile.mkdtemp())
        script = build_script(
            config,
            prompt_file=tmpdir / "prompt.txt",
            start_file=tmpdir / "start",
            exit_file=tmpdir / "exit",
            work_dir=tmpdir,
            agent_timeout_seconds=123,
        )
        content = script.read_text()
        self.assertNotIn("timeout --signal", content,
                         "script headless should manage timeout via watchdog, not timeout(1)")
        self.assertIn("AGENT_TIMEOUT_SECONDS=123", content)
        self.assertIn('WATCHDOG_PID=$!', content)
        self.assertIn('kill -- -"$AGENT_PID"', content)
        self.assertIn('setsid "${real_cmd[@]}"', content)


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
        config_mock.state_dir = repo / ".agent-supervisor"
        config_mock.goal_file = repo / "GOAL.md"
        config_mock.workflow.paper_tex_path = None
        config_mock.tmux.session_name = "test"
        config_mock.tmux.burst_user = "testuser"

        agent = CorrespondenceAgentConfig(provider="claude", model="test", label="Test")

        with patch("lagent_tablets.cycle.run_reviewer_burst") as mock_burst:
            mock_burst.return_value = _fake_result()
            with patch("lagent_tablets.cycle._accept_validated_artifact", return_value=(None, "missing")):
                _run_single_correspondence_agent(
                    config_mock, tablet, ["test_node"], agent,
                    paper_tex="", human_input="", log_dir=repo / ".agent-supervisor" / "logs",
                    agent_index=0,
                )
                args = mock_burst.call_args
                done_file = args.kwargs.get("done_file")
                self.assertIsNotNone(done_file, "done_file must be passed")
                self.assertEqual(done_file.name, "correspondence_result_0.done")

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
        config_mock.state_dir = repo / ".agent-supervisor"
        config_mock.goal_file = repo / "GOAL.md"
        config_mock.workflow.paper_tex_path = None
        config_mock.tmux.session_name = "test"
        config_mock.tmux.burst_user = "testuser"

        agent = CorrespondenceAgentConfig(provider="codex", model="test", label="Test")

        with patch("lagent_tablets.cycle.run_reviewer_burst") as mock_burst:
            mock_burst.return_value = _fake_result()
            with patch("lagent_tablets.cycle._accept_validated_artifact", return_value=(None, "missing")):
                _run_single_node_soundness(
                    config_mock, tablet, "test_node", agent,
                    paper_tex="", human_input="", log_dir=repo / ".agent-supervisor" / "logs",
                    agent_index=0, node_index=0,
                )
                args = mock_burst.call_args
                done_file = args.kwargs.get("done_file")
                self.assertIsNotNone(done_file, "done_file must be passed")
                self.assertEqual(done_file.name, "nl_proof_test_node_0.done")


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
        config_mock.state_dir = repo / ".agent-supervisor"
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


class TestEffortPassthrough(unittest.TestCase):
    """Verify effort/reasoning_effort is passed through to each backend."""

    def test_codex_effort_in_script(self):
        """Codex script includes reasoning_effort when effort is set."""
        from lagent_tablets.agents.codex_headless import build_script
        config = ProviderConfig(provider="codex", model="gpt-5.4", effort="xhigh")
        tmpdir = Path(tempfile.mkdtemp())
        script = build_script(config, prompt_file=tmpdir / "p.txt",
                              start_file=tmpdir / "s", exit_file=tmpdir / "e",
                              work_dir=tmpdir)
        content = script.read_text()
        self.assertIn("reasoning_effort", content,
                      "codex script must include reasoning_effort when effort is set")
        self.assertIn("xhigh", content)

    def test_codex_no_effort_when_none(self):
        """Codex script omits reasoning_effort when effort is None."""
        from lagent_tablets.agents.codex_headless import build_script
        config = ProviderConfig(provider="codex", model="gpt-5.4")
        tmpdir = Path(tempfile.mkdtemp())
        script = build_script(config, prompt_file=tmpdir / "p.txt",
                              start_file=tmpdir / "s", exit_file=tmpdir / "e",
                              work_dir=tmpdir)
        content = script.read_text()
        self.assertNotIn("reasoning_effort", content)

    def test_claude_effort_in_command(self):
        """Claude command includes --effort when set."""
        from lagent_tablets.agents.agentapi_backend import _agent_command
        config = ProviderConfig(provider="claude", model="opus", effort="max")
        cmd = _agent_command(config)
        self.assertIn("--effort", cmd)
        idx = cmd.index("--effort")
        self.assertEqual(cmd[idx + 1], "max")

    def test_claude_no_effort_when_none(self):
        """Claude command omits --effort when not set."""
        from lagent_tablets.agents.agentapi_backend import _agent_command
        config = ProviderConfig(provider="claude", model="opus")
        cmd = _agent_command(config)
        self.assertNotIn("--effort", cmd)

    def test_effort_from_config_to_provider(self):
        """CorrespondenceAgentConfig.effort flows to ProviderConfig."""
        from lagent_tablets.config import CorrespondenceAgentConfig
        agent = CorrespondenceAgentConfig(provider="codex", model="gpt-5.4", effort="xhigh")
        provider = ProviderConfig(
            provider=agent.provider, model=agent.model,
            effort=getattr(agent, 'effort', None),
            extra_args=agent.extra_args,
        )
        self.assertEqual(provider.effort, "xhigh")

    def test_effort_parsed_from_json(self):
        """Config parser reads effort from correspondence_agents JSON."""
        from lagent_tablets.config import _parse_verification_config
        raw = {
            "correspondence_agents": [
                {"provider": "codex", "model": "gpt-5.4", "effort": "xhigh"},
                {"provider": "claude", "model": "opus", "effort": "max"},
                {"provider": "gemini", "model": "auto"},
            ],
            "soundness_agents": [
                {"provider": "codex", "model": "gpt-5.4", "effort": "xhigh"},
            ],
        }
        v = _parse_verification_config(raw)
        self.assertEqual(v.correspondence_agents[0].effort, "xhigh")
        self.assertEqual(v.correspondence_agents[1].effort, "max")
        self.assertIsNone(v.correspondence_agents[2].effort)
        self.assertEqual(v.soundness_agents[0].effort, "xhigh")


class TestFullConfigLoad(unittest.TestCase):
    """End-to-end test: load real config and verify all agents are configured correctly."""

    def test_extremal_vectors_config(self):
        from lagent_tablets.config import load_config
        config = load_config(Path("configs/extremal_vectors_run.json"))

        # Worker
        self.assertEqual(config.worker.provider, "codex")
        self.assertEqual(config.worker.effort, "xhigh")

        # Easy/hard workers
        self.assertIsNotNone(config.easy_worker)
        self.assertEqual(config.easy_worker.provider, "gemini")
        self.assertIsNotNone(config.hard_worker)
        self.assertEqual(config.hard_worker.provider, "codex")
        self.assertEqual(config.hard_worker.effort, "xhigh")

        # Reviewer
        self.assertEqual(config.reviewer.provider, "codex")
        self.assertEqual(config.reviewer.effort, "xhigh")

        # Correspondence agents
        corr = config.verification.correspondence_agents
        self.assertEqual(len(corr), 3)
        claude_agent = [a for a in corr if a.provider == "claude"][0]
        codex_agent = [a for a in corr if a.provider == "codex"][0]
        gemini_agent = [a for a in corr if a.provider == "gemini"][0]
        self.assertEqual(claude_agent.effort, "max")
        self.assertEqual(codex_agent.effort, "xhigh")
        self.assertIsNone(gemini_agent.effort)
        self.assertTrue(len(gemini_agent.fallback_models) > 0)

        # Soundness agents
        sound = config.verification.soundness_agents
        self.assertEqual(len(sound), 3)
        codex_sound = [a for a in sound if a.provider == "codex"][0]
        self.assertEqual(codex_sound.effort, "xhigh")


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

    def test_worker_clears_stale_handoff_file(self):
        config = ProviderConfig(provider="claude", model="test")
        tmpdir = Path(tempfile.mkdtemp())
        stale = tmpdir / "worker_handoff.json"
        stale.write_text('{"summary":"stale"}', encoding="utf-8")

        def _check_stale_removed(*args, **kwargs):
            self.assertFalse(stale.exists(), "stale worker_handoff.json must be removed before launch")
            return _fake_result()

        with patch("lagent_tablets.agents.agentapi_backend.run", side_effect=_check_stale_removed) as mock_run:
            from lagent_tablets.burst import run_worker_burst
            with patch("lagent_tablets.burst.run_with_retry", side_effect=lambda fn, **kw: fn()):
                run_worker_burst(config, "test", session_name="t", work_dir=tmpdir)
            self.assertTrue(mock_run.called)


if __name__ == "__main__":
    unittest.main()
