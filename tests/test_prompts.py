"""Tests for prompt assembly."""

from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path

from lagent_tablets.config import (
    BranchingConfig, ChatConfig, Config, GitConfig, Policy,
    ProviderConfig, TmuxConfig, VerificationConfig, WorkflowConfig,
    VerificationPolicy,
)
from lagent_tablets.state import SupervisorState, TabletNode, TabletState
from lagent_tablets.tablet import generate_node_lean, node_lean_path, node_tex_path
from lagent_tablets.prompts import (
    build_correspondence_prompt,
    build_node_soundness_prompt,
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
        self.assertIn("Wait for that command to finish", prompt)
        self.assertIn("Do not write the completion marker while that checker is still running", prompt)

    def test_cleanup_worker_prompt_uses_cleanup_template(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="closed"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=3, phase="proof_complete_style_cleanup", active_node="main_thm")

        prompt = build_worker_prompt(
            config,
            state,
            tablet,
            Policy(),
            cleanup_check_payload_path=Path("<cleanup-scope.json>"),
        )
        self.assertIn("proof_complete_style_cleanup phase", prompt)
        self.assertIn("cleanup-preserving", prompt)
        self.assertIn("--phase proof_complete_style_cleanup", prompt)

    def test_proof_worker_prompt_uses_phase_specific_skill_file(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=1, phase="proof_formalization", active_node="main_thm")

        prompt = build_worker_prompt(config, state, tablet, Policy())
        self.assertIn("PROOF_FORMALIZATION_WORKER.md", prompt)

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

    def test_easy_worker_prompt_locks_to_single_lean_proof(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        }, active_node="main_thm")
        state = SupervisorState(cycle=1, phase="proof_formalization", active_node="main_thm")

        prompt = build_worker_prompt(config, state, tablet, Policy(), difficulty="easy")
        self.assertIn("Work ONLY on `Tablet/main_thm.lean`", prompt)
        self.assertIn("Do NOT edit `Tablet/main_thm.tex`", prompt)
        self.assertNotIn("Update `Tablet/main_thm.tex`", prompt)

    def test_hard_local_prompt_warns_that_coarse_package_changes_need_coarse_restructure(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", coarse=True),
        }, active_node="main_thm")
        state = SupervisorState(cycle=1, phase="proof_formalization", active_node="main_thm")

        prompt = build_worker_prompt(config, state, tablet, Policy(), difficulty="hard")
        self.assertIn("accepted coarse theorem-stating package", prompt)
        self.assertIn("proof_edit_mode: \"coarse_restructure\"", prompt)

    def test_hard_restructure_worker_prompt_authorizes_region(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        helper_lean = generate_node_lean("helper", "theorem helper : True", ["Tablet.Preamble"])
        (repo / "Tablet" / "helper.lean").write_text(helper_lean)
        (repo / "Tablet" / "helper.tex").write_text(
            "\\begin{lemma}\nTrue.\n\\end{lemma}\n\\begin{proof}\nTrivial.\n\\end{proof}\n"
        )
        main_lean = (repo / "Tablet" / "main_thm.lean").read_text(encoding="utf-8") + "\nimport Tablet.helper\n"
        (repo / "Tablet" / "main_thm.lean").write_text(main_lean)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "helper": TabletNode(name="helper", kind="helper_lemma", status="open"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        }, active_node="main_thm")
        state = SupervisorState(
            cycle=1,
            phase="proof_formalization",
            active_node="main_thm",
            proof_target_edit_mode="restructure",
        )

        prompt = build_worker_prompt(config, state, tablet, Policy(), difficulty="hard")
        self.assertIn("reviewer-authorized restructure", prompt)
        self.assertIn("AUTHORIZED IMPACT REGION", prompt)
        self.assertIn("helper", prompt)
        self.assertIn("Edit other existing node files only when those nodes are inside the authorized impact region", prompt)

    def test_hard_coarse_restructure_worker_prompt_authorizes_coarse_package_mutation(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        helper_lean = generate_node_lean("helper", "theorem helper : True", ["Tablet.Preamble"])
        (repo / "Tablet" / "helper.lean").write_text(helper_lean)
        (repo / "Tablet" / "helper.tex").write_text(
            "\\begin{lemma}\nTrue.\n\\end{lemma}\n\\begin{proof}\nTrivial.\n\\end{proof}\n"
        )
        main_lean = (repo / "Tablet" / "main_thm.lean").read_text(encoding="utf-8") + "\nimport Tablet.helper\n"
        (repo / "Tablet" / "main_thm.lean").write_text(main_lean)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "helper": TabletNode(name="helper", kind="helper_lemma", status="open"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", coarse=True),
        }, active_node="main_thm")
        state = SupervisorState(
            cycle=1,
            phase="proof_formalization",
            active_node="main_thm",
            proof_target_edit_mode="coarse_restructure",
        )

        prompt = build_worker_prompt(config, state, tablet, Policy(), difficulty="hard")
        self.assertIn("coarse-restructure", prompt)
        self.assertIn("accepted coarse theorem-stating package", prompt)
        self.assertIn("coarse-wide correspondence / paper-faithfulness sweep", prompt)

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

    def test_proof_reviewer_prompt_uses_phase_specific_skill_file(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(cycle=1, phase="proof_formalization")

        prompt = build_reviewer_prompt(config, state, tablet, Policy())
        self.assertIn("PROOF_FORMALIZATION_REVIEWER.md", prompt)

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
        self.assertIn("\"proof_edit_mode\"", prompt)

    def test_cleanup_reviewer_prompt_uses_cleanup_template(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="closed"),
        })
        state = SupervisorState(cycle=5, phase="proof_complete_style_cleanup", active_node="main_thm")

        prompt = build_reviewer_prompt(config, state, tablet, Policy())
        self.assertIn("proof_complete_style_cleanup phase", prompt)
        self.assertIn("\"decision\": \"CONTINUE | NEED_INPUT | DONE\"", prompt)

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

    def test_reviewer_prompt_includes_reject_biased_soundness_split_note(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })
        state = SupervisorState(cycle=1, phase="proof_formalization")

        prompt = build_reviewer_prompt(
            config, state, tablet,
            Policy(verification=VerificationPolicy(soundness_agent_selectors=("gemini", "codex"), soundness_disagree_bias="reject")),
            nl_verification=[{
                "check": "nl_proof",
                "overall": "REJECT",
                "summary": "Failed: ['main_thm']",
                "node_verdicts": [{
                    "node": "main_thm",
                    "overall": "REJECT",
                    "panel_split": True,
                    "agent_results": [
                        {"agent": "Gemini", "overall": "APPROVE"},
                        {"agent": "Codex", "overall": "REJECT"},
                    ],
                }],
            }],
        )
        self.assertIn("1-1 panel split", prompt)
        self.assertIn("default to CONTINUE/REJECT", prompt)


class TestTheoremStatingPrompts(unittest.TestCase):

    def test_worker_prompt_includes_open_blockers(self):
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
            open_blockers=[
                {
                    "node": "main_thm",
                    "phase": "correspondence",
                    "reason": "The Lean statement drops a quantifier.",
                }
            ],
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("Fix the statement.", prompt)
        self.assertIn("CURRENT OPEN BLOCKERS", prompt)
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

    def test_theorem_stating_worker_prompt_uses_phase_specific_skill_file(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(cycle=2, phase="theorem_stating")

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("THEOREM_STATING_WORKER.md", prompt)
        self.assertIn("Wait for that command to finish", prompt)
        self.assertIn("Do not write the completion marker while that checker is still running", prompt)

    def test_theorem_stating_reviewer_prompt_restricts_invalid_attempt_decisions(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(cycle=2, phase="theorem_stating")

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            state,
            tablet,
            Policy(),
            validation_summary={"outcome": "INVALID", "detail": "synthetic failure", "consecutive_invalids": 1},
        )
        self.assertIn("allowed decisions are only `CONTINUE` or `NEED_INPUT`", prompt)
        self.assertIn("Do not use `ADVANCE_PHASE`", prompt)

    def test_target_repair_prompt_does_not_reference_skill_file(self):
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
            theorem_soundness_target="main_thm",
            theorem_target_edit_mode="repair",
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertNotIn("Read the skill file at", prompt)

    def test_worker_prompt_without_target_still_tells_worker_to_stay_local(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(cycle=2, phase="theorem_stating")

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("work in deterministic deepest-first DAG order", prompt)
        self.assertIn("broad opportunistic rewrites", prompt)

    def test_worker_prompt_uses_updated_broad_node_count_guidance(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })
        state = SupervisorState(cycle=2, phase="theorem_stating")

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("Aim for 15-50 nodes", prompt)

    def test_worker_prompt_surfaces_previous_invalid_attempt_detail(self):
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
            validation_summary={
                "last_outcome": "INVALID",
                "last_invalid_detail": "foo: synthetic failure",
                "attempt": 1,
                "consecutive_invalids": 1,
                "last_reset_to_checkpoint": "",
            },
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("--- PREVIOUS ATTEMPT ---", prompt)
        self.assertIn("foo: synthetic failure", prompt)
        self.assertIn("worktree has been preserved", prompt)

    def test_worker_prompt_with_target_requires_scope_to_stay_in_target_chain(self):
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
            theorem_soundness_target="main_thm",
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("CURRENT SOUNDNESS TARGET", prompt)
        self.assertIn("Work ONLY on `Tablet/main_thm.tex`", prompt)
        self.assertIn("request restructure", prompt)
        self.assertNotIn("If the tablet is still missing major parts, create the needed nodes", prompt)
        self.assertNotIn("For each node, create two files", prompt)

    def test_worker_prompt_with_target_restructure_mode_authorizes_broader_slice(self):
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
            theorem_soundness_target="main_thm",
            theorem_target_edit_mode="restructure",
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("Current target mode: `restructure`.", prompt)
        self.assertIn("Broader restructure is authorized", prompt)
        self.assertIn("MODE: target restructure", prompt)
        self.assertIn("WHAT YOU MAY EDIT:", prompt)
        self.assertNotIn("DECOMPOSITION STRATEGY:", prompt)
        self.assertNotIn("For each node, create two files", prompt)

    def test_worker_prompt_with_target_restructure_includes_scoped_region_check(self):
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
            theorem_soundness_target="main_thm",
            theorem_target_edit_mode="restructure",
        )

        prompt = build_theorem_stating_prompt(
            config,
            state,
            tablet,
            Policy(),
            authorized_region=["main_thm", "consumer"],
            scoped_tablet_check_payload_path=Path("/tmp/theorem_target_scope_check.json"),
        )
        self.assertIn("AUTHORIZED IMPACT REGION", prompt)
        self.assertIn("main_thm, consumer", prompt)
        self.assertIn("tablet-scoped", prompt)
        self.assertIn("--scope-json /tmp/theorem_target_scope_check.json", prompt)

    def test_worker_prompt_with_target_hides_stale_freeform_next_prompt(self):
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
            theorem_soundness_target="main_thm",
            last_review={"next_prompt": "Start with some unrelated slice first."},
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertIn("Focus this cycle on `main_thm`", prompt)
        self.assertNotIn("Start with some unrelated slice first.", prompt)

    def test_worker_prompt_without_target_hides_stale_proof_phase_guidance(self):
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
            cycle=8,
            phase="theorem_stating",
            last_review={
                "decision": "CONTINUE",
                "next_prompt": "Begin proof_formalization on `nonzero_orthogonal_count`.",
                "next_active_node": "nonzero_orthogonal_count",
                "paper_focus_ranges": [
                    {"start_line": 2, "end_line": 3, "reason": "proof target lines"}
                ],
                "open_blockers": [
                    {
                        "node": "main_thm",
                        "phase": "correspondence",
                        "reason": "The Lean statement drops a quantifier.",
                    }
                ],
            },
            open_blockers=[
                {
                    "node": "main_thm",
                    "phase": "correspondence",
                    "reason": "The Lean statement drops a quantifier.",
                }
            ],
        )

        prompt = build_theorem_stating_prompt(config, state, tablet, Policy())
        self.assertNotIn("Begin proof_formalization", prompt)
        self.assertNotIn("nonzero_orthogonal_count", prompt)
        self.assertNotIn("RELEVANT PAPER EXCERPTS", prompt)
        self.assertIn("CURRENT OPEN BLOCKERS", prompt)

    def test_reviewer_prompt_requires_open_blockers(self):
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
            open_blockers=[
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
        self.assertIn("\"open_blockers\"", prompt)
        self.assertIn("\"target_edit_mode\"", prompt)
        self.assertIn("\"paper_focus_ranges\"", prompt)
        self.assertIn("Do NOT advance while `open_blockers` is non-empty.", prompt)
        self.assertIn("PREVIOUS OPEN BLOCKERS", prompt)

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

    def test_theorem_reviewer_prompt_uses_phase_specific_skill_file(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
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
        self.assertIn("THEOREM_STATING_REVIEWER.md", prompt)

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

    def test_theorem_reviewer_prompt_includes_reject_biased_soundness_split_note(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            SupervisorState(cycle=2, phase="theorem_stating"),
            tablet,
            Policy(verification=VerificationPolicy(soundness_agent_selectors=("gemini", "codex"), soundness_disagree_bias="reject")),
            nl_verification=[{
                "check": "nl_proof",
                "overall": "REJECT",
                "summary": "Failed: ['main_thm']",
                "node_verdicts": [{
                    "node": "main_thm",
                    "overall": "REJECT",
                    "panel_split": True,
                    "agent_results": [
                        {"agent": "Gemini", "overall": "APPROVE"},
                        {"agent": "Codex", "overall": "REJECT"},
                    ],
                }],
            }],
        )
        self.assertIn("1-1 panel split", prompt)
        self.assertIn("default to CONTINUE/REJECT", prompt)

    def test_theorem_reviewer_prompt_surfaces_structural_soundness_objection(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            SupervisorState(cycle=2, phase="theorem_stating"),
            tablet,
            Policy(verification=VerificationPolicy(soundness_agent_selectors=("gemini", "codex"), soundness_disagree_bias="reject")),
            nl_verification=[{
                "check": "nl_proof",
                "overall": "REJECT",
                "summary": "Failed: ['main_thm']",
                "node_verdicts": [{
                    "node": "main_thm",
                    "overall": "REJECT",
                    "agent_results": [
                        {"agent": "Gemini", "overall": "APPROVE", "soundness": {"decision": "SOUND"}},
                        {"agent": "Codex", "overall": "REJECT", "soundness": {"decision": "STRUCTURAL"}},
                    ],
                }],
            }],
        )
        self.assertIn("[soundness structural] main_thm", prompt)
        self.assertIn("STRUCTURAL objection from Codex", prompt)

    def test_theorem_reviewer_instructions_encourage_judicious_restructure(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
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
        self.assertIn("richer DAG structure is generally good when it reflects real paper structure", prompt)
        self.assertIn("take them seriously", prompt)
        self.assertIn("You may override a `STRUCTURAL` objection", prompt)

    def test_theorem_reviewer_prompt_says_missing_prereqs_require_restructure(self):
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
            theorem_soundness_target="main_thm",
            theorem_target_edit_mode="repair",
        )

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            state,
            tablet,
            Policy(),
        )
        self.assertIn("set `target_edit_mode` to `restructure`", prompt)

    def test_theorem_reviewer_prompt_says_resolved_target_advances_automatically(self):
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
            theorem_soundness_target="main_thm",
            theorem_target_edit_mode="repair",
        )

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            state,
            tablet,
            Policy(),
            nl_verification=[{
                "check": "nl_proof",
                "overall": "APPROVE",
                "summary": "Passed: main_thm",
                "node_verdicts": [{
                    "node": "main_thm",
                    "overall": "APPROVE",
                    "agent_results": [
                        {"agent": "Gemini", "overall": "APPROVE", "soundness": {"decision": "SOUND"}},
                        {"agent": "Codex", "overall": "APPROVE", "soundness": {"decision": "SOUND"}},
                    ],
                }],
            }],
        )
        self.assertIn("This target has passed NL proof soundness in the current cycle.", prompt)
        self.assertIn("the next cycle will move automatically to the next unresolved target", prompt)
        self.assertIn("reopened", prompt)

    def test_theorem_reviewer_prompt_lists_valid_reset_checkpoints_on_invalid(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open", title="Main"),
        })

        prompt = build_theorem_stating_reviewer_prompt(
            config,
            SupervisorState(cycle=2, phase="theorem_stating"),
            tablet,
            Policy(),
            validation_summary={
                "outcome": "INVALID",
                "detail": "foo: synthetic failure",
                "consecutive_invalids": 5,
            },
            available_reset_checkpoints=[
                {"ref": "initial", "label": "initial setup commit"},
                {"ref": "cycle-4", "label": "cycle 4 | reviewer/final | theorem_stating | PROGRESS"},
            ],
        )
        self.assertIn("AVAILABLE VALID RESET CHECKPOINTS", prompt)
        self.assertIn("`initial`", prompt)
        self.assertIn("`cycle-4`", prompt)
        self.assertIn("good time to consider whether continuing from the current worktree is unproductive", prompt)

    def test_correspondence_prompt_includes_preamble_interface_items(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })

        prompt = build_correspondence_prompt(
            config,
            tablet,
            node_names=["Preamble", "main_thm"],
        )
        self.assertIn("PREAMBLE INTERFACE ITEMS TO CHECK", prompt)
        self.assertIn("Preamble[1]", prompt)
        self.assertIn("use the exact preamble item id", prompt)


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

    def test_correspondence_prompt_includes_previous_and_current_for_changed_node(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "tag", "cycle-12"], check=True, capture_output=True, text=True)

        (repo / "Tablet" / "main_thm.tex").write_text(
            "\\begin{theorem}[Main]\nFor all $x$, $x = x$ and this is explicit.\n\\end{theorem}\n\n"
            "\\begin{proof}\nBy reflexivity.\n\\end{proof}\n",
            encoding="utf-8",
        )

        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(
                name="main_thm",
                kind="paper_main_result",
                status="open",
                correspondence_status="pass",
                verification_at_cycle=12,
            ),
        })

        prompt = build_correspondence_prompt(config, tablet, node_names=["main_thm"])
        self.assertIn("CHANGE CONTEXT FOR THE NODE THAT REOPENED THIS FRONTIER", prompt)
        self.assertIn("last verified in cycle-12", prompt)
        self.assertIn("Previous NL statement block:", prompt)
        self.assertIn("Current NL statement block:", prompt)
        self.assertIn("For all $x$, $x = x$.", prompt)
        self.assertIn("For all $x$, $x = x$ and this is explicit.", prompt)


class TestNodeSoundnessPrompt(unittest.TestCase):

    def test_single_node_prompt_uses_singular_language(self):
        repo = Path(tempfile.mkdtemp())
        _setup_repo(repo)
        config = _make_config(repo)
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })

        prompt = build_node_soundness_prompt(config, tablet, node_name="main_thm")
        self.assertIn("For the node shown below, check:", prompt)
        self.assertNotIn("For each node listed below, check:", prompt)


if __name__ == "__main__":
    unittest.main()
