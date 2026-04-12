from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


@dataclass(frozen=True)
class PromptActionSpec:
    source_prompt: str
    situation: str
    may_read: List[str]
    may_do: List[str]
    must_finish_by: List[str]


def _spec(
    source_prompt: str,
    situation: str,
    may_read: List[str],
    may_do: List[str],
    must_finish_by: List[str],
) -> PromptActionSpec:
    return PromptActionSpec(
        source_prompt=source_prompt,
        situation=situation,
        may_read=may_read,
        may_do=may_do,
        must_finish_by=must_finish_by,
    )


PROMPT_ACTION_SPECS: Dict[str, PromptActionSpec] = {
    "correspondence_basic": _spec(
        "prompt_catalog/correspondence_basic.md",
        "Basic correspondence / paper-faithfulness verification for one node.",
        [
            "Read the listed node's `.lean` and `.tex` files and follow its imports.",
            "Read the cited source-paper excerpt and the full paper on disk as needed.",
            "Use Loogle to check whether a project-specific definition duplicates a Mathlib concept.",
        ],
        [
            "Judge Lean/NL correspondence for the listed node.",
            "Judge paper-faithfulness for the listed node relative to the configured targets.",
            "Check structured provenance for paper-anchored nodes and for any definition node that carries provenance.",
            "Report current open failures in `correspondence.issues` and `paper_faithfulness.issues`.",
            "Summarize whether the node should be APPROVE or REJECT overall.",
        ],
        [
            "Write `correspondence_result_0.raw.json`.",
            "Run `check.py correspondence-result ...`.",
            "Write `correspondence_result_0.done` if the checker passes.",
        ],
    ),
    "correspondence_full_context_multiple_changed_nodes": _spec(
        "prompt_catalog/correspondence_full_context_multiple_changed_nodes.md",
        "Correspondence verification with multiple changed nodes, previous results, and preamble items.",
        [
            "Read the listed node files plus `Preamble.lean` and `Preamble.tex`.",
            "Read the cited source-paper excerpts and the full paper on disk as needed.",
            "Use the old-vs-new change blocks and previous-cycle findings as context while still verifying independently.",
        ],
        [
            "Judge correspondence and paper-faithfulness for each listed node.",
            "Treat listed `Preamble[...]` items as first-class correspondence targets.",
            "Decide whether previously flagged issues are genuinely fixed or only superficially changed.",
            "Report only currently open failures and give an overall APPROVE/REJECT result.",
        ],
        [
            "Write `correspondence_result_2.raw.json`.",
            "Run `check.py correspondence-result ...`.",
            "Write `correspondence_result_2.done` if the checker passes.",
        ],
    ),
    "correspondence_single_changed_node": _spec(
        "prompt_catalog/correspondence_single_changed_node.md",
        "Correspondence verification for one node with old-vs-new context and prior findings.",
        [
            "Read the listed node's `.lean` and `.tex` files and follow imports.",
            "Read the cited paper excerpt and the full paper on disk as needed.",
            "Use the old-vs-new change context and previous-cycle findings as context while still verifying independently.",
        ],
        [
            "Judge correspondence and paper-faithfulness for the listed node.",
            "Check whether the new version really fixes the prior issue or just rephrases it.",
            "Report only currently open failures and give an overall APPROVE/REJECT result.",
        ],
        [
            "Write `correspondence_result_1.raw.json`.",
            "Run `check.py correspondence-result ...`.",
            "Write `correspondence_result_1.done` if the checker passes.",
        ],
    ),
    "nl_proof_batch": _spec(
        "prompt_catalog/nl_proof_batch.md",
        "Batch NL-proof soundness verification for multiple proof-bearing nodes.",
        [
            "Read the displayed NL proof-bearing nodes and their child-node NL statements.",
            "Read additional `Tablet/{name}.tex` files if a proof cites nodes not in the prompt.",
            "Use the source paper as a rigor benchmark for proof detail.",
        ],
        [
            "Judge whether each displayed NL proof rigorously establishes its statement from child-node NL statements.",
            "Treat the task as purely mathematical; no Lean reading is required.",
            "Report a single batch PASS/FAIL soundness decision with issues and summary.",
        ],
        [
            "Write `nl_proof_result.raw.json`.",
            "Run `check.py soundness-batch-result ...`.",
            "Write `nl_proof_result.done` if the checker passes.",
        ],
    ),
    "node_soundness_leaf": _spec(
        "prompt_catalog/node_soundness_leaf.md",
        "Single-node soundness verification for a leaf helper node.",
        [
            "Read the displayed node's `.tex` content.",
            "Read any child-node `.tex` files if needed, though this leaf example has none.",
        ],
        [
            "Judge the node's NL proof as SOUND, UNSOUND, or STRUCTURAL.",
            "Treat the task as purely mathematical; Lean is irrelevant here.",
            "Explain the verdict in detail and summarize the approval result.",
        ],
        [
            "Write `nl_proof_floating_note_0.raw.json`.",
            "Run `check.py soundness-result ... --node floating_note`.",
            "Write `nl_proof_floating_note_0.done` if the checker passes.",
        ],
    ),
    "node_soundness_with_children_and_previous_issues": _spec(
        "prompt_catalog/node_soundness_with_children_and_previous_issues.md",
        "Single-node soundness verification with children, source-paper context, and prior issues.",
        [
            "Read the displayed node's `.tex` content and its child-node `.tex` statements.",
            "Read the relevant source-paper excerpt.",
            "Use the previous-cycle issue note as context while still verifying independently.",
        ],
        [
            "Judge the node's NL proof as SOUND, UNSOUND, or STRUCTURAL.",
            "Decide whether the prior soundness issue is genuinely fixed.",
            "Explain the verdict in detail and summarize the approval result.",
        ],
        [
            "Write `nl_proof_main_result_part_b_0.raw.json`.",
            "Run `check.py soundness-result ... --node main_result_part_b`.",
            "Write `nl_proof_main_result_part_b_0.done` if the checker passes.",
        ],
    ),
    "proof_reviewer_cleanup": _spec(
        "prompt_catalog/proof_reviewer_cleanup.md",
        "Cleanup-phase reviewer for semantics-preserving polish only.",
        [
            "Read the accepted tablet snapshot and the worker handoff/output.",
            "Read any tablet files as needed to judge whether the cleanup is semantics-preserving.",
        ],
        [
            "Choose `CONTINUE`, `NEED_INPUT`, or `DONE`.",
            "Decide whether further cleanup is worthwhile or whether to stop successfully.",
            "Optionally set a cleanup focus node and paper-focus ranges.",
        ],
        [
            "Write `reviewer_decision.raw.json`.",
            "Run `check.py reviewer-decision ... --phase proof_complete_style_cleanup`.",
            "Write `reviewer_decision.done` if the checker passes.",
        ],
    ),
    "proof_reviewer_standard": _spec(
        "prompt_catalog/proof_reviewer_standard.md",
        "Standard proof-formalization reviewer with verification results and an unsupported-node advisory.",
        [
            "Read the active-node context, worker handoff/output, verification results, human feedback, and recent reviews.",
            "Read tablet files as needed to arbitrate correspondence or soundness disagreements.",
            "Notice any unsupported-node advisory, but treat it as guidance rather than a separate decision object.",
        ],
        [
            "Choose `CONTINUE`, `ADVANCE_PHASE`, `STUCK`, `NEED_INPUT`, or `DONE`.",
            "Pick the next active node.",
            "Assign difficulty or elevate an easy node to hard.",
            "Set `proof_edit_mode` to `local`, `restructure`, or `coarse_restructure`; the non-local modes only take effect if the same hard node remains active.",
            "Arbitrate verification disagreements and explain the next guidance.",
        ],
        [
            "Write `reviewer_decision.raw.json`.",
            "Run `check.py reviewer-decision ... --phase proof_formalization`.",
            "Write `reviewer_decision.done` if the checker passes.",
        ],
    ),
    "proof_worker_cleanup": _spec(
        "prompt_catalog/proof_worker_cleanup.md",
        "Cleanup-phase worker for semantics-preserving polish only.",
        [
            "Read the active node, imported nodes, reviewer guidance, and any tablet files needed for local polish.",
            "Read the proof-formalization worker skill file before starting.",
        ],
        [
            "Perform semantics-preserving cleanup only: proof refactors, comments, formatting, or import tidying.",
            "Keep all node statements fixed.",
            "Avoid creating/deleting nodes or editing any `.tex` file.",
            "Return a cleanup handoff with `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.",
        ],
        [
            "Run `check.py cleanup-preserving ...`.",
            "Write `worker_handoff.raw.json`.",
            "Run `check.py worker-handoff ... --phase proof_complete_style_cleanup`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "proof_worker_easy_local": _spec(
        "prompt_catalog/proof_worker_easy_local.md",
        "Easy proof-formalization worker locked to one proof body.",
        [
            "Read the active node's `.lean` and `.tex`, its imported nodes, paper excerpts, and reviewer guidance.",
            "Read the proof-formalization worker skill file before starting.",
        ],
        [
            "Edit only the proof body of the active node's `.lean` file.",
            "Use only the existing imports and children.",
            "Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT` in the handoff.",
            "Conclude `STUCK` if the node really needs helpers or broader structural changes.",
        ],
        [
            "Run the deterministic self-check commands, including `check.py node <active>`.",
            "Write `worker_handoff.raw.json`.",
            "Run `check.py worker-handoff ... --phase proof_formalization`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "proof_worker_hard_coarse_restructure": _spec(
        "prompt_catalog/proof_worker_hard_coarse_restructure.md",
        "Hard proof-formalization worker with explicit permission to mutate the accepted coarse package.",
        [
            "Read the active node, authorized impact region, reviewer guidance, and related tablet files.",
            "Read the proof-formalization worker skill file before starting.",
        ],
        [
            "Edit the active node's `.lean` and `.tex` files.",
            "Edit existing nodes inside the authorized impact region.",
            "Change accepted coarse-node statements or `.tex` files when needed for the same target-centered restructure.",
            "Edit import lines in `Preamble.lean` as needed.",
            "Create new nodes whose resulting placement is inside the authorized region and include provenance hints when needed.",
            "Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.",
        ],
        [
            "Run the proof scope checks and `check.py node <active>`.",
            "Write `worker_handoff.raw.json`.",
            "Run `check.py worker-handoff ... --phase proof_formalization`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "proof_worker_hard_local": _spec(
        "prompt_catalog/proof_worker_hard_local.md",
        "Hard proof-formalization worker on one node with local hard-mode freedom.",
        [
            "Read the active node's `.lean` and `.tex`, its imported nodes, source-paper excerpts, and reviewer guidance.",
            "Read the proof-formalization worker skill file before starting.",
        ],
        [
            "Edit the active node's `.lean` file, including imports and proof body.",
            "Edit import lines in `Preamble.lean` as needed.",
            "Create new nodes with matching `.lean`/`.tex` files when they genuinely unblock the proof.",
            "Update the active node's `.tex` to reflect new helpers in the NL proof.",
            "Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.",
        ],
        [
            "Run the deterministic self-check commands, including `check.py node <active>`.",
            "Write `worker_handoff.raw.json` with any needed `paper_provenance_hints`.",
            "Run `check.py worker-handoff ... --phase proof_formalization`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "proof_worker_hard_restructure": _spec(
        "prompt_catalog/proof_worker_hard_restructure.md",
        "Hard proof-formalization worker with reviewer-authorized local restructure.",
        [
            "Read the active node, authorized impact region, reviewer guidance, and related tablet files.",
            "Read the proof-formalization worker skill file before starting.",
        ],
        [
            "Edit the active node's `.lean` and `.tex` files.",
            "Edit existing nodes inside the authorized impact region.",
            "Edit import lines in `Preamble.lean` as needed.",
            "Create new nodes whose resulting placement is inside the authorized impact region when they simplify the target.",
            "Adjust imports and supporting files inside the authorized impact region for the same target-centered restructure, while keeping the active declaration line fixed.",
            "Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.",
        ],
        [
            "Run the proof scope checks and `check.py node <active>`.",
            "Write `worker_handoff.raw.json` with any needed `paper_provenance_hints`.",
            "Run `check.py worker-handoff ... --phase proof_formalization`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "theorem_reviewer_invalid_with_reset_options": _spec(
        "prompt_catalog/theorem_reviewer_invalid_with_reset_options.md",
        "Theorem-stating reviewer on an invalid attempt, with optional reset to a valid checkpoint.",
        [
            "Read the current tablet snapshot, worker handoff, worker output, invalid blocker, and valid reset checkpoint list.",
            "Read the source paper and tablet files as needed to judge the failed attempt.",
        ],
        [
            "Choose `CONTINUE` or `NEED_INPUT` only.",
            "Optionally request `reset_to_checkpoint` from the listed valid checkpoints.",
            "Set `target_edit_mode`, `next_prompt`, `issues`, `paper_provenance_assignments`, `paper_focus_ranges`, `support_resolutions`, and `open_blockers`.",
            "If the worker's `CRISIS` seems real, escalate with `NEED_INPUT`.",
        ],
        [
            "Write `reviewer_decision.raw.json`.",
            "Run `check.py reviewer-decision ... --phase theorem_stating`.",
            "Write `reviewer_decision.done` if the checker passes.",
        ],
    ),
    "theorem_reviewer_target_resolved": _spec(
        "prompt_catalog/theorem_reviewer_target_resolved.md",
        "Theorem-stating reviewer after the current soundness target has already passed this cycle.",
        [
            "Read the tablet snapshot, current target status, NL verification result, and tablet files as needed.",
            "Read the source paper and any configured targets in view.",
        ],
        [
            "Choose `CONTINUE`, `ADVANCE_PHASE`, or `NEED_INPUT`.",
            "Keep the same target in focus or authorize `restructure` to reopen it.",
            "Set `next_active_node` if advancing.",
            "Set `paper_focus_ranges`, `paper_provenance_assignments`, `support_resolutions`, and `open_blockers`.",
        ],
        [
            "Write `reviewer_decision.raw.json`.",
            "Run `check.py reviewer-decision ... --phase theorem_stating`.",
            "Write `reviewer_decision.done` if the checker passes.",
        ],
    ),
    "theorem_reviewer_with_main_result_target_issues": _spec(
        "prompt_catalog/theorem_reviewer_with_main_result_target_issues.md",
        "Theorem-stating reviewer when a configured target is still missing or helper-only.",
        [
            "Read the target-coverage summary, tablet snapshot, and source paper as needed.",
        ],
        [
            "Choose `CONTINUE` or `NEED_INPUT`.",
            "Direct the worker to add or reclassify non-helper coverage for the missing target.",
            "Set `paper_focus_ranges`, `paper_provenance_assignments`, `support_resolutions`, and `open_blockers`.",
            "Decline phase advance while target-coverage issues remain.",
        ],
        [
            "Write `reviewer_decision.raw.json`.",
            "Run `check.py reviewer-decision ... --phase theorem_stating`.",
            "Write `reviewer_decision.done` if the checker passes.",
        ],
    ),
    "theorem_reviewer_with_unsupported_nodes": _spec(
        "prompt_catalog/theorem_reviewer_with_unsupported_nodes.md",
        "Theorem-stating reviewer with open blockers, a current target, and unsupported-node decisions to make.",
        [
            "Read the tablet snapshot, worker handoff/output, current target, verification results, recent reviews, and unsupported-node list.",
            "Read the source paper and tablet files as needed to arbitrate the verification results.",
        ],
        [
            "Choose `CONTINUE` or `NEED_INPUT`.",
            "Set `target_edit_mode` for the current soundness target.",
            "Resolve each unsupported node as `remove` or `keep_and_add_dependency`.",
            "Record `paper_provenance_assignments`, `paper_focus_ranges`, and `open_blockers`.",
            "Arbitrate correspondence and soundness feedback, including structural objections.",
        ],
        [
            "Write `reviewer_decision.raw.json`.",
            "Run `check.py reviewer-decision ... --phase theorem_stating`.",
            "Write `reviewer_decision.done` if the checker passes.",
        ],
    ),
    "theorem_worker_broad_initial_empty": _spec(
        "prompt_catalog/theorem_worker_broad_initial_empty.md",
        "Theorem-stating worker at cycle start with an empty tablet.",
        [
            "Read the source paper, configured targets, and runtime worker skill file.",
            "Use the repo-local scratch directory and Loogle while planning the decomposition.",
        ],
        [
            "Create proof-bearing nodes and definition nodes as `.lean`/`.tex` pairs.",
            "Choose imports and DAG edges to build the support structure for the configured targets.",
            "Add definition nodes with actual bodies and specific Mathlib imports.",
            "Assign `difficulty_hints` for new nodes.",
            "Provide `paper_provenance_hints` for new paper-anchored nodes and for any new definition that is intended to cover a configured target; otherwise that target will remain uncovered later.",
            "Optionally fully prove a node in Lean if it is immediately available and the exact node check passes.",
            "Return `NOT_STUCK`, `STUCK`, `DONE`, `NEED_INPUT`, or `CRISIS`; `CRISIS` is only actually available in broad theorem-stating with no current soundness target and no Tablet edits.",
        ],
        [
            "Run `check.py tablet ...`.",
            "Write `worker_handoff.raw.json`.",
            "Run `check.py worker-handoff ... --phase theorem_stating`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "theorem_worker_broad_with_blockers_and_retry": _spec(
        "prompt_catalog/theorem_worker_broad_with_blockers_and_retry.md",
        "Broad theorem-stating worker with open blockers, support actions, and a preserved invalid retry.",
        [
            "Read the source paper, focused paper excerpts, current tablet snapshot, reviewer guidance, open blockers, support actions, and prior invalid blocker.",
            "Continue from the preserved invalid worktree.",
            "Use the runtime worker skill file, scratch area, and Loogle as needed.",
        ],
        [
            "Repair the target-support DAG while keeping the cycle local to the deepest unresolved slice.",
            "Resolve open blockers before treating theorem-stating as complete.",
            "Remove unsupported nodes or connect them into a real support chain when the prompt tells you to do so.",
            "Create or revise nodes, imports, and NL proofs broadly because there is no current soundness target.",
            "Assign `difficulty_hints` and `paper_provenance_hints` for new nodes; any new definition intended to cover a configured target needs structured provenance for that target to count as covered later.",
            "Optionally close nodes in Lean if their deterministic node checks pass.",
            "Return `NOT_STUCK`, `STUCK`, `DONE`, `NEED_INPUT`, or `CRISIS`; `CRISIS` is only actually available in broad theorem-stating with no current soundness target and no Tablet edits.",
        ],
        [
            "Run `check.py tablet ...`.",
            "Write `worker_handoff.raw.json`.",
            "Run `check.py worker-handoff ... --phase theorem_stating`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "theorem_worker_target_repair": _spec(
        "prompt_catalog/theorem_worker_target_repair.md",
        "Theorem-stating worker locked to one target `.tex` proof in repair mode.",
        [
            "Read the current target's `.lean` and `.tex`, its imports, reviewer guidance, and the tablet snapshot.",
            "Read the source paper and runtime theorem-stating worker skill file.",
        ],
        [
            "Edit only the target node's `.tex` proof.",
            "Keep the current DAG and all node statements fixed.",
            "Return `STUCK` with a restructure request if richer dependencies or statement/import changes are needed.",
            "Return `NOT_STUCK`, `DONE`, or `NEED_INPUT` if the repair is otherwise complete.",
        ],
        [
            "Run the theorem target repair scope check and `check.py tablet ...`.",
            "Write `worker_handoff.raw.json`.",
            "Run `check.py worker-handoff ... --phase theorem_stating`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "theorem_worker_target_restructure": _spec(
        "prompt_catalog/theorem_worker_target_restructure.md",
        "Theorem-stating worker with reviewer-authorized restructure around the current target.",
        [
            "Read the current target, authorized impact region, reviewer guidance, tablet snapshot, and source paper.",
            "Read the runtime theorem-stating worker skill file.",
        ],
        [
            "Edit the target's `.lean` and `.tex` files.",
            "Edit existing prerequisites and downstream consumers inside the authorized impact region.",
            "Create new prerequisite nodes that genuinely enter the target's authorized region.",
            "Change statements/imports inside that region for the same target-centered restructure.",
            "Optionally close touched nodes in Lean if their exact deterministic checks pass.",
            "Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.",
        ],
        [
            "Run the theorem-target edit-scope check and the scoped tablet check.",
            "Write `worker_handoff.raw.json`.",
            "Run `check.py worker-handoff ... --phase theorem_stating`.",
            "Write `worker_handoff.done` if the checker passes.",
        ],
    ),
    "verification_wrapper_compat": _spec(
        "prompt_catalog/verification_wrapper_compat.md",
        "Backward-compatible combined verification wrapper for correspondence/paper-faithfulness.",
        [
            "Read the listed node `.lean` and `.tex` files and follow imports.",
            "Read the cited source-paper excerpts and the full paper on disk as needed.",
            "Use Loogle to check for duplicated Mathlib concepts if needed.",
        ],
        [
            "Judge correspondence and paper-faithfulness for the listed nodes.",
            "Check provenance for paper-anchored nodes and for any definition node with structured provenance.",
            "Report only currently open failures and give an overall APPROVE/REJECT result.",
        ],
        [
            "Write `correspondence_result.raw.json`.",
            "Run `check.py correspondence-result ...`.",
            "Write `correspondence_result.done` if the checker passes.",
        ],
    ),
}


def catalog_prompt_names() -> List[str]:
    return sorted(PROMPT_ACTION_SPECS)


def render_prompt_action_file(name: str, spec: PromptActionSpec) -> str:
    lines: List[str] = [
        f"# {name}",
        "",
        f"- Source prompt: `{spec.source_prompt}`",
        f"- Situation: {spec.situation}",
        "- Perspective: cold-reader audit of what the prompt appears to make available.",
        "- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.",
        "",
        "**May Read / Consult**",
    ]
    lines.extend([f"- {item}" for item in spec.may_read])
    lines.append("")
    lines.append("**Apparently Available Actions**")
    lines.extend([f"- {item}" for item in spec.may_do])
    lines.append("")
    lines.append("**Prompt-Instructed Completion Steps**")
    lines.extend([f"- {item}" for item in spec.must_finish_by])
    lines.append("")
    return "\n".join(lines)


def render_readme() -> str:
    names = catalog_prompt_names()
    lines = [
        "# Prompt Action Catalog",
        "",
        "This directory records, for each generated prompt in `prompt_catalog/`, the actions that the prompt appears to make available to a cold agent reading it with no extra context.",
        "",
        "These files are intentionally about *apparent affordances*, not a second-pass semantic correction. They are meant to help audit whether the prompts are steering agents toward the intended behavior.",
        "",
        "Regenerate with:",
        "",
        "```bash",
        "python3 scripts/generate_prompt_action_catalog.py",
        "```",
        "",
        "Files:",
    ]
    lines.extend([f"- [{name}.md]({name}.md)" for name in names])
    lines.append("")
    return "\n".join(lines)


def write_prompt_action_catalog(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("*.md"):
        old.unlink()
    (output_dir / "README.md").write_text(render_readme(), encoding="utf-8")
    for name in catalog_prompt_names():
        spec = PROMPT_ACTION_SPECS[name]
        (output_dir / f"{name}.md").write_text(
            render_prompt_action_file(name, spec),
            encoding="utf-8",
        )
