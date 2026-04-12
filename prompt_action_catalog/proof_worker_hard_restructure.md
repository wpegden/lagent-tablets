# proof_worker_hard_restructure

- Source prompt: `prompt_catalog/proof_worker_hard_restructure.md`
- Situation: Hard proof-formalization worker with reviewer-authorized local restructure.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the active node, authorized impact region, reviewer guidance, and related tablet files.
- Read the proof-formalization worker skill file before starting.

**Apparently Available Actions**
- Edit the active node's `.lean` and `.tex` files.
- Edit existing nodes inside the authorized impact region.
- Edit import lines in `Preamble.lean` as needed.
- Create new nodes whose resulting placement is inside the authorized impact region when they simplify the target.
- Adjust imports and supporting files inside the authorized impact region for the same target-centered restructure, while keeping the active declaration line fixed.
- Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.

**Prompt-Instructed Completion Steps**
- Run the proof scope checks and `check.py node <active>`.
- Write `worker_handoff.raw.json` with any needed `paper_provenance_hints`.
- Run `check.py worker-handoff ... --phase proof_formalization`.
- Write `worker_handoff.done` if the checker passes.
