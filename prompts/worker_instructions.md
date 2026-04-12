--- INSTRUCTIONS ---

YOUR ACTIVE NODE: `{node_name}`
YOUR SINGLE GOAL: Eliminate the `sorry` in `Tablet/{node_name}.lean`.

IMPORTANT: Before starting, read the skill file at `{skill_path}` — it contains Loogle usage, proof strategies, and workflow examples.

WORKFLOW:
1. Work ONLY on `Tablet/{node_name}.lean`. Do NOT edit any other node's .lean file.
2. When you have a result -- whether the proof compiles, you need helpers, or you're stuck -- STOP and write the raw handoff file `{raw_output_path}`.
3. Do NOT move on to other nodes. The reviewer decides what to work on next.

You may:
- Edit the proof body (everything after `:=`) in `Tablet/{node_name}.lean`
- Add or remove `import Tablet.*` or `import Mathlib.*` lines in `Tablet/{node_name}.lean`
- Edit import lines in `Tablet/Preamble.lean` as needed
- Create new nodes when they genuinely unblock the proof: write both `Tablet/{{name}}.lean` and `Tablet/{{name}}.tex` files and follow the shared node spec for the chosen statement environment
- Update `Tablet/{node_name}.tex` to reflect new helpers in your NL proof
- Update the STRATEGY comment block with your approach, blockers, and failed attempts

You must NOT:
- Edit any other existing node's `.lean` file (they are read-only)
- Modify the declaration line (`theorem {node_name} ...` -- this is frozen)
- Add `axiom`, `constant`, `unsafe`, `native_decide`, `opaque`, or other forbidden keywords
- Use `sorry` only in proof-bearing theorem-like declaration bodies (`helper`, `lemma`, `theorem`, `corollary`); never in definitions
- Use `import Mathlib` -- only specific submodule imports (e.g., `import Mathlib.Analysis.SpecialFunctions.Log.Basic`)

New paper-anchored `theorem`/`lemma`/`corollary` nodes in proof_formalization can be legitimate when the local proof work exposes a missing statement. The same is true for new `definition` nodes that are intended to cover a configured `main_result_target`. If you create either kind of node, it must satisfy the full node spec, including structured `paper_provenance_hints`, and it must not mutate the accepted coarse package unless the reviewer has explicitly authorized `proof_edit_mode: "coarse_restructure"`.
If you create or edit a node that covers one of the configured `main_result_targets`, treat it as part of the human-reviewed target package rather than disposable local churn.

Hard mode is still node-centered. If you conclude that this node needs edits to other existing nodes, stop and return `status: STUCK` with a concrete broader-restructure request; only the reviewer can authorize that wider scope.

If `{node_name}` is part of the accepted coarse theorem-stating package, ordinary proof-formalization may still fill in its Lean proof and add non-coarse helpers beneath it, but it must NOT mutate that accepted coarse package. In particular, changing the coarse node's `.tex`, changing its accepted statement/interface, or changing coarse-to-coarse structure requires reviewer-authorized `proof_edit_mode: "coarse_restructure"`.

MANDATORY BEFORE SUBMITTING: Run the self-check and fix any errors:
  {proof_scope_check_command}
  {proof_worker_delta_check_command}
  python3 {check_script} node {node_name} {repo_path}
You MUST iterate until the checker reports all deterministic node checks pass before writing the handoff.

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
