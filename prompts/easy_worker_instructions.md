--- INSTRUCTIONS (EASY NODE) ---

YOUR ACTIVE NODE: `{node_name}`
YOUR SINGLE GOAL: Prove `Tablet/{node_name}.lean` using ONLY its existing imports and children.

This node is classified as EASY -- a straightforward proof from existing dependencies.

IMPORTANT: Before starting, read the skill file at `{skill_path}` — it contains Loogle usage, proof strategies, and workflow examples.

WORKFLOW:
1. Work ONLY on `Tablet/{node_name}.lean`. Do NOT edit any other file.
2. When you have a result -- whether the proof compiles or you're stuck -- STOP and write `worker_handoff.json`.
3. Do NOT move on to other nodes. The reviewer decides what to work on next.

You may:
- Edit the proof body (everything after `:=`) in `Tablet/{node_name}.lean`
- Update `Tablet/{node_name}.tex` to reflect your proof
- Update the STRATEGY comment block with your approach, blockers, and failed attempts

You must NOT:
- Add or remove any `import` lines (the existing imports are all you need)
- Create any new files (no new helper nodes)
- Edit `Tablet/Preamble.lean` or any other node's files
- Modify the declaration line (`theorem {node_name} ...` -- this is frozen)
- Add `axiom`, `constant`, `unsafe`, `native_decide`, `opaque`, or other forbidden keywords
- Use `import Mathlib` -- only specific submodule imports (already present)

If you believe this node genuinely needs new helpers or structural changes that you cannot accomplish within these constraints, write `worker_handoff.json` with status `STUCK` and explain why. The supervisor will elevate the node to "hard" mode where refactoring is allowed.

MANDATORY BEFORE SUBMITTING: Run the self-check and fix any errors:
  {check_node} {node_name}
You MUST iterate until check_node.sh reports all checks pass before writing worker_handoff.json.

WHEN DONE -- write `worker_handoff.json`:
{{
  "summary": "brief description of what you did",
  "status": "NOT_STUCK | STUCK | DONE",
  "new_nodes": []
}}
Stop after writing this file. The supervisor takes over.
