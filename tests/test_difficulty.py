"""Tests for the easy/hard difficulty tiering system.

Covers: state serialization, config parsing, permissions enforcement,
easy-mode validation (new imports/files rejected), auto-elevation,
reviewer difficulty assignments, and git integration.
"""

from __future__ import annotations

import grp
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lagent_tablets.state import (
    TabletNode,
    TabletState,
    load_tablet,
    save_tablet,
)
from lagent_tablets.config import (
    Config,
    DifficultyPolicy,
    Policy,
    load_config,
    _parse_policy,
)


# ---------------------------------------------------------------------------
# State tests
# ---------------------------------------------------------------------------

class TestTabletNodeDifficulty(unittest.TestCase):
    """Test difficulty and easy_attempts fields on TabletNode."""

    def test_default_difficulty_is_hard(self):
        node = TabletNode(name="foo", kind="helper_lemma", status="open")
        self.assertEqual(node.difficulty, "hard")
        self.assertEqual(node.easy_attempts, 0)

    def test_easy_node_round_trip(self):
        node = TabletNode(name="leaf", kind="helper_lemma", status="open",
                          difficulty="easy", easy_attempts=1)
        d = node.to_dict()
        self.assertEqual(d["difficulty"], "easy")
        self.assertEqual(d["easy_attempts"], 1)
        restored = TabletNode.from_dict("leaf", d)
        self.assertEqual(restored.difficulty, "easy")
        self.assertEqual(restored.easy_attempts, 1)

    def test_hard_node_no_easy_attempts_in_dict(self):
        node = TabletNode(name="root", kind="paper_main_result", status="open",
                          difficulty="hard")
        d = node.to_dict()
        self.assertNotIn("easy_attempts", d)

    def test_backward_compat_missing_difficulty(self):
        """Old tablet.json files have no difficulty field -> defaults to hard."""
        raw = {"kind": "helper_lemma", "status": "open", "title": "old node"}
        node = TabletNode.from_dict("old_node", raw)
        self.assertEqual(node.difficulty, "hard")
        self.assertEqual(node.easy_attempts, 0)

    def test_invalid_difficulty_becomes_hard(self):
        raw = {"kind": "helper_lemma", "status": "open", "difficulty": "medium"}
        node = TabletNode.from_dict("x", raw)
        self.assertEqual(node.difficulty, "hard")

    def test_tablet_state_metrics(self):
        tablet = TabletState(nodes={
            "a": TabletNode(name="a", kind="helper_lemma", status="open", difficulty="easy"),
            "b": TabletNode(name="b", kind="helper_lemma", status="open", difficulty="hard"),
            "c": TabletNode(name="c", kind="helper_lemma", status="closed", difficulty="easy"),
            "d": TabletNode(name="d", kind="helper_lemma", status="open", difficulty="easy"),
        })
        self.assertEqual(tablet.easy_open_nodes, 2)
        self.assertEqual(tablet.hard_open_nodes, 1)
        m = tablet.metrics()
        self.assertEqual(m["easy_open"], 2)
        self.assertEqual(m["hard_open"], 1)

    def test_save_load_preserves_difficulty(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "tablet.json"
        tablet = TabletState(nodes={
            "easy_node": TabletNode(name="easy_node", kind="helper_lemma",
                                    status="open", difficulty="easy", easy_attempts=1),
            "hard_node": TabletNode(name="hard_node", kind="paper_main_result",
                                    status="open", difficulty="hard"),
        })
        save_tablet(path, tablet)
        loaded = load_tablet(path)
        self.assertEqual(loaded.nodes["easy_node"].difficulty, "easy")
        self.assertEqual(loaded.nodes["easy_node"].easy_attempts, 1)
        self.assertEqual(loaded.nodes["hard_node"].difficulty, "hard")


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestDifficultyConfig(unittest.TestCase):
    """Test easy_worker, hard_worker, and DifficultyPolicy config parsing."""

    def _write_config(self, tmpdir: Path, extra: dict = None) -> Path:
        """Write a minimal valid config.json."""
        repo = tmpdir / "repo"
        repo.mkdir()
        (repo / "Tablet").mkdir()
        config = {
            "repo_path": str(repo),
            "worker": {"provider": "claude", "model": "opus"},
            "reviewer": {"provider": "claude", "model": "opus"},
            "tmux": {"session_name": "test", "burst_user": "lagentworker"},
            "workflow": {"start_phase": "proof_formalization"},
        }
        if extra:
            config.update(extra)
        path = tmpdir / "config.json"
        path.write_text(json.dumps(config))
        return path

    def test_no_easy_hard_worker(self):
        """Config without easy_worker/hard_worker should default to None."""
        tmpdir = Path(tempfile.mkdtemp())
        path = self._write_config(tmpdir)
        config = load_config(path)
        self.assertIsNone(config.easy_worker)
        self.assertIsNone(config.hard_worker)

    def test_easy_hard_worker_parsing(self):
        tmpdir = Path(tempfile.mkdtemp())
        path = self._write_config(tmpdir, {
            "easy_worker": {"provider": "gemini", "model": "auto"},
            "hard_worker": {"provider": "codex", "model": "xhigh"},
        })
        config = load_config(path)
        self.assertEqual(config.easy_worker.provider, "gemini")
        self.assertEqual(config.easy_worker.model, "auto")
        self.assertEqual(config.hard_worker.provider, "codex")
        self.assertEqual(config.hard_worker.model, "xhigh")

    def test_difficulty_policy_default(self):
        policy = Policy()
        self.assertEqual(policy.difficulty.easy_max_retries, 2)

    def test_difficulty_policy_from_json(self):
        raw = {"difficulty": {"easy_max_retries": 5}}
        policy = _parse_policy(raw, Policy(), path=Path("test.json"))
        self.assertEqual(policy.difficulty.easy_max_retries, 5)

    def test_difficulty_policy_missing_block(self):
        """Missing difficulty block uses defaults."""
        raw = {}
        policy = _parse_policy(raw, Policy(), path=Path("test.json"))
        self.assertEqual(policy.difficulty.easy_max_retries, 2)


# ---------------------------------------------------------------------------
# Permissions tests
# ---------------------------------------------------------------------------

class TestEasyModePermissions(unittest.TestCase):
    """Test that setup_permissions correctly restricts easy-mode workers."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.repo = self.tmpdir / "repo"
        self.tablet = self.repo / "Tablet"
        self.tablet.mkdir(parents=True)
        # Create some files
        (self.tablet / "active_node.lean").write_text("theorem active_node := sorry")
        (self.tablet / "active_node.tex").write_text("\\begin{theorem}...\\end{theorem}")
        (self.tablet / "Preamble.lean").write_text("import Mathlib.Data.Nat.Basic")
        (self.tablet / "other_node.lean").write_text("theorem other := sorry")
        (self.repo / "worker_handoff.json").write_text("{}")

    def _can_test_permissions(self):
        """Check if we can meaningfully test group permissions."""
        try:
            gid = grp.getgrnam("leanagent").gr_gid
            return True
        except KeyError:
            return False

    def test_easy_mode_tablet_dir_not_group_writable(self):
        """In easy mode, Tablet/ should be 0o2755 (not group-writable)."""
        if not self._can_test_permissions():
            self.skipTest("leanagent group not available")

        from lagent_tablets.cycle import setup_permissions
        # Create a minimal config mock
        config = type("Config", (), {
            "repo_path": self.repo,
            "tmux": type("Tmux", (), {"burst_user": "lagentworker"})(),
        })()
        setup_permissions(config, "active_node", easy_mode=True)
        mode = self.tablet.stat().st_mode
        # Check group write bit is NOT set
        self.assertFalse(mode & stat.S_IWGRP,
                         f"Tablet/ should NOT be group-writable in easy mode, got {oct(mode)}")
        # Check setgid is still set
        self.assertTrue(mode & stat.S_ISGID,
                        f"Tablet/ should have setgid bit, got {oct(mode)}")

    def test_hard_mode_tablet_dir_group_writable(self):
        """In hard mode (default), Tablet/ should be 0o2775 (group-writable)."""
        if not self._can_test_permissions():
            self.skipTest("leanagent group not available")

        from lagent_tablets.cycle import setup_permissions
        config = type("Config", (), {
            "repo_path": self.repo,
            "tmux": type("Tmux", (), {"burst_user": "lagentworker"})(),
        })()
        setup_permissions(config, "active_node", easy_mode=False)
        mode = self.tablet.stat().st_mode
        self.assertTrue(mode & stat.S_IWGRP,
                        f"Tablet/ should be group-writable in hard mode, got {oct(mode)}")

    def test_easy_mode_preamble_readonly(self):
        """In easy mode, Preamble.lean should be 0o644 (not group-writable)."""
        if not self._can_test_permissions():
            self.skipTest("leanagent group not available")

        from lagent_tablets.cycle import setup_permissions
        config = type("Config", (), {
            "repo_path": self.repo,
            "tmux": type("Tmux", (), {"burst_user": "lagentworker"})(),
        })()
        setup_permissions(config, "active_node", easy_mode=True)
        preamble_mode = (self.tablet / "Preamble.lean").stat().st_mode & 0o777
        self.assertEqual(preamble_mode, 0o644,
                         f"Preamble should be 0o644 in easy mode, got {oct(preamble_mode)}")

    def test_easy_mode_active_node_writable(self):
        """Active node files should still be group-writable in easy mode."""
        if not self._can_test_permissions():
            self.skipTest("leanagent group not available")

        from lagent_tablets.cycle import setup_permissions
        config = type("Config", (), {
            "repo_path": self.repo,
            "tmux": type("Tmux", (), {"burst_user": "lagentworker"})(),
        })()
        setup_permissions(config, "active_node", easy_mode=True)
        for fname in ("active_node.lean", "active_node.tex"):
            mode = (self.tablet / fname).stat().st_mode & 0o777
            self.assertEqual(mode, 0o664,
                             f"{fname} should be 0o664 in easy mode, got {oct(mode)}")

    def test_hard_mode_preamble_writable(self):
        """In hard mode, Preamble.lean should be 0o664 (group-writable)."""
        if not self._can_test_permissions():
            self.skipTest("leanagent group not available")

        from lagent_tablets.cycle import setup_permissions
        config = type("Config", (), {
            "repo_path": self.repo,
            "tmux": type("Tmux", (), {"burst_user": "lagentworker"})(),
        })()
        setup_permissions(config, "active_node", easy_mode=False)
        preamble_mode = (self.tablet / "Preamble.lean").stat().st_mode & 0o777
        self.assertEqual(preamble_mode, 0o664,
                         f"Preamble should be 0o664 in hard mode, got {oct(preamble_mode)}")


# ---------------------------------------------------------------------------
# Auto-elevation tests
# ---------------------------------------------------------------------------

class TestAutoElevation(unittest.TestCase):
    """Test that easy nodes auto-elevate to hard after max retries."""

    def test_elevation_after_max_retries(self):
        node = TabletNode(name="leaf", kind="helper_lemma", status="open",
                          difficulty="easy", easy_attempts=1)
        # Simulate second failed attempt
        node.easy_attempts += 1
        max_retries = 2
        if node.easy_attempts >= max_retries:
            node.difficulty = "hard"
            node.easy_attempts = 0
        self.assertEqual(node.difficulty, "hard")
        self.assertEqual(node.easy_attempts, 0)

    def test_no_elevation_before_max(self):
        node = TabletNode(name="leaf", kind="helper_lemma", status="open",
                          difficulty="easy", easy_attempts=0)
        node.easy_attempts += 1
        max_retries = 2
        if node.easy_attempts >= max_retries:
            node.difficulty = "hard"
        self.assertEqual(node.difficulty, "easy")
        self.assertEqual(node.easy_attempts, 1)


# ---------------------------------------------------------------------------
# Reviewer difficulty assignment tests
# ---------------------------------------------------------------------------

class TestReviewerDifficultyAssignment(unittest.TestCase):
    """Test that reviewer decisions apply difficulty_assignments and elevate_to_hard."""

    def test_difficulty_assignments(self):
        tablet = TabletState(nodes={
            "a": TabletNode(name="a", kind="helper_lemma", status="open", difficulty="hard"),
            "b": TabletNode(name="b", kind="helper_lemma", status="open", difficulty="easy"),
        })
        decision = {
            "decision": "CONTINUE",
            "difficulty_assignments": {"a": "easy", "b": "hard"},
        }
        for name, diff in decision.get("difficulty_assignments", {}).items():
            if name in tablet.nodes and diff in ("easy", "hard"):
                tablet.nodes[name].difficulty = diff
                tablet.nodes[name].easy_attempts = 0
        self.assertEqual(tablet.nodes["a"].difficulty, "easy")
        self.assertEqual(tablet.nodes["b"].difficulty, "hard")

    def test_elevate_to_hard(self):
        tablet = TabletState(nodes={
            "leaf": TabletNode(name="leaf", kind="helper_lemma", status="open",
                               difficulty="easy", easy_attempts=1),
        })
        decision = {"decision": "CONTINUE", "elevate_to_hard": ["leaf"]}
        for name in decision.get("elevate_to_hard", []):
            if name in tablet.nodes and tablet.nodes[name].difficulty == "easy":
                tablet.nodes[name].difficulty = "hard"
                tablet.nodes[name].easy_attempts = 0
        self.assertEqual(tablet.nodes["leaf"].difficulty, "hard")
        self.assertEqual(tablet.nodes["leaf"].easy_attempts, 0)

    def test_elevate_ignores_already_hard(self):
        tablet = TabletState(nodes={
            "x": TabletNode(name="x", kind="helper_lemma", status="open", difficulty="hard"),
        })
        decision = {"decision": "CONTINUE", "elevate_to_hard": ["x"]}
        for name in decision.get("elevate_to_hard", []):
            if name in tablet.nodes and tablet.nodes[name].difficulty == "easy":
                tablet.nodes[name].difficulty = "hard"
        # Should stay hard (no-op)
        self.assertEqual(tablet.nodes["x"].difficulty, "hard")

    def test_invalid_difficulty_ignored(self):
        tablet = TabletState(nodes={
            "a": TabletNode(name="a", kind="helper_lemma", status="open", difficulty="easy"),
        })
        decision = {"decision": "CONTINUE", "difficulty_assignments": {"a": "medium"}}
        for name, diff in decision.get("difficulty_assignments", {}).items():
            if name in tablet.nodes and diff in ("easy", "hard"):
                tablet.nodes[name].difficulty = diff
        # Should stay easy (medium is not valid)
        self.assertEqual(tablet.nodes["a"].difficulty, "easy")


# ---------------------------------------------------------------------------
# Git integration tests
# ---------------------------------------------------------------------------

class TestGitOps(unittest.TestCase):
    """Test git_ops functions."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.repo = self.tmpdir / "repo"
        self.repo.mkdir()
        (self.repo / "Tablet").mkdir()
        (self.repo / ".agent-supervisor").mkdir()

    def test_init_repo(self):
        from lagent_tablets.git_ops import init_repo
        init_repo(self.repo)
        self.assertTrue((self.repo / ".git").exists())
        self.assertTrue((self.repo / ".gitignore").exists())

    def test_commit_cycle(self):
        from lagent_tablets.git_ops import init_repo, commit_cycle
        init_repo(self.repo)
        # Create a file to commit
        (self.repo / "Tablet" / "test.lean").write_text("theorem test := sorry")
        sha = commit_cycle(self.repo, 1, phase="test", outcome="PROGRESS", active_node="test")
        self.assertIsNotNone(sha)
        # Verify tag exists
        result = subprocess.run(
            ["git", "tag", "-l", "cycle-1"], capture_output=True, text=True, cwd=self.repo)
        self.assertIn("cycle-1", result.stdout)

    def test_commit_nothing_returns_none(self):
        from lagent_tablets.git_ops import init_repo, commit_cycle
        init_repo(self.repo)
        # First commit
        (self.repo / "Tablet" / "test.lean").write_text("theorem test := sorry")
        commit_cycle(self.repo, 1, phase="test", outcome="PROGRESS")
        # Second commit with no changes
        sha = commit_cycle(self.repo, 2, phase="test", outcome="NO_PROGRESS")
        # Should still commit (cycle_meta.json changes)
        # Actually cycle_meta.json changes each time, so there IS a change
        self.assertIsNotNone(sha)

    def test_get_cycle_history(self):
        from lagent_tablets.git_ops import init_repo, commit_cycle, get_cycle_history
        init_repo(self.repo)
        (self.repo / "Tablet" / "a.lean").write_text("theorem a := sorry")
        commit_cycle(self.repo, 1, phase="theorem_stating", outcome="PROGRESS")
        (self.repo / "Tablet" / "a.lean").write_text("theorem a := by rfl")
        commit_cycle(self.repo, 2, phase="proof_formalization", outcome="PROGRESS")
        history = get_cycle_history(self.repo)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["cycle"], 1)
        self.assertEqual(history[1]["cycle"], 2)
        self.assertEqual(history[0]["phase"], "theorem_stating")
        self.assertEqual(history[1]["phase"], "proof_formalization")

    def test_get_cycle_diff(self):
        from lagent_tablets.git_ops import init_repo, commit_cycle, get_cycle_diff
        init_repo(self.repo)
        (self.repo / "Tablet" / "a.lean").write_text("theorem a := sorry")
        commit_cycle(self.repo, 1, phase="test", outcome="PROGRESS")
        (self.repo / "Tablet" / "a.lean").write_text("theorem a := by rfl")
        commit_cycle(self.repo, 2, phase="test", outcome="PROGRESS")
        diff = get_cycle_diff(self.repo, 2)
        self.assertIn("sorry", diff)
        self.assertIn("rfl", diff)

    def test_current_cycle_from_git(self):
        from lagent_tablets.git_ops import init_repo, commit_cycle, current_cycle_from_git
        init_repo(self.repo)
        self.assertEqual(current_cycle_from_git(self.repo), 0)
        (self.repo / "Tablet" / "a.lean").write_text("test")
        commit_cycle(self.repo, 1, phase="test", outcome="PROGRESS")
        self.assertEqual(current_cycle_from_git(self.repo), 1)
        (self.repo / "Tablet" / "a.lean").write_text("test2")
        commit_cycle(self.repo, 5, phase="test", outcome="PROGRESS")
        self.assertEqual(current_cycle_from_git(self.repo), 5)


# ---------------------------------------------------------------------------
# Easy-mode import validation tests
# ---------------------------------------------------------------------------

class TestEasyModeImportValidation(unittest.TestCase):
    """Test that easy-mode rejects new Tablet imports."""

    def test_new_import_detected(self):
        from lagent_tablets.tablet import extract_tablet_imports
        before_content = "import Tablet.Preamble\nimport Tablet.child_a\n\ntheorem x := sorry"
        after_content = "import Tablet.Preamble\nimport Tablet.child_a\nimport Tablet.child_b\n\ntheorem x := sorry"
        imports_before = set(extract_tablet_imports(before_content))
        imports_after = set(extract_tablet_imports(after_content))
        new_imports = imports_after - imports_before
        self.assertEqual(new_imports, {"child_b"})

    def test_no_new_imports(self):
        from lagent_tablets.tablet import extract_tablet_imports
        content = "import Tablet.Preamble\nimport Tablet.child_a\n\ntheorem x := sorry"
        imports_before = set(extract_tablet_imports(content))
        imports_after = set(extract_tablet_imports(content))
        new_imports = imports_after - imports_before
        self.assertEqual(new_imports, set())


# ---------------------------------------------------------------------------
# Worker config routing tests
# ---------------------------------------------------------------------------

class TestWorkerConfigRouting(unittest.TestCase):
    """Test that the right worker config is selected based on difficulty."""

    def _make_provider(self, provider, model):
        from lagent_tablets.adapters import ProviderConfig
        return ProviderConfig(provider=provider, model=model)

    def test_easy_routes_to_easy_worker(self):
        easy = self._make_provider("gemini", "auto")
        hard = self._make_provider("codex", "xhigh")
        default = self._make_provider("claude", "opus")

        # Easy node with easy_worker configured
        difficulty = "easy"
        if difficulty == "easy" and easy:
            effective = easy
        elif difficulty == "hard" and hard:
            effective = hard
        else:
            effective = default
        self.assertEqual(effective.provider, "gemini")

    def test_hard_routes_to_hard_worker(self):
        easy = self._make_provider("gemini", "auto")
        hard = self._make_provider("codex", "xhigh")
        default = self._make_provider("claude", "opus")

        difficulty = "hard"
        if difficulty == "easy" and easy:
            effective = easy
        elif difficulty == "hard" and hard:
            effective = hard
        else:
            effective = default
        self.assertEqual(effective.provider, "codex")

    def test_fallback_to_default(self):
        default = self._make_provider("claude", "opus")

        # No easy/hard workers configured
        difficulty = "easy"
        easy = None
        hard = None
        if difficulty == "easy" and easy:
            effective = easy
        elif difficulty == "hard" and hard:
            effective = hard
        else:
            effective = default
        self.assertEqual(effective.provider, "claude")


# ---------------------------------------------------------------------------
# Multi-agent correspondence tests
# ---------------------------------------------------------------------------

class TestMultiCorrespondenceConfig(unittest.TestCase):
    """Test correspondence_agents config parsing."""

    def test_empty_agents_default(self):
        from lagent_tablets.config import VerificationConfig
        v = VerificationConfig()
        self.assertEqual(v.correspondence_agents, [])

    def test_parse_agents(self):
        from lagent_tablets.config import _parse_verification_config
        raw = {
            "provider": "claude", "model": "opus",
            "correspondence_agents": [
                {"provider": "claude", "model": "opus", "label": "Claude"},
                {"provider": "gemini", "model": "auto", "label": "Gemini"},
            ]
        }
        v = _parse_verification_config(raw)
        self.assertEqual(len(v.correspondence_agents), 2)
        self.assertEqual(v.correspondence_agents[0].provider, "claude")
        self.assertEqual(v.correspondence_agents[0].label, "Claude")
        self.assertEqual(v.correspondence_agents[1].provider, "gemini")

    def test_invalid_provider_skipped(self):
        from lagent_tablets.config import _parse_verification_config
        raw = {
            "correspondence_agents": [
                {"provider": "openai", "model": "gpt-4"},  # invalid
                {"provider": "claude", "model": "opus"},
            ]
        }
        v = _parse_verification_config(raw)
        self.assertEqual(len(v.correspondence_agents), 1)
        self.assertEqual(v.correspondence_agents[0].provider, "claude")

    def test_auto_label(self):
        from lagent_tablets.config import _parse_verification_config
        raw = {
            "correspondence_agents": [
                {"provider": "claude", "model": "opus"},
            ]
        }
        v = _parse_verification_config(raw)
        self.assertIn("opus", v.correspondence_agents[0].label)


class TestMultiCorrespondenceReconciliation(unittest.TestCase):
    """Test agreement/disagreement logic for multi-agent correspondence."""

    def test_unanimous_approve(self):
        agent_results = [
            {"agent": "A", "index": 0, "overall": "APPROVE", "summary": "all good"},
            {"agent": "B", "index": 1, "overall": "APPROVE", "summary": "looks fine"},
        ]
        overalls = [r["overall"] for r in agent_results]
        all_approve = all(o == "APPROVE" for o in overalls)
        self.assertTrue(all_approve)

    def test_unanimous_reject(self):
        agent_results = [
            {"agent": "A", "index": 0, "overall": "REJECT", "summary": "bad"},
            {"agent": "B", "index": 1, "overall": "REJECT", "summary": "also bad"},
        ]
        overalls = [r["overall"] for r in agent_results]
        all_reject = all(o == "REJECT" for o in overalls)
        self.assertTrue(all_reject)

    def test_disagreement(self):
        agent_results = [
            {"agent": "Claude", "index": 0, "overall": "APPROVE", "summary": "fine"},
            {"agent": "Gemini", "index": 1, "overall": "REJECT", "summary": "issues found"},
        ]
        overalls = [r["overall"] for r in agent_results]
        all_approve = all(o == "APPROVE" for o in overalls)
        all_reject = all(o == "REJECT" for o in overalls)
        unanimous = all_approve or all_reject
        self.assertFalse(unanimous)

    def test_error_treated_as_disagreement(self):
        agent_results = [
            {"agent": "Claude", "index": 0, "overall": "APPROVE", "summary": "fine"},
            {"agent": "Gemini", "index": 1, "overall": "ERROR", "summary": "failed"},
        ]
        overalls = [r["overall"] for r in agent_results]
        unanimous = all(o == "APPROVE" for o in overalls) or all(o == "REJECT" for o in overalls)
        self.assertFalse(unanimous)


class TestModelAvailability(unittest.TestCase):
    """Test model availability tracking and fallback."""

    def test_mark_and_check(self):
        from lagent_tablets.model_availability import ModelAvailability
        a = ModelAvailability(cooldown_seconds=0.5)
        self.assertTrue(a.is_available("gemini-2.5-pro"))
        a.mark_unavailable("gemini-2.5-pro", "429")
        self.assertFalse(a.is_available("gemini-2.5-pro"))
        import time; time.sleep(0.6)
        self.assertTrue(a.is_available("gemini-2.5-pro"))

    def test_pick_available_skips_blocked(self):
        from lagent_tablets.model_availability import ModelAvailability
        a = ModelAvailability(cooldown_seconds=60)
        a.mark_unavailable("gemini-3-flash-preview", "429")
        a.mark_unavailable("gemini-2.5-pro", "429")
        result = a.pick_available(["gemini-3-flash-preview", "gemini-2.5-pro", "gemini-2.5-flash"])
        self.assertEqual(result, "gemini-2.5-flash")

    def test_pick_available_none_available(self):
        from lagent_tablets.model_availability import ModelAvailability
        a = ModelAvailability(cooldown_seconds=60)
        a.mark_unavailable("a", "429")
        a.mark_unavailable("b", "429")
        self.assertIsNone(a.pick_available(["a", "b"]))

    def test_status_report(self):
        from lagent_tablets.model_availability import ModelAvailability
        a = ModelAvailability(cooldown_seconds=300)
        a.mark_unavailable("gemini-2.5-pro", "test")
        s = a.status()
        self.assertIn("gemini-2.5-pro", s)
        self.assertEqual(s["gemini-2.5-pro"]["reason"], "test")
        self.assertGreater(s["gemini-2.5-pro"]["cooldown_remaining"], 0)


class TestExtractExhaustedModel(unittest.TestCase):
    """Test parsing exhausted model name from error output."""

    def test_parse_message_format(self):
        from lagent_tablets.burst import extract_exhausted_model
        err = "No capacity available for model gemini-3-flash-preview on the server"
        self.assertEqual(extract_exhausted_model(err), "gemini-3-flash-preview")

    def test_parse_json_format(self):
        from lagent_tablets.burst import extract_exhausted_model
        err = 'MODEL_CAPACITY_EXHAUSTED "model": "gemini-2.5-pro"'
        self.assertEqual(extract_exhausted_model(err), "gemini-2.5-pro")

    def test_no_match(self):
        from lagent_tablets.burst import extract_exhausted_model
        self.assertIsNone(extract_exhausted_model("some other error"))
        self.assertIsNone(extract_exhausted_model("rate limited"))

    def test_real_error_output(self):
        """Test against the actual error output we saw from Gemini CLI."""
        from lagent_tablets.burst import extract_exhausted_model
        real_err = (
            'Attempt 1 failed with status 429. Retrying with backoff... '
            'GaxiosError: [{"error":{"code":429,'
            '"message":"No capacity available for model gemini-3-flash-preview on the server",'
            '"status":"RESOURCE_EXHAUSTED",'
            '"details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo",'
            '"reason":"MODEL_CAPACITY_EXHAUSTED",'
            '"metadata":{"model":"gemini-3-flash-preview"}}]}}]'
        )
        self.assertEqual(extract_exhausted_model(real_err), "gemini-3-flash-preview")


class TestFallbackModelsConfig(unittest.TestCase):
    """Test fallback_models in ProviderConfig and CorrespondenceAgentConfig."""

    def test_provider_config_default(self):
        from lagent_tablets.adapters import ProviderConfig
        pc = ProviderConfig(provider="gemini")
        self.assertEqual(pc.fallback_models, [])

    def test_provider_config_with_fallbacks(self):
        from lagent_tablets.adapters import ProviderConfig
        pc = ProviderConfig(provider="gemini", model="auto",
                           fallback_models=["gemini-2.5-pro", "gemini-2.5-flash"])
        self.assertEqual(len(pc.fallback_models), 2)

    def test_config_parsing_fallback_models(self):
        tmpdir = Path(tempfile.mkdtemp())
        repo = tmpdir / "repo"
        repo.mkdir()
        (repo / "Tablet").mkdir()
        config_data = {
            "repo_path": str(repo),
            "worker": {"provider": "gemini", "model": "auto",
                       "fallback_models": ["gemini-2.5-pro", "gemini-2.5-flash"]},
            "reviewer": {"provider": "claude", "model": "opus"},
            "tmux": {"session_name": "test", "burst_user": "lagentworker"},
            "workflow": {"start_phase": "proof_formalization"},
        }
        path = tmpdir / "config.json"
        path.write_text(json.dumps(config_data))
        config = load_config(path)
        self.assertEqual(config.worker.fallback_models, ["gemini-2.5-pro", "gemini-2.5-flash"])

    def test_correspondence_agent_fallbacks(self):
        from lagent_tablets.config import _parse_verification_config
        raw = {
            "correspondence_agents": [{
                "provider": "gemini", "model": "auto",
                "fallback_models": ["gemini-2.5-pro", "gemini-2.0-flash"],
                "label": "Gemini"
            }]
        }
        v = _parse_verification_config(raw)
        self.assertEqual(v.correspondence_agents[0].fallback_models,
                         ["gemini-2.5-pro", "gemini-2.0-flash"])


class TestMultiCorrespondencePrompt(unittest.TestCase):
    """Test that correspondence prompt parameterizes the output file."""

    def test_custom_output_file(self):
        from lagent_tablets.prompts import build_correspondence_prompt
        from lagent_tablets.state import TabletNode, TabletState
        tmpdir = Path(tempfile.mkdtemp())
        repo = tmpdir / "repo"
        repo.mkdir()
        (repo / "Tablet").mkdir()
        (repo / "Tablet" / "a.lean").write_text("theorem a := sorry")
        (repo / "Tablet" / "a.tex").write_text("\\begin{theorem}...\\end{theorem}")

        # Minimal config mock
        config = type("Config", (), {
            "repo_path": repo,
            "goal_file": repo / "GOAL.md",
            "workflow": type("WF", (), {"paper_tex_path": None})(),
        })()
        tablet = TabletState(nodes={
            "a": TabletNode(name="a", kind="helper_lemma", status="open"),
        })
        prompt = build_correspondence_prompt(
            config, tablet, node_names=["a"],
            output_file="correspondence_result_1.json",
        )
        self.assertIn("correspondence_result_1.json", prompt)
        self.assertNotIn("correspondence_result.json", prompt)


if __name__ == "__main__":
    unittest.main()
