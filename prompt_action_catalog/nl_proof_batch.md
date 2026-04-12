# nl_proof_batch

- Source prompt: `prompt_catalog/nl_proof_batch.md`
- Situation: Batch NL-proof soundness verification for multiple proof-bearing nodes.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the displayed NL proof-bearing nodes and their child-node NL statements.
- Read additional `Tablet/{name}.tex` files if a proof cites nodes not in the prompt.
- Use the source paper as a rigor benchmark for proof detail.

**Apparently Available Actions**
- Judge whether each displayed NL proof rigorously establishes its statement from child-node NL statements.
- Treat the task as purely mathematical; no Lean reading is required.
- Report a single batch PASS/FAIL soundness decision with issues and summary.

**Prompt-Instructed Completion Steps**
- Write `nl_proof_result.raw.json`.
- Run `check.py soundness-batch-result ...`.
- Write `nl_proof_result.done` if the checker passes.
