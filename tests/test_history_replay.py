from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from lagent_tablets.chat_history import ensure_chat_repo
from lagent_tablets.config import (
    BranchingConfig,
    ChatConfig,
    Config,
    GitConfig,
    Policy,
    ProviderConfig,
    SandboxConfig,
    TmuxConfig,
    VerificationConfig,
    WorkflowConfig,
)
from lagent_tablets.git_ops import commit_cycle, init_repo
from lagent_tablets.history_replay import _normalize_prompt, find_first_history_divergence
from lagent_tablets.prompts import build_theorem_stating_prompt
from lagent_tablets.state import SupervisorState, TabletState, save_state, save_tablet


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_config(repo: Path) -> Config:
    return Config(
        repo_path=repo,
        goal_file=repo / "GOAL.md",
        state_dir=repo / ".agent-supervisor",
        worker=ProviderConfig(provider="codex", model="gpt-test"),
        reviewer=ProviderConfig(provider="codex", model="gpt-test"),
        verification=VerificationConfig(),
        tmux=TmuxConfig(
            session_name="t",
            dashboard_window_name="d",
            kill_windows_after_capture=True,
            burst_user="lagentworker",
        ),
        sandbox=SandboxConfig(),
        workflow=WorkflowConfig(
            start_phase="theorem_stating",
            paper_tex_path=repo / "paper.tex",
            approved_axioms_path=repo / "APPROVED_AXIOMS.json",
            allowed_import_prefixes=["Mathlib"],
            forbidden_keyword_allowlist=[],
            human_input_path=repo / "HUMAN_INPUT.md",
            input_request_path=repo / "INPUT_REQUEST.md",
        ),
        chat=ChatConfig(
            root_dir=repo / ".agent-supervisor" / "chats",
            repo_name="test",
            project_name="Test",
            public_base_url="http://example.com",
        ),
        git=GitConfig(
            remote_url=None,
            remote_name="origin",
            branch="main",
            author_name="t",
            author_email="t@t",
        ),
        max_cycles=0,
        sleep_seconds=0.0,
        startup_timeout_seconds=30.0,
        burst_timeout_seconds=300.0,
        branching=BranchingConfig(),
    )


def _write_base_repo(repo: Path) -> None:
    (repo / "Tablet").mkdir(parents=True, exist_ok=True)
    (repo / ".agent-supervisor").mkdir(parents=True, exist_ok=True)
    (repo / ".agent-supervisor" / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / ".agent-supervisor" / "scripts" / "check.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "GOAL.md").write_text("Formalize the paper.\n", encoding="utf-8")
    (repo / "paper.tex").write_text("\\section{Intro}\n", encoding="utf-8")
    (repo / "APPROVED_AXIOMS.json").write_text("[]\n", encoding="utf-8")
    (repo / "HUMAN_INPUT.md").write_text("", encoding="utf-8")
    (repo / "INPUT_REQUEST.md").write_text("", encoding="utf-8")
    (repo / "Tablet" / "Preamble.lean").write_text("import Mathlib.Data.Nat.Basic\n", encoding="utf-8")
    (repo / "Tablet" / "Preamble.tex").write_text(
        "\\begin{definition}[Prelude]\nBasic imports.\n\\end{definition}\n",
        encoding="utf-8",
    )
    save_state(repo / ".agent-supervisor" / "state.json", SupervisorState(cycle=0, phase="theorem_stating"))
    save_tablet(repo / ".agent-supervisor" / "tablet.json", TabletState())
    (repo / ".agent-supervisor" / "viewer_state.json").write_text(
        json.dumps(
            {
                "state": {"cycle": 0, "phase": "theorem_stating"},
                "tablet": {"nodes": {}},
                "nodes": {},
                "meta": {"source": "startup", "in_flight_cycle": 0},
            }
        ),
        encoding="utf-8",
    )


def _initial_commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")


class TestHistoryReplay(unittest.TestCase):
    def test_normalize_prompt_canonicalizes_runtime_skill_paths(self) -> None:
        repo = Path(tempfile.mkdtemp())
        init_repo(repo)
        _write_base_repo(repo)
        config = _make_config(repo)
        replay_repo = Path(tempfile.mkdtemp()) / "replay"
        replay_repo.mkdir(parents=True, exist_ok=True)
        replay_config = _make_config(replay_repo)

        prompt = (
            f"Read the skill file at "
            f"`{repo / '.agent-supervisor' / 'runtime' / 'skills' / 'THEOREM_STATING_WORKER.md'}`.\n"
        )
        normalized = _normalize_prompt(prompt, config, replay_config)

        self.assertIn(
            str((Path(__file__).resolve().parent.parent / "skills" / "THEOREM_STATING_WORKER.md").resolve()),
            normalized,
        )
        self.assertNotIn(str(repo / ".agent-supervisor" / "runtime" / "skills"), normalized)

    def test_reports_worker_prompt_divergence(self) -> None:
        repo = Path(tempfile.mkdtemp())
        init_repo(repo)
        _write_base_repo(repo)
        _initial_commit(repo)

        chats = ensure_chat_repo(repo)
        artifact = chats / "cycle-0001" / "worker_handoff_attempt_0001"
        artifact.mkdir(parents=True)
        (artifact / "prompt.txt").write_text("historical worker prompt\n", encoding="utf-8")
        (artifact / "output.log").write_text("", encoding="utf-8")

        (repo / "CYCLE1.txt").write_text("cycle 1 final\n", encoding="utf-8")
        commit_cycle(repo, 1, phase="theorem_stating", outcome="PROGRESS", detail="done")

        result = find_first_history_divergence(_make_config(repo), Policy())

        self.assertEqual(result.status, "diverged")
        self.assertEqual(result.cycle, 1)
        self.assertEqual(result.stage, "worker")
        self.assertIn("Prompt text differs", result.reason)

    def test_reports_unreplayable_on_repeated_worker_attempts(self) -> None:
        repo = Path(tempfile.mkdtemp())
        init_repo(repo)
        _write_base_repo(repo)
        _initial_commit(repo)

        config = _make_config(repo)
        state = SupervisorState(cycle=0, phase="theorem_stating")
        tablet = TabletState()
        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())

        chats = ensure_chat_repo(repo)
        first = chats / "cycle-0001" / "worker_handoff_attempt_0001"
        first.mkdir(parents=True)
        (first / "prompt.txt").write_text(prompt, encoding="utf-8")
        (first / "output.log").write_text("", encoding="utf-8")

        second = chats / "cycle-0001" / "worker_handoff_attempt_0002"
        second.mkdir(parents=True)
        (second / "prompt.txt").write_text(prompt, encoding="utf-8")
        (second / "output.log").write_text("", encoding="utf-8")

        (repo / "CYCLE1.txt").write_text("cycle 1 final\n", encoding="utf-8")
        commit_cycle(repo, 1, phase="theorem_stating", outcome="PROGRESS", detail="done")

        result = find_first_history_divergence(config, Policy())

        self.assertEqual(result.status, "unreplayable")
        self.assertEqual(result.cycle, 1)
        self.assertEqual(result.stage, "worker")
        self.assertIn("repeated attempts", result.reason)
