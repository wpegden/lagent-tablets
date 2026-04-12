# theorem_reviewer_with_unsupported_nodes

- Source prompt: `prompt_catalog/theorem_reviewer_with_unsupported_nodes.md`
- Situation: Theorem-stating reviewer with open blockers, a current target, and unsupported-node decisions to make.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the tablet snapshot, worker handoff/output, current target, verification results, recent reviews, and unsupported-node list.
- Read the source paper and tablet files as needed to arbitrate the verification results.

**Apparently Available Actions**
- Choose `CONTINUE` or `NEED_INPUT`.
- Set `target_edit_mode` for the current soundness target.
- Resolve each unsupported node as `remove` or `keep_and_add_dependency`.
- Record `paper_provenance_assignments`, `paper_focus_ranges`, and `open_blockers`.
- Arbitrate correspondence and soundness feedback, including structural objections.

**Prompt-Instructed Completion Steps**
- Write `reviewer_decision.raw.json`.
- Run `check.py reviewer-decision ... --phase theorem_stating`.
- Write `reviewer_decision.done` if the checker passes.
