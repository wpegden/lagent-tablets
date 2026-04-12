--- INSTRUCTIONS (HARD NODE, REVIEWER-AUTHORIZED COARSE-RESTRUCTURE) ---

YOUR ACTIVE NODE: `{node_name}`
YOUR PRIMARY GOAL: Eliminate the `sorry` in `Tablet/{node_name}.lean`, while deliberately mutating the accepted coarse theorem-stating package in a controlled way.

IMPORTANT: Before starting, read the skill file at `{skill_path}` — it contains Loogle usage, proof strategies, and workflow examples.

WORKFLOW:
1. Keep `{node_name}` as the center of the cycle. Coarse-package edits are authorized only inside the impact region below.
2. When you have a result -- whether the proof compiles, the coarse restructure works, or you are still blocked -- STOP and write the raw handoff file `{raw_output_path}`.
3. Do NOT move on to unrelated parts of the tablet. The reviewer decides what to work on next.

{authorized_region_note}
You may:
- Edit `Tablet/{node_name}.lean` and `Tablet/{node_name}.tex`
- Edit other existing node files only when those nodes are inside the authorized impact region above
- Change accepted coarse-node statements or `.tex` files when that is genuinely necessary for the same target-centered restructure
- Add or remove `import Tablet.*` or `import Mathlib.*` lines in files inside the authorized impact region
- Edit import lines in `Tablet/Preamble.lean` as needed
- Create new nodes inside the authorized region when they genuinely improve the coarse package, following the shared node spec for their chosen statement environments
- Update the STRATEGY comment block with your approach, blockers, and failed attempts

You must NOT:
- Edit existing nodes outside the authorized impact region
- Treat this as a general whole-tablet refactor
- Add `axiom`, `constant`, `unsafe`, `native_decide`, `opaque`, or other forbidden keywords
- Use `sorry` in definitions -- only in proof-bearing theorem-like declaration bodies (`helper`, `lemma`, `theorem`, `corollary`)
- Use `import Mathlib` -- only specific submodule imports

This mode has a high bar. Use it only because the accepted coarse theorem-stating package itself must change. If the issue can be solved by adding non-coarse helpers beneath the existing coarse package, do that in ordinary proof mode instead.

After the usual deterministic and local verification checks, the supervisor will run a coarse-wide correspondence / paper-faithfulness sweep over the resulting coarse package before accepting this cycle. New nodes you create during a successful coarse-restructure will become part of that refreshed coarse package.

If you create new paper-anchored `theorem`/`lemma`/`corollary` nodes in this mode, or new `definition` nodes that are intended to cover configured `main_result_targets`, include structured `paper_provenance_hints` in the handoff. Treat those nodes as part of the coarse-package redesign, not as disposable helper churn.

If you believe even this broader coarse-restructure is insufficient, return `status: STUCK` and explain what still blocks progress.

MANDATORY BEFORE SUBMITTING: Run the self-check and fix any errors:
  {proof_scope_check_command}
  {proof_worker_delta_check_command}
  python3 {check_script} node {node_name} {repo_path}
You MUST iterate until the checker reports all deterministic checks pass before writing the handoff.

WHEN DONE -- write the raw handoff JSON to `{raw_output_path}`:
{{
  "summary": "brief description of what you changed in the coarse package",
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
