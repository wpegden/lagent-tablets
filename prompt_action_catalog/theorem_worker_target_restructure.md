# theorem_worker_target_restructure

- Source prompt: `prompt_catalog/theorem_worker_target_restructure.md`
- Situation: Theorem-stating worker with reviewer-authorized restructure around the current target.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the current target, authorized impact region, reviewer guidance, tablet snapshot, and source paper.
- Read the runtime theorem-stating worker skill file.

**Apparently Available Actions**
- Edit the target's `.lean` and `.tex` files.
- Edit existing prerequisites and downstream consumers inside the authorized impact region.
- Create new prerequisite nodes that genuinely enter the target's authorized region.
- Change statements/imports inside that region for the same target-centered restructure.
- Optionally close touched nodes in Lean if their exact deterministic checks pass.
- Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.

**Prompt-Instructed Completion Steps**
- Run the theorem-target edit-scope check and the scoped tablet check.
- Write `worker_handoff.raw.json`.
- Run `check.py worker-handoff ... --phase theorem_stating`.
- Write `worker_handoff.done` if the checker passes.
