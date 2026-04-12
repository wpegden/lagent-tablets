# theorem_reviewer_target_resolved

- Source prompt: `prompt_catalog/theorem_reviewer_target_resolved.md`
- Situation: Theorem-stating reviewer after the current soundness target has already passed this cycle.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the tablet snapshot, current target status, NL verification result, and tablet files as needed.
- Read the source paper and any configured targets in view.

**Apparently Available Actions**
- Choose `CONTINUE`, `ADVANCE_PHASE`, or `NEED_INPUT`.
- Keep the same target in focus or authorize `restructure` to reopen it.
- Set `next_active_node` if advancing.
- Set `paper_focus_ranges`, `paper_provenance_assignments`, `support_resolutions`, and `open_blockers`.

**Prompt-Instructed Completion Steps**
- Write `reviewer_decision.raw.json`.
- Run `check.py reviewer-decision ... --phase theorem_stating`.
- Write `reviewer_decision.done` if the checker passes.
