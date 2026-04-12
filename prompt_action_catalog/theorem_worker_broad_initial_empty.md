# theorem_worker_broad_initial_empty

- Source prompt: `prompt_catalog/theorem_worker_broad_initial_empty.md`
- Situation: Theorem-stating worker at cycle start with an empty tablet.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the source paper, configured targets, and runtime worker skill file.
- Use the repo-local scratch directory and Loogle while planning the decomposition.

**Apparently Available Actions**
- Create proof-bearing nodes and definition nodes as `.lean`/`.tex` pairs.
- Choose imports and DAG edges to build the support structure for the configured targets.
- Add definition nodes with actual bodies and specific Mathlib imports.
- Assign `difficulty_hints` for new nodes.
- Provide `paper_provenance_hints` for new paper-anchored nodes and for any new definition that is intended to cover a configured target; otherwise that target will remain uncovered later.
- Optionally fully prove a node in Lean if it is immediately available and the exact node check passes.
- Return `NOT_STUCK`, `STUCK`, `DONE`, `NEED_INPUT`, or `CRISIS`; `CRISIS` is only actually available in broad theorem-stating with no current soundness target and no Tablet edits.

**Prompt-Instructed Completion Steps**
- Run `check.py tablet ...`.
- Write `worker_handoff.raw.json`.
- Run `check.py worker-handoff ... --phase theorem_stating`.
- Write `worker_handoff.done` if the checker passes.
