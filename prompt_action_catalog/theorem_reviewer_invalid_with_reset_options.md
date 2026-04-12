# theorem_reviewer_invalid_with_reset_options

- Source prompt: `prompt_catalog/theorem_reviewer_invalid_with_reset_options.md`
- Situation: Theorem-stating reviewer on an invalid attempt, with optional reset to a valid checkpoint.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the current tablet snapshot, worker handoff, worker output, invalid blocker, and valid reset checkpoint list.
- Read the source paper and tablet files as needed to judge the failed attempt.

**Apparently Available Actions**
- Choose `CONTINUE` or `NEED_INPUT` only.
- Optionally request `reset_to_checkpoint` from the listed valid checkpoints.
- Set `target_edit_mode`, `next_prompt`, `issues`, `paper_provenance_assignments`, `paper_focus_ranges`, `support_resolutions`, and `open_blockers`.
- If the worker's `CRISIS` seems real, escalate with `NEED_INPUT`.

**Prompt-Instructed Completion Steps**
- Write `reviewer_decision.raw.json`.
- Run `check.py reviewer-decision ... --phase theorem_stating`.
- Write `reviewer_decision.done` if the checker passes.
