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
    build_correspondence_prompt,
    build_reviewer_prompt,
    build_theorem_stating_prompt,
    build_theorem_stating_reviewer_prompt,
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
    (repo / ".agent-supervisor" / "staging").mkdir(parents=True, exist_ok=True)
    (repo / ".agent-supervisor" / "scripts" / "check.py").write_text("#!/usr/bin/env python3\nprint('ok')\n")
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
        self.assertIn(".agent-supervisor/scripts/check.py node main_thm", prompt)
        self.assertIn("worker_handoff.raw.json", prompt)
        self.assertIn("worker_handoff.done", prompt)

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

    def test_includes_targeted_paper_excerpt_from_review(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        (repo / "paper.tex").write_text(
            "LINE 1\nLINE 2\nUNIQUE_TARGET_LINE\nLINE 4\nUNRELATED_TAIL\n"
        )
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        }, active_node="main_thm")
        state = SupervisorState(
            cycle=1,
            phase="proof_formalization",
            active_node="main_thm",
            last_review={
                "paper_focus_ranges": [
                    {"start_line": 2, "end_line": 3, "reason": "focused theorem statement"}
                ]
            },
        )

        prompt = build_worker_prompt(config, state, tablet, Policy())
        self.assertIn("RELEVANT PAPER EXCERPTS", prompt)
        self.assertIn("focused theorem statement", prompt)
        self.assertIn("UNIQUE_TARGET_LINE", prompt)
        self.assertNotIn("UNRELATED_TAIL", prompt)


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

    def test_references_paper_without_inlining_full_contents(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        (repo / "paper.tex").write_text("UNIQUE_PAPER_SENTINEL_FOR_REVIEWER\n")
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })
        state = SupervisorState(cycle=1, phase="proof_formalization")

        prompt = build_reviewer_prompt(config, state, tablet, Policy())
        self.assertIn("Read the source paper directly", prompt)
        self.assertNotIn("UNIQUE_PAPER_SENTINEL_FOR_REVIEWER", prompt)

    def test_reviewer_prompt_requests_paper_focus_ranges(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })
        state = SupervisorState(cycle=1, phase="proof_formalization")

        prompt = build_reviewer_prompt(config, state, tablet, Policy())
        self.assertIn("\"paper_focus_ranges\"", prompt)

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


class TestTheoremStatingPrompts(unittest.TestCase):

    def test_worker_prompt_includes_open_rejections(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(
            cycle=2,
            phase="theorem_stating",
            last_review={"decision": "CONTINUE", "next_prompt": "Fix the statement."},
            open_rejections=[
                {
                    "node": "main_thm",
                    "phase": "correspondence",
                    "reason": "The Lean statement drops a quantifier.",
                }
            ],
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("CURRENT OPEN REJECTIONS", prompt)
        self.assertIn("Theorem-stating continues until this list is empty", prompt)
        self.assertIn("[correspondence] main_thm: The Lean statement drops a quantifier.", prompt)

    def test_worker_prompt_includes_orphan_node_actions(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "helper_a": TabletNode(name="helper_a", kind="helper_lemma", status="open", title="Helper"),
        })
        state = SupervisorState(
            cycle=2,
            phase="theorem_stating",
            last_review={
                "decision": "CONTINUE",
                "next_prompt": "Resolve the orphan candidates.",
                "orphan_resolutions": [
                    {
                        "node": "helper_a",
                        "action": "remove",
                        "reason": "No downstream node needs it.",
                        "suggested_parents": [],
                    }
                ],
            },
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("ORPHAN NODE ACTIONS", prompt)
        self.assertIn("[remove] helper_a: No downstream node needs it.", prompt)

    def test_worker_prompt_references_files_not_contents(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        (repo / "paper.tex").write_text("UNIQUE_PAPER_SENTINEL_12345\n")
        (repo / "Tablet" / "main_thm.lean").write_text("UNIQUE_LEAN_SENTINEL_67890\n")
        (repo / "Tablet" / "main_thm.tex").write_text("UNIQUE_TEX_SENTINEL_24680\n")
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(cycle=2, phase="theorem_stating")

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("Read the source paper directly", prompt)
        self.assertIn("Tablet/main_thm.lean", prompt)
        self.assertIn("Tablet/main_thm.tex", prompt)
        self.assertNotIn("UNIQUE_PAPER_SENTINEL_12345", prompt)
        self.assertNotIn("UNIQUE_LEAN_SENTINEL_67890", prompt)
        self.assertNotIn("UNIQUE_TEX_SENTINEL_24680", prompt)

    def test_worker_prompt_includes_targeted_paper_excerpt(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        (repo / "paper.tex").write_text(
            "INTRO\nDEFN\nUNIQUE_TARGET_THEOREM\nTAIL\n"
        )
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(
            cycle=2,
            phase="theorem_stating",
            last_review={
                "paper_focus_ranges": [
                    {"start_line": 2, "end_line": 3, "reason": "main theorem lines"}
                ]
            },
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("RELEVANT PAPER EXCERPTS", prompt)
        self.assertIn("main theorem lines", prompt)
        self.assertIn("UNIQUE_TARGET_THEOREM", prompt)
        self.assertNotIn("TAIL", prompt)

    def test_reviewer_prompt_requires_open_rejections(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(
            cycle=2,
            phase="theorem_stating",
            open_rejections=[
                {
                    "node": "main_thm",
                    "phase": "correspondence",
                    "reason": "The Lean statement drops a quantifier.",
                }
            ],
        )

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            state,
            tablet,
            Policy(),
            nl_verification=[{
                "check": "correspondence",
                "overall": "REJECT",
                "correspondence": {
                    "decision": "FAIL",
                    "issues": [{"node": "main_thm", "description": "Missing quantifier."}],
                },
            }],
        )
        self.assertIn("\"open_rejections\"", prompt)
        self.assertIn("\"paper_focus_ranges\"", prompt)
        self.assertIn("Do NOT advance while `open_rejections` is non-empty.", prompt)
        self.assertIn("PREVIOUS OPEN REJECTIONS", prompt)

    def test_reviewer_prompt_includes_current_orphan_candidates(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
            "orphan_helper": TabletNode(name="orphan_helper", kind="helper_lemma", status="open", title="Orphan"),
        })

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            SupervisorState(cycle=2, phase="theorem_stating"),
            tablet,
            Policy(),
            orphan_candidates=["orphan_helper"],
        )
        self.assertIn("\"orphan_resolutions\"", prompt)
        self.assertIn("CURRENT ORPHAN CANDIDATES", prompt)
        self.assertIn("orphan_helper", prompt)

    def test_reviewer_prompt_references_files_not_contents(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        (repo / "paper.tex").write_text("UNIQUE_PAPER_SENTINEL_ABCDE\n")
        (repo / "Tablet" / "main_thm.lean").write_text("UNIQUE_LEAN_SENTINEL_FGHIJ\n")
        (repo / "Tablet" / "main_thm.tex").write_text("UNIQUE_TEX_SENTINEL_KLMNO\n")
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            SupervisorState(cycle=2, phase="theorem_stating"),
            tablet,
            Policy(),
        )
        self.assertIn("Read the source paper directly", prompt)
        self.assertIn("Tablet/main_thm.lean", prompt)
        self.assertIn("Tablet/main_thm.tex", prompt)
        self.assertNotIn("UNIQUE_PAPER_SENTINEL_ABCDE", prompt)
        self.assertNotIn("UNIQUE_LEAN_SENTINEL_FGHIJ", prompt)
        self.assertNotIn("UNIQUE_TEX_SENTINEL_KLMNO", prompt)


class TestVerificationPrompt(unittest.TestCase):

    def test_correspondence_prompt_requires_only_open_issues(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })

        prompt = build_correspondence_prompt(
            config, tablet,
            node_names=["main_thm"],
        )

        self.assertIn("Put only CURRENTLY OPEN failures", prompt)
        self.assertIn("mention that in `summary`, not in `issues`", prompt)

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
        self.assertIn("paper", prompt.lower())  # references paper file path
        # Paper content is read from disk, not inlined
        self.assertIn("paper.tex", prompt)


if __name__ == "__main__":
    unittest.main()
