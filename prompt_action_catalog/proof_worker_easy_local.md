# proof_worker_easy_local

- Source prompt: `prompt_catalog/proof_worker_easy_local.md`
- Situation: Easy proof-formalization worker locked to one proof body.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the active node's `.lean` and `.tex`, its imported nodes, paper excerpts, and reviewer guidance.
- Read the proof-formalization worker skill file before starting.

**Apparently Available Actions**
- Edit only the proof body of the active node's `.lean` file.
- Use only the existing imports and children.
- Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT` in the handoff.
- Conclude `STUCK` if the node really needs helpers or broader structural changes.

**Prompt-Instructed Completion Steps**
- Run the deterministic self-check commands, including `check.py node <active>`.
- Write `worker_handoff.raw.json`.
- Run `check.py worker-handoff ... --phase proof_formalization`.
- Write `worker_handoff.done` if the checker passes.
