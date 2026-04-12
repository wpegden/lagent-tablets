# proof_reviewer_cleanup

- Source prompt: `prompt_catalog/proof_reviewer_cleanup.md`
- Situation: Cleanup-phase reviewer for semantics-preserving polish only.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the accepted tablet snapshot and the worker handoff/output.
- Read any tablet files as needed to judge whether the cleanup is semantics-preserving.

**Apparently Available Actions**
- Choose `CONTINUE`, `NEED_INPUT`, or `DONE`.
- Decide whether further cleanup is worthwhile or whether to stop successfully.
- Optionally set a cleanup focus node and paper-focus ranges.

**Prompt-Instructed Completion Steps**
- Write `reviewer_decision.raw.json`.
- Run `check.py reviewer-decision ... --phase proof_complete_style_cleanup`.
- Write `reviewer_decision.done` if the checker passes.
