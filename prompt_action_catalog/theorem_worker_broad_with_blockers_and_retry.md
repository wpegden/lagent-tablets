# theorem_worker_broad_with_blockers_and_retry

- Source prompt: `prompt_catalog/theorem_worker_broad_with_blockers_and_retry.md`
- Situation: Broad theorem-stating worker with open blockers, support actions, and a preserved invalid retry.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the source paper, focused paper excerpts, current tablet snapshot, reviewer guidance, open blockers, support actions, and prior invalid blocker.
- Continue from the preserved invalid worktree.
- Use the runtime worker skill file, scratch area, and Loogle as needed.

**Apparently Available Actions**
- Repair the target-support DAG while keeping the cycle local to the deepest unresolved slice.
- Resolve open blockers before treating theorem-stating as complete.
- Remove unsupported nodes or connect them into a real support chain when the prompt tells you to do so.
- Create or revise nodes, imports, and NL proofs broadly because there is no current soundness target.
- Assign `difficulty_hints` and `paper_provenance_hints` for new nodes; any new definition intended to cover a configured target needs structured provenance for that target to count as covered later.
- Optionally close nodes in Lean if their deterministic node checks pass.
- Return `NOT_STUCK`, `STUCK`, `DONE`, `NEED_INPUT`, or `CRISIS`; `CRISIS` is only actually available in broad theorem-stating with no current soundness target and no Tablet edits.

**Prompt-Instructed Completion Steps**
- Run `check.py tablet ...`.
- Write `worker_handoff.raw.json`.
- Run `check.py worker-handoff ... --phase theorem_stating`.
- Write `worker_handoff.done` if the checker passes.
