# proof_worker_hard_local

- Source prompt: `prompt_catalog/proof_worker_hard_local.md`
- Situation: Hard proof-formalization worker on one node with local hard-mode freedom.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the active node's `.lean` and `.tex`, its imported nodes, source-paper excerpts, and reviewer guidance.
- Read the proof-formalization worker skill file before starting.

**Apparently Available Actions**
- Edit the active node's `.lean` file, including imports and proof body.
- Edit import lines in `Preamble.lean` as needed.
- Create new nodes with matching `.lean`/`.tex` files when they genuinely unblock the proof.
- Update the active node's `.tex` to reflect new helpers in the NL proof.
- Return `NOT_STUCK`, `STUCK`, `DONE`, or `NEED_INPUT`.

**Prompt-Instructed Completion Steps**
- Run the deterministic self-check commands, including `check.py node <active>`.
- Write `worker_handoff.raw.json` with any needed `paper_provenance_hints`.
- Run `check.py worker-handoff ... --phase proof_formalization`.
- Write `worker_handoff.done` if the checker passes.
