# proof_reviewer_standard

- Source prompt: `prompt_catalog/proof_reviewer_standard.md`
- Situation: Standard proof-formalization reviewer with verification results and an unsupported-node advisory.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the active-node context, worker handoff/output, verification results, human feedback, and recent reviews.
- Read tablet files as needed to arbitrate correspondence or soundness disagreements.
- Notice any unsupported-node advisory, but treat it as guidance rather than a separate decision object.

**Apparently Available Actions**
- Choose `CONTINUE`, `ADVANCE_PHASE`, `STUCK`, `NEED_INPUT`, or `DONE`.
- Pick the next active node.
- Assign difficulty or elevate an easy node to hard.
- Set `proof_edit_mode` to `local`, `restructure`, or `coarse_restructure`; the non-local modes only take effect if the same hard node remains active.
- Arbitrate verification disagreements and explain the next guidance.

**Prompt-Instructed Completion Steps**
- Write `reviewer_decision.raw.json`.
- Run `check.py reviewer-decision ... --phase proof_formalization`.
- Write `reviewer_decision.done` if the checker passes.
