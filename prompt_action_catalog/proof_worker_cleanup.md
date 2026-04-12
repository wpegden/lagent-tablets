# proof_worker_cleanup

- Source prompt: `prompt_catalog/proof_worker_cleanup.md`
- Situation: Cleanup-phase worker for semantics-preserving polish only.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the active node, imported nodes, reviewer guidance, and any tablet files needed for local polish.
- Read the proof-formalization worker skill file before starting.

**Apparently Available Actions**
- Perform semantics-preserving cleanup only: proof refactors, comments, formatting, or import tidying.
- Keep all node statements fixed.
- Avoid creating/deleting nodes or editing any `.tex` file.
- Return a cleanup handoff with `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.

**Prompt-Instructed Completion Steps**
- Run `check.py cleanup-preserving ...`.
- Write `worker_handoff.raw.json`.
- Run `check.py worker-handoff ... --phase proof_complete_style_cleanup`.
- Write `worker_handoff.done` if the checker passes.
