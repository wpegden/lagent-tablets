--- INSTRUCTIONS ---

PHASE: theorem_stating
MODE: target restructure

YOUR GOAL: Strengthen the current soundness target by making paper-faithful DAG changes inside that target's prerequisite slice only.

WHAT YOU MAY EDIT:
- `Tablet/{target}.tex`
- `Tablet/{target}.lean`
- Existing prerequisite nodes of `{target}` when they genuinely need statement/proof/dependency changes for this same target
- New nodes, only when they become genuine prerequisites of `{target}` by the end of the cycle

WHAT YOU MUST NOT EDIT:
- Unrelated nodes outside `{target}`'s final prerequisite slice
- `Tablet/Preamble.lean` unless the restructure genuinely requires a new specific Mathlib import
- `Tablet.lean`
- Any generated support file
- Broad cleanup edits outside the target slice

RESTRUCTURE EXPECTATIONS:
- Keep the cycle centered on `{target}`; do not switch to a different soundness target
- Prefer paper-facing intermediate claims that make the DAG richer and later Lean formalization cleaner
- Do not invent gratuitous helpers; every new node should reflect real paper structure
- If you add or revise prerequisite nodes, make the dependency chain explicit in `.lean` imports and `.tex` citations
- Every node you touch or create must end up in `{target}`'s prerequisite chain by the end of the cycle

TABLET / NODE RULES:
- Every node must still have matching `.lean` and `.tex` files
- Every definition must be concrete: no `opaque`, no `axiom`, no `sorry` in definitions
- Prefer existing Mathlib definitions over project wrappers whenever feasible
- Use `\noderef{{name}}` to cite other nodes in NL proofs
- The paper's detail level is a floor, not a ceiling

MANDATORY BEFORE SUBMITTING:
- Run `python3 {check_script} tablet {repo_path}` and fix any deterministic errors

WHEN DONE:
Write the raw handoff JSON to `{raw_output_path}`:
{{
  "summary": "brief description of the restructure or proof improvement",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": ["list any genuinely new prerequisite nodes you added"],
  "difficulty_hints": {{"node_name": "easy | hard"}}
}}

Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase theorem_stating --repo {repo_path}

If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
