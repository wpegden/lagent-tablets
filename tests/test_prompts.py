"""Tests for prompt assembly."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lagent_tablets.config import (
    BranchingConfig, ChatConfig, Config, GitConfig, Policy,
    ProviderConfig, TmuxConfig, VerificationConfig, WorkflowConfig,
)
from lagent_tablets.state import SupervisorState, TabletNode, TabletState
from lagent_tablets.tablet import generate_node_lean, node_lean_path, node_tex_path
from lagent_tablets.prompts import (
    build_reviewer_prompt,
    build_verification_prompt,
    build_worker_prompt,
)


def _make_config(repo: Path) -> Config:
    return Config(
        repo_path=repo, goal_file=repo / "GOAL.md", state_dir=repo / ".agent-supervisor",
        worker=ProviderConfig(provider="claude"), reviewer=ProviderConfig(provider="claude"),
        verification=VerificationConfig(),
        tmux=TmuxConfig(session_name="t", dashboard_window_name="d", kill_windows_after_capture=True, burst_user="u"),
        workflow=WorkflowConfig(
            start_phase="proof_formalization", paper_tex_path=repo / "paper.tex",
            approved_axioms_path=repo / "ax.json", allowed_import_prefixes=["Mathlib"],
            forbidden_keyword_allowlist=[], human_input_path=repo / "h.md", input_request_path=repo / "i.md",
        ),
        chat=ChatConfig(root_dir=repo / "chats", repo_name="test", project_name="Test", public_base_url="http://x"),
        git=GitConfig(remote_url=None, remote_name="origin", branch="main", author_name="t", author_email="t@t"),
        max_cycles=0, sleep_seconds=1.0, startup_timeout_seconds=60.0, burst_timeout_seconds=600.0,
    )


def _setup_repo(repo: Path) -> None:
    """Create minimal repo structure with tablet nodes."""
    (repo / "GOAL.md").write_text("Prove the main theorem.\n")
    (repo / ".agent-supervisor" / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / ".agent-supervisor" / "scripts" / "check_node.sh").write_text("#!/bin/bash\necho ok\n")
    (repo / ".agent-supervisor" / "scripts" / "check_tablet.sh").write_text("#!/bin/bash\necho ok\n")

    tdir = repo / "Tablet"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "Preamble.lean").write_text("import Mathlib.Topology.Basic\n")
    (tdir / "Preamble.tex").write_text("\\begin{proposition}[BW]\nBounded sequences converge.\n\\end{proposition}\n")

    lean = generate_node_lean("main_thm", "theorem main_thm (x : Nat) : x = x", ["Tablet.Preamble"])
    (tdir / "main_thm.lean").write_text(lean)
    (tdir / "main_thm.tex").write_text(
        "\\begin{theorem}[Main]\nFor all $x$, $x = x$.\n\\end{theorem}\n\n"
        "\\begin{proof}\nBy reflexivity.\n\\end{proof}\n"
    )


class TestWorkerPrompt(unittest.TestCase):

    def test_includes_goal(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=1, phase="proof_formalization", active_node="main_thm")

        prompt = build_worker_prompt(config, state, tablet, Policy())
        self.assertIn("Prove the main theorem", prompt)

    def test_includes_active_node(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=1, phase="proof_formalization", active_node="main_thm")

        prompt = build_worker_prompt(config, state, tablet, Policy())
        self.assertIn("Active Node: main_thm", prompt)
        self.assertIn("theorem main_thm", prompt)

    def test_includes_check_script_path(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=1, phase="proof_formalization", active_node="main_thm")

        prompt = build_worker_prompt(config, state, tablet, Policy())
        self.assertIn("check_node.sh", prompt)

    def test_includes_reviewer_guidance(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        }, active_node="main_thm")
        state = SupervisorState(
            cycle=2, phase="proof_formalization", active_node="main_thm",
            last_review={"decision": "CONTINUE", "reason": "on track", "next_prompt": "Try using rfl tactic."},
        )

        prompt = build_worker_prompt(config, state, tablet, Policy())
        self.assertIn("Try using rfl tactic", prompt)

    def test_includes_previous_failure(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=2, phase="proof_formalization", active_node="main_thm")
        outcome = {
            "outcome": "INVALID",
            "detail": "Compilation failed for main_thm",
            "build_output": "type mismatch expected Nat got String",
        }

        prompt = build_worker_prompt(config, state, tablet, Policy(), previous_outcome=outcome)
        self.assertIn("INVALID", prompt)
        self.assertIn("type mismatch", prompt)

    def test_includes_tablet_status(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=1, phase="proof_formalization", active_node="main_thm")

        prompt = build_worker_prompt(config, state, tablet, Policy())
        self.assertIn("0/1 nodes closed", prompt)

    def test_includes_policy_notes(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=1, phase="proof_formalization", active_node="main_thm")

        from lagent_tablets.config import PromptNotesPolicy
        policy = Policy(prompt_notes=PromptNotesPolicy(worker="Use simp aggressively."))
        prompt = build_worker_prompt(config, state, tablet, policy)
        self.assertIn("Use simp aggressively", prompt)


class TestReviewerPrompt(unittest.TestCase):

    def test_includes_tablet_status(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(cycle=1, phase="proof_formalization")

        prompt = build_reviewer_prompt(config, state, tablet, Policy())
        self.assertIn("main_thm", prompt)
        self.assertIn("CONTINUE", prompt)
        self.assertIn("next_active_node", prompt)

    def test_includes_worker_handoff(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })
        state = SupervisorState(cycle=1, phase="proof_formalization")

        prompt = build_reviewer_prompt(
            config, state, tablet, Policy(),
            worker_handoff={"summary": "Tried rfl, didn't work", "status": "NOT_STUCK"},
        )
        self.assertIn("Tried rfl", prompt)
        self.assertIn("NOT_STUCK", prompt)

    def test_shows_orphan_warning(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        # Add an orphan node
        tdir = repo / "Tablet"
        orphan_lean = generate_node_lean("orphan_helper", "theorem orphan_helper : True", ["Tablet.Preamble"])
        (tdir / "orphan_helper.lean").write_text(orphan_lean)
        (tdir / "orphan_helper.tex").write_text("\\begin{lemma}\nTrue.\n\\end{lemma}\n\\begin{proof}\nTrivial.\n\\end{proof}\n")

        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
            "orphan_helper": TabletNode(name="orphan_helper", kind="helper_lemma", status="open"),
        })
        state = SupervisorState(cycle=1, phase="proof_formalization")

        prompt = build_reviewer_prompt(config, state, tablet, Policy())
        self.assertIn("Orphan", prompt)
        self.assertIn("orphan_helper", prompt)


class TestVerificationPrompt(unittest.TestCase):

    def test_includes_new_nodes(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })

        prompt = build_verification_prompt(
            config, tablet,
            new_nodes=["main_thm"],
            modified_nodes=[],
        )
        self.assertIn("main_thm", prompt)
        self.assertIn("CORRESPONDENCE", prompt.upper())
        self.assertIn("FAITHFULNESS", prompt.upper())
        self.assertIn("SOUNDNESS", prompt.upper())
        self.assertIn("APPROVE", prompt)

    def test_includes_paper(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        (repo / "paper.tex").write_text("\\section{Main Result}\nThe theorem states...\n")
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })

        prompt = build_verification_prompt(
            config, tablet,
            new_nodes=["main_thm"],
            modified_nodes=[],
            paper_tex="\\section{Main Result}\nThe theorem states...\n",
        )
        self.assertIn("SOURCE PAPER", prompt)
        self.assertIn("Main Result", prompt)


if __name__ == "__main__":
    unittest.main()
