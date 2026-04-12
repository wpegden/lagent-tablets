# theorem_reviewer_with_main_result_target_issues

- Source prompt: `prompt_catalog/theorem_reviewer_with_main_result_target_issues.md`
- Situation: Theorem-stating reviewer when a configured target is still missing or helper-only.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the target-coverage summary, tablet snapshot, and source paper as needed.

**Apparently Available Actions**
- Choose `CONTINUE` or `NEED_INPUT`.
- Direct the worker to add or reclassify non-helper coverage for the missing target.
- Set `paper_focus_ranges`, `paper_provenance_assignments`, `support_resolutions`, and `open_blockers`.
- Decline phase advance while target-coverage issues remain.

**Prompt-Instructed Completion Steps**
- Write `reviewer_decision.raw.json`.
- Run `check.py reviewer-decision ... --phase theorem_stating`.
- Write `reviewer_decision.done` if the checker passes.
