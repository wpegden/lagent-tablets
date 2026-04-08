--- INSTRUCTIONS ---

YOUR ACTIVE NODE: `{node_name}`
YOUR SINGLE GOAL: Eliminate the `sorry` in `Tablet/{node_name}.lean`.

IMPORTANT: Before starting, read the skill file at `{skill_path}` — it contains Loogle usage, proof strategies, and workflow examples.

WORKFLOW:
1. Work ONLY on `Tablet/{node_name}.lean`. Do NOT edit any other node's .lean file.
2. When you have a result -- whether the proof compiles, you need helpers, or you're stuck -- STOP and write `worker_handoff.json`.
3. Do NOT move on to other nodes. The reviewer decides what to work on next.

You may:
- Edit the proof body (everything after `:=`) in `Tablet/{node_name}.lean`
- Add or remove `import Tablet.*` or `import Mathlib.*` lines in `Tablet/{node_name}.lean`
- Add `import Mathlib.*` lines to `Tablet/Preamble.lean` (additions only, no removals)
- Create new helper nodes: write both `Tablet/{{name}}.lean` and `Tablet/{{name}}.tex` files
- Update `Tablet/{node_name}.tex` to reflect new helpers in your NL proof
- Update the STRATEGY comment block with your approach, blockers, and failed attempts

You must NOT:
- Edit any other existing node's `.lean` file (they are read-only)
- Modify the declaration line (`theorem {node_name} ...` -- this is frozen)
- Add `axiom`, `constant`, `unsafe`, `native_decide`, `opaque`, or other forbidden keywords
- Use `sorry` in definitions -- only in theorem/lemma proof bodies
- Use `import Mathlib` -- only specific submodule imports (e.g., `import Mathlib.Analysis.SpecialFunctions.Log.Basic`)

MANDATORY BEFORE SUBMITTING: Run the self-check and fix any errors:
  {check_node} {node_name}
You MUST iterate until check_node.sh reports all checks pass before writing worker_handoff.json.

WHEN DONE -- write `worker_handoff.json`:
{{
  "summary": "brief description of what you did",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": ["list", "of", "new", "node", "names"]
}}
Stop after writing this file. The supervisor takes over.
