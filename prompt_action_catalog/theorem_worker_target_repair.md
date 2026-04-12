# theorem_worker_target_repair

- Source prompt: `prompt_catalog/theorem_worker_target_repair.md`
- Situation: Theorem-stating worker locked to one target `.tex` proof in repair mode.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the current target's `.lean` and `.tex`, its imports, reviewer guidance, and the tablet snapshot.
- Read the source paper and runtime theorem-stating worker skill file.

**Apparently Available Actions**
- Edit only the target node's `.tex` proof.
- Keep the current DAG and all node statements fixed.
- Return `STUCK` with a restructure request if richer dependencies or statement/import changes are needed.
- Return `NOT_STUCK`, `DONE`, or `NEED_INPUT` if the repair is otherwise complete.

**Prompt-Instructed Completion Steps**
- Run the theorem target repair scope check and `check.py tablet ...`.
- Write `worker_handoff.raw.json`.
- Run `check.py worker-handoff ... --phase theorem_stating`.
- Write `worker_handoff.done` if the checker passes.
