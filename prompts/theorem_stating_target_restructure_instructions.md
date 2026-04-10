--- INSTRUCTIONS ---

PHASE: theorem_stating
MODE: target restructure

YOUR GOAL: Strengthen the current soundness target by making paper-faithful DAG changes inside that target's authorized impact region only.

{authorized_region_note}

WHAT YOU MAY EDIT:
- `Tablet/{target}.tex`
- `Tablet/{target}.lean`
- Existing prerequisite nodes of `{target}` when they genuinely need statement/proof/dependency changes for this same target
- Existing downstream consumers of `{target}` when they need mechanical interface or proof updates because this target changed
- New nodes, only when they become genuine prerequisites of `{target}` by the end of the cycle

WHAT YOU MUST NOT EDIT:
- Unrelated nodes outside `{target}`'s authorized impact region
- `Tablet/Preamble.lean` unless the restructure genuinely requires a new specific Mathlib import
- `Tablet.lean`
- Any generated support file
- Broad cleanup edits outside the target slice

RESTRUCTURE EXPECTATIONS:
- Keep the cycle centered on `{target}`; do not switch to a different soundness target
- Prefer paper-facing intermediate claims that make the DAG richer and later Lean formalization cleaner
- Do not invent gratuitous helpers; every new node should reflect real paper structure
- If you add or revise prerequisite nodes, make the dependency chain explicit in `.lean` imports and `.tex` citations
- If the target's statement or interface changes, update any downstream consumers only as far as needed to keep the target-centered region internally consistent
- Every node you touch or create must remain in `{target}`'s authorized impact region by the end of the cycle
- If you can completely close `{target}` or a newly added prerequisite node in Lean within this authorized region, you may do that in this cycle. In that case, run `python3 {check_script} node <node_name> {repo_path}` and only treat the Lean shortcut as complete if that exact deterministic check passes.

TABLET / NODE RULES:
- Every node must still have matching `.lean` and `.tex` files
- Every definition must be concrete: no `opaque`, no `axiom`, no `sorry` in definitions
- Prefer existing Mathlib definitions over project wrappers whenever feasible
- Do not use theorem/lemma/corollary nodes as disguised definitions. If you are introducing a paper-facing concept, make it an actual definition node.
- Use `\noderef{{name}}` to cite other nodes in NL proofs
- The paper's detail level is a floor, not a ceiling

MANDATORY BEFORE SUBMITTING:
- Run `{target_edit_scope_check_command}` and fix any scope violations
- Run `{scoped_tablet_check_command}` and fix any newly introduced deterministic errors in the authorized impact region
- Pre-existing unrelated deterministic errors outside that authorized region do not need to be fixed in this cycle

WHEN DONE:
Write the raw handoff JSON to `{raw_output_path}`:
{{
  "summary": "brief description of the restructure or proof improvement",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": ["list any genuinely new prerequisite nodes you added"],
  "difficulty_hints": {{"new_node_name": "easy | hard"}},
  "kind_hints": {{"new_node_name": "paper_intermediate | paper_main_result"}}
}}

Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase theorem_stating --repo {repo_path}

If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
