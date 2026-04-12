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
- Edit import lines in `Tablet/Preamble.lean` as needed
- Create new nodes inside the authorized region when they genuinely simplify the target, following the shared node spec for their chosen statement environments
- Update the STRATEGY comment block with your approach, blockers, and failed attempts

You must NOT:
- Edit existing nodes outside the authorized impact region
- Modify the declaration line of `{node_name}`
- Add `axiom`, `constant`, `unsafe`, `native_decide`, `opaque`, or other forbidden keywords
- Use `sorry` in definitions -- only in proof-bearing theorem-like declaration bodies (`helper`, `lemma`, `theorem`, `corollary`)
- Use `import Mathlib` -- only specific submodule imports

If you believe even this broader target-centered restructure is insufficient, return `status: STUCK` and explain what broader refactor would be required.

This mode still does NOT authorize mutation of the accepted coarse theorem-stating package. If a coarse node's accepted statement, `.tex`, or coarse-to-coarse package structure must change, stop and request reviewer-authorized `proof_edit_mode: "coarse_restructure"` instead.

New paper-anchored `theorem`/`lemma`/`corollary` nodes in proof_formalization can be legitimate when the local proof work exposes a missing statement. The same is true for new `definition` nodes that are intended to cover a configured `main_result_target`. If you create either kind of node, include structured `paper_provenance_hints` in the handoff and keep it strictly inside this authorized local restructure; do not use it as a back door to mutate the accepted coarse package.

MANDATORY BEFORE SUBMITTING: Run the self-check and fix any errors:
  {proof_scope_check_command}
  {proof_worker_delta_check_command}
  python3 {check_script} node {node_name} {repo_path}
You MUST iterate until the checker reports all deterministic checks pass before writing the handoff.

WHEN DONE -- write the raw handoff JSON to `{raw_output_path}`:
{{
  "summary": "brief description of what you did",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": ["list", "of", "new", "node", "names"],
  "paper_provenance_hints": {{
    "paper_result_node": {{"start_line": 130, "end_line": 148, "tex_label": "sum"}}
  }},
  "feedback": "optional short note if the task/setup seems impossible, inconsistent, or poorly supported"
}}
Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase proof_formalization --repo {repo_path}
Wait for that command to finish. Do not start any other repo command after launching this final acceptance check.
If that passes, write the completion marker `{done_path}` and stop. Do not write the completion marker while that checker is still running.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
