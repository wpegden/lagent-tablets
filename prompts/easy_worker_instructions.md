--- INSTRUCTIONS (EASY NODE) ---

YOUR ACTIVE NODE: `{node_name}`
YOUR SINGLE GOAL: Prove `Tablet/{node_name}.lean` using ONLY its existing imports and children.

This node is classified as EASY -- a straightforward proof from existing dependencies.

IMPORTANT: Before starting, read the skill file at `{skill_path}` — it contains Loogle usage, proof strategies, and workflow examples.

WORKFLOW:
1. Work ONLY on `Tablet/{node_name}.lean`, and only on the proof body after `:=`. Do NOT edit `Tablet/{node_name}.tex`, `Preamble.lean`, or any other node's files.
2. When you have a result -- whether the proof compiles or you're stuck -- STOP and write the raw handoff file `{raw_output_path}`.
3. Do NOT move on to other nodes. The reviewer decides what to work on next.

You may:
- Edit the proof body (everything after `:=`) in `Tablet/{node_name}.lean`

You must NOT:
- Add or remove any `import` lines (the existing imports are all you need)
- Create any new files (no new helper nodes)
- Edit `Tablet/{node_name}.tex`, `Tablet/Preamble.lean`, or any other node's files
- Modify the declaration line (`theorem {node_name} ...` -- this is frozen)
- Add `axiom`, `constant`, `unsafe`, `native_decide`, `opaque`, or other forbidden keywords
- Use `import Mathlib` -- only specific submodule imports (already present)

If you believe this node genuinely needs new helpers or structural changes that you cannot accomplish within these constraints, write the raw handoff file with status `STUCK` and explain why. The supervisor will elevate the node to "hard" mode where refactoring is allowed.

MANDATORY BEFORE SUBMITTING: Run the self-check and fix any errors:
  {proof_scope_check_command}
  {proof_worker_delta_check_command}
  python3 {check_script} node {node_name} {repo_path}
You MUST iterate until the checker reports all deterministic node checks pass before writing the handoff.

WHEN DONE -- write the raw handoff JSON to `{raw_output_path}`:
{{
  "summary": "brief description of what you did",
  "status": "NOT_STUCK | STUCK | DONE",
  "new_nodes": []
}}
Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase proof_formalization --repo {repo_path}
If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
