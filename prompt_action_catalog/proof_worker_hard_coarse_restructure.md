# proof_worker_hard_coarse_restructure

- Source prompt: `prompt_catalog/proof_worker_hard_coarse_restructure.md`
- Situation: Hard proof-formalization worker with explicit permission to mutate the accepted coarse package.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the active node, authorized impact region, reviewer guidance, and related tablet files.
- Read the proof-formalization worker skill file before starting.

**Apparently Available Actions**
- Edit the active node's `.lean` and `.tex` files.
- Edit existing nodes inside the authorized impact region.
- Change accepted coarse-node statements or `.tex` files when needed for the same target-centered restructure.
- Edit import lines in `Preamble.lean` as needed.
- Create new nodes whose resulting placement is inside the authorized region and include provenance hints when needed.
- Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.

**Prompt-Instructed Completion Steps**
- Run the proof scope checks and `check.py node <active>`.
- Write `worker_handoff.raw.json`.
- Run `check.py worker-handoff ... --phase proof_formalization`.
- Write `worker_handoff.done` if the checker passes.
