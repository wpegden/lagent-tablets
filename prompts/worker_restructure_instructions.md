--- INSTRUCTIONS (HARD NODE, REVIEWER-AUTHORIZED RESTRUCTURE) ---

YOUR ACTIVE NODE: `{node_name}`
YOUR PRIMARY GOAL: Eliminate the `sorry` in `Tablet/{node_name}.lean`.

IMPORTANT: Before starting, read the skill file at `{skill_path}` — it contains Loogle usage, proof strategies, and workflow examples.

WORKFLOW:
1. Keep `{node_name}` as the center of the cycle. You may edit nearby existing nodes only inside the authorized impact region below.
2. When you have a result -- whether the proof compiles, you need a different authorization, or you're stuck -- STOP and write the raw handoff file `{raw_output_path}`.
3. Do NOT move on to unrelated parts of the tablet. The reviewer decides what to work on next.

{authorized_region_note}
You may:
- Edit `Tablet/{node_name}.lean` and `Tablet/{node_name}.tex`
- Edit other existing node files only when those nodes are inside the authorized impact region above
- Add or remove `import Tablet.*` or `import Mathlib.*` lines in files inside the authorized impact region
- Add `import Mathlib.*` lines to `Tablet/Preamble.lean` (additions only, no removals)
- Create new helper nodes: write both `Tablet/{{name}}.lean` and `Tablet/{{name}}.tex` files
- Update the STRATEGY comment block with your approach, blockers, and failed attempts

You must NOT:
- Edit existing nodes outside the authorized impact region
- Modify the declaration line of `{node_name}` unless your reviewer-authorized restructure genuinely requires a paper-faithful statement adjustment for this same target
- Add `axiom`, `constant`, `unsafe`, `native_decide`, `opaque`, or other forbidden keywords
- Use `sorry` in definitions -- only in theorem/lemma proof bodies
- Use `import Mathlib` -- only specific submodule imports

If you believe even this broader target-centered restructure is insufficient, return `status: STUCK` and explain what broader refactor would be required.

MANDATORY BEFORE SUBMITTING: Run the self-check and fix any errors:
  {proof_scope_check_command}
  {proof_worker_delta_check_command}
  python3 {check_script} node {node_name} {repo_path}
You MUST iterate until the checker reports all deterministic checks pass before writing the handoff.

WHEN DONE -- write the raw handoff JSON to `{raw_output_path}`:
{{
  "summary": "brief description of what you did",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": ["list", "of", "new", "node", "names"]
}}
Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase proof_formalization --repo {repo_path}
If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
