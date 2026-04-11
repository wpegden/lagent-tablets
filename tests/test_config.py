"""Tests for config and policy loading."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lagent_tablets.config import (
    Config,
    ConfigError,
    ConfigManager,
    Policy,
    PolicyManager,
    SandboxConfig,
    load_config,
    policy_to_dict,
)
from lagent_tablets.project_paths import project_chats_dir, project_policy_path


class TestLoadConfig(unittest.TestCase):

    def _write_config(self, repo: Path, **overrides) -> Path:
        config = {
            "repo_path": str(repo),
            "goal_file": "GOAL.md",
            "worker": {"provider": "claude", "model": "sonnet"},
            "reviewer": {"provider": "claude", "model": "sonnet"},
            "tmux": {
                "session_name": "test",
                "dashboard_window_name": "dash",
                "kill_windows_after_capture": True,
                "burst_user": "testworker",
            },
            **overrides,
        }
        path = repo / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def _make_repo(self) -> Path:
        repo = Path(tempfile.mkdtemp())
        (repo / "GOAL.md").write_text("Prove stuff\n")
        return repo

    def test_loads_minimal_config(self):
        repo = self._make_repo()
        path = self._write_config(repo)
        config = load_config(path)
        self.assertEqual(config.repo_path, repo)
        self.assertEqual(config.worker.provider, "claude")
        self.assertEqual(config.worker.model, "sonnet")
        self.assertEqual(config.tmux.burst_user, "testworker")
        self.assertEqual(config.workflow.start_phase, "paper_check")
        self.assertEqual(config.workflow.allowed_import_prefixes, ["Mathlib"])
        self.assertEqual(config.max_cycles, 0)
        self.assertIsNotNone(config.policy_path)
        self.assertTrue(config.sandbox.enabled)
        self.assertEqual(config.sandbox.backend, "bwrap")

    def test_rejects_missing_repo(self):
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps({
            "repo_path": "/nonexistent/path",
            "worker": {"provider": "claude"},
            "reviewer": {"provider": "claude"},
            "tmux": {"session_name": "t", "dashboard_window_name": "d", "kill_windows_after_capture": True, "burst_user": "u"},
        }))
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_rejects_missing_burst_user(self):
        repo = self._make_repo()
        path = self._write_config(repo, tmux={"session_name": "t", "dashboard_window_name": "d", "kill_windows_after_capture": True})
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_rejects_invalid_provider(self):
        repo = self._make_repo()
        path = self._write_config(repo, worker={"provider": "gpt"})
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_rejects_invalid_phase(self):
        repo = self._make_repo()
        path = self._write_config(repo, workflow={"start_phase": "doesnt_exist"})
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_reads_verification_config(self):
        repo = self._make_repo()
        path = self._write_config(repo, verification={
            "provider": "claude",
            "model": "opus",
            "thinking_budget": "max",
            "max_context_tokens": 80000,
        })
        config = load_config(path)
        self.assertEqual(config.verification.model, "opus")
        self.assertEqual(config.verification.thinking_budget, "max")
        self.assertEqual(config.verification.max_context_tokens, 80000)

    def test_defaults_verification_config(self):
        repo = self._make_repo()
        path = self._write_config(repo)
        config = load_config(path)
        self.assertEqual(config.verification.provider, "claude")
        self.assertEqual(config.verification.model, "claude-opus-4-6")
        self.assertEqual(config.verification.max_context_tokens, 50000)

    def test_reads_workflow_overrides(self):
        repo = self._make_repo()
        path = self._write_config(repo, workflow={
            "start_phase": "planning",
            "allowed_import_prefixes": ["Mathlib", "MyLib"],
            "forbidden_keyword_allowlist": ["native_decide"],
        })
        config = load_config(path)
        self.assertEqual(config.workflow.start_phase, "planning")
        self.assertEqual(config.workflow.allowed_import_prefixes, ["Mathlib", "MyLib"])
        self.assertEqual(config.workflow.forbidden_keyword_allowlist, ["native_decide"])

    def test_reads_branching_overrides(self):
        repo = self._make_repo()
        path = self._write_config(repo, branching={
            "max_current_branches": 4,
            "evaluation_cycle_budget": 50,
        })
        config = load_config(path)
        self.assertEqual(config.branching.max_current_branches, 4)
        self.assertEqual(config.branching.evaluation_cycle_budget, 50)

    def test_sanitizes_tmux_session_name(self):
        repo = self._make_repo()
        path = self._write_config(repo, tmux={
            "session_name": "my.session-name!",
            "dashboard_window_name": "d",
            "kill_windows_after_capture": True,
            "burst_user": "u",
        })
        config = load_config(path)
        self.assertEqual(config.tmux.session_name, "my_session_name")

    def test_policy_path_defaults_to_sibling(self):
        repo = self._make_repo()
        path = self._write_config(repo)
        config = load_config(path)
        self.assertEqual(config.policy_path, path.with_suffix(".policy.json").resolve())

    def test_project_local_config_defaults_policy_and_chat_paths(self):
        repo = self._make_repo()
        path = repo / "lagent.config.json"
        path.write_text(json.dumps({
            "repo_path": str(repo),
            "worker": {"provider": "claude", "model": "sonnet"},
            "reviewer": {"provider": "claude", "model": "sonnet"},
            "tmux": {
                "session_name": "test",
                "dashboard_window_name": "dash",
                "kill_windows_after_capture": True,
                "burst_user": "testworker",
            },
        }), encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config.policy_path, project_policy_path(repo).resolve())
        self.assertEqual(config.chat.root_dir, project_chats_dir(repo / ".agent-supervisor").resolve())

    def test_relative_chat_root_is_resolved_under_repo(self):
        repo = self._make_repo()
        path = self._write_config(repo, chat={"root_dir": ".agent-supervisor/chats"})
        config = load_config(path)
        self.assertEqual(config.chat.root_dir, (repo / ".agent-supervisor" / "chats").resolve())

    def test_can_disable_sandbox_explicitly(self):
        repo = self._make_repo()
        path = self._write_config(repo, sandbox={"enabled": False, "backend": "bwrap"})
        config = load_config(path)
        self.assertFalse(config.sandbox.enabled)


class TestPolicyManager(unittest.TestCase):

    def test_creates_default_policy_file(self):
        tmpdir = Path(tempfile.mkdtemp())
        from lagent_tablets.config import Config, ProviderConfig, TmuxConfig, WorkflowConfig, ChatConfig, GitConfig, VerificationConfig
        config = Config(
            repo_path=tmpdir, goal_file=tmpdir / "GOAL.md", state_dir=tmpdir,
            worker=ProviderConfig(provider="claude"), reviewer=ProviderConfig(provider="claude"),
            verification=VerificationConfig(),
            tmux=TmuxConfig(session_name="t", dashboard_window_name="d", kill_windows_after_capture=True, burst_user="u"),
            sandbox=SandboxConfig(),
            workflow=WorkflowConfig(start_phase="paper_check", paper_tex_path=None, approved_axioms_path=tmpdir / "ax.json", allowed_import_prefixes=["Mathlib"], forbidden_keyword_allowlist=[], human_input_path=tmpdir / "h.md", input_request_path=tmpdir / "i.md"),
            chat=ChatConfig(root_dir=tmpdir, repo_name="test", project_name="test", public_base_url="http://x"),
            git=GitConfig(remote_url=None, remote_name="origin", branch="main", author_name="test", author_email="test@test"),
            max_cycles=0, sleep_seconds=1.0, startup_timeout_seconds=60.0, burst_timeout_seconds=600.0,
            policy_path=tmpdir / "policy.json",
        )
        pm = PolicyManager(config)
        policy = pm.current()
        self.assertEqual(policy.stuck_recovery.mainline_max_attempts, 10)
        self.assertEqual(policy.timing.sleep_seconds, 1.0)
        self.assertEqual(policy.verification.soundness_disagree_bias, "reject")
        self.assertTrue((tmpdir / "policy.json").exists())

    def test_reloads_on_change(self):
        tmpdir = Path(tempfile.mkdtemp())
        policy_path = tmpdir / "policy.json"
        policy_path.write_text(json.dumps({"stuck_recovery": {"mainline_max_attempts": 5}}))
        from lagent_tablets.config import Config, ProviderConfig, TmuxConfig, WorkflowConfig, ChatConfig, GitConfig, VerificationConfig
        config = Config(
            repo_path=tmpdir, goal_file=tmpdir / "GOAL.md", state_dir=tmpdir,
            worker=ProviderConfig(provider="claude"), reviewer=ProviderConfig(provider="claude"),
            verification=VerificationConfig(),
            tmux=TmuxConfig(session_name="t", dashboard_window_name="d", kill_windows_after_capture=True, burst_user="u"),
            sandbox=SandboxConfig(),
            workflow=WorkflowConfig(start_phase="paper_check", paper_tex_path=None, approved_axioms_path=tmpdir / "ax.json", allowed_import_prefixes=["Mathlib"], forbidden_keyword_allowlist=[], human_input_path=tmpdir / "h.md", input_request_path=tmpdir / "i.md"),
            chat=ChatConfig(root_dir=tmpdir, repo_name="test", project_name="test", public_base_url="http://x"),
            git=GitConfig(remote_url=None, remote_name="origin", branch="main", author_name="test", author_email="test@test"),
            max_cycles=0, sleep_seconds=1.0, startup_timeout_seconds=60.0, burst_timeout_seconds=600.0,
            policy_path=policy_path,
        )
        pm = PolicyManager(config)
        p1 = pm.current()
        self.assertEqual(p1.stuck_recovery.mainline_max_attempts, 5)

        # Modify and reload
        import time; time.sleep(0.01)
        policy_path.write_text(json.dumps({"stuck_recovery": {"mainline_max_attempts": 20}}))
        p2 = pm.reload(force=True)
        self.assertEqual(p2.stuck_recovery.mainline_max_attempts, 20)

    def test_keeps_last_good_on_parse_error(self):
        tmpdir = Path(tempfile.mkdtemp())
        policy_path = tmpdir / "policy.json"
        policy_path.write_text(json.dumps({"stuck_recovery": {"mainline_max_attempts": 7}}))
        from lagent_tablets.config import Config, ProviderConfig, TmuxConfig, WorkflowConfig, ChatConfig, GitConfig, VerificationConfig
        config = Config(
            repo_path=tmpdir, goal_file=tmpdir / "GOAL.md", state_dir=tmpdir,
            worker=ProviderConfig(provider="claude"), reviewer=ProviderConfig(provider="claude"),
            verification=VerificationConfig(),
            tmux=TmuxConfig(session_name="t", dashboard_window_name="d", kill_windows_after_capture=True, burst_user="u"),
            sandbox=SandboxConfig(),
            workflow=WorkflowConfig(start_phase="paper_check", paper_tex_path=None, approved_axioms_path=tmpdir / "ax.json", allowed_import_prefixes=["Mathlib"], forbidden_keyword_allowlist=[], human_input_path=tmpdir / "h.md", input_request_path=tmpdir / "i.md"),
            chat=ChatConfig(root_dir=tmpdir, repo_name="test", project_name="test", public_base_url="http://x"),
            git=GitConfig(remote_url=None, remote_name="origin", branch="main", author_name="test", author_email="test@test"),
            max_cycles=0, sleep_seconds=1.0, startup_timeout_seconds=60.0, burst_timeout_seconds=600.0,
            policy_path=policy_path,
        )
        pm = PolicyManager(config)
        p1 = pm.current()
        self.assertEqual(p1.stuck_recovery.mainline_max_attempts, 7)

        # Write invalid JSON
        policy_path.write_text("not valid json!!!")
        p2 = pm.reload(force=True)
        # Should keep old value
        self.assertEqual(p2.stuck_recovery.mainline_max_attempts, 7)

    def test_reloads_verification_agent_policy(self):
        tmpdir = Path(tempfile.mkdtemp())
        policy_path = tmpdir / "policy.json"
        policy_path.write_text(json.dumps({
            "verification": {
                "correspondence_agent_selectors": ["claude", "gemini", "codex"],
                "soundness_agent_selectors": ["gemini", "codex"],
                "soundness_disagree_bias": "reject",
            }
        }))
        from lagent_tablets.config import Config, ProviderConfig, TmuxConfig, WorkflowConfig, ChatConfig, GitConfig, VerificationConfig
        config = Config(
            repo_path=tmpdir, goal_file=tmpdir / "GOAL.md", state_dir=tmpdir,
            worker=ProviderConfig(provider="claude"), reviewer=ProviderConfig(provider="claude"),
            verification=VerificationConfig(),
            tmux=TmuxConfig(session_name="t", dashboard_window_name="d", kill_windows_after_capture=True, burst_user="u"),
            sandbox=SandboxConfig(),
            workflow=WorkflowConfig(start_phase="paper_check", paper_tex_path=None, approved_axioms_path=tmpdir / "ax.json", allowed_import_prefixes=["Mathlib"], forbidden_keyword_allowlist=[], human_input_path=tmpdir / "h.md", input_request_path=tmpdir / "i.md"),
            chat=ChatConfig(root_dir=tmpdir, repo_name="test", project_name="test", public_base_url="http://x"),
            git=GitConfig(remote_url=None, remote_name="origin", branch="main", author_name="test", author_email="test@test"),
            max_cycles=0, sleep_seconds=1.0, startup_timeout_seconds=60.0, burst_timeout_seconds=600.0,
            policy_path=policy_path,
        )
        pm = PolicyManager(config)
        policy = pm.current()
        self.assertEqual(policy.verification.correspondence_agent_selectors, ("claude", "gemini", "codex"))
        self.assertEqual(policy.verification.soundness_agent_selectors, ("gemini", "codex"))
        self.assertEqual(policy.verification.soundness_disagree_bias, "reject")


class TestConfigManager(unittest.TestCase):

    def test_detects_config_change(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "GOAL.md").write_text("goal\n")
        config_data = {
            "repo_path": str(repo),
            "worker": {"provider": "claude", "model": "sonnet"},
            "reviewer": {"provider": "claude", "model": "sonnet"},
            "tmux": {"session_name": "t", "dashboard_window_name": "d", "kill_windows_after_capture": True, "burst_user": "u"},
        }
        path = repo / "config.json"
        path.write_text(json.dumps(config_data))
        config = load_config(path)
        cm = ConfigManager(config)
        self.assertFalse(cm.check_reload())

        import time; time.sleep(0.01)
        config_data["max_cycles"] = 100
        path.write_text(json.dumps(config_data))
        self.assertTrue(cm.check_reload())
        self.assertEqual(cm.config.max_cycles, 100)


if __name__ == "__main__":
    unittest.main()
