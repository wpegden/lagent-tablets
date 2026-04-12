--- INSTRUCTIONS ---

PHASE: theorem_stating
MODE: target repair

YOUR GOAL: Repair the NL proof of the current soundness target without changing the current DAG or any node statements.

WHAT YOU MAY EDIT:
- `Tablet/{target}.tex` only

WHAT YOU MUST NOT EDIT:
- Any `.lean` file
- Any other `.tex` file
- `Tablet/Preamble.lean`
- `Tablet.lean`
- Any generated support file
- Any node set, dependency edge, or node statement

SCRATCH WORK:
- If you need a temporary Lean experiment or note file, use `{scratch_dir}` rather than `/tmp`
- `example.lean` in that directory is a trivial buildable starting point

WHEN TO REQUEST RESTRUCTURE:
- If you think this proof should be preceded by richer dependencies or meaningful intermediate nodes
- If you think any statement, import list, or dependency edge needs to change
- If the current child nodes do not give a paper-faithful route to a rigorous proof of the target

In any of those cases, do NOT make the broader edits yourself in this cycle. Instead, stop and write the handoff with status `STUCK`, and explain the concrete DAG enrichment or dependency changes you think are needed.

PROOF EXPECTATIONS:
- Keep the target node statement fixed
- Keep the proof rigorous, not sketch-level
- The paper's detail level is a floor, not a ceiling
- Cite existing child nodes with `\noderef{{name}}`
- Do not cite nodes that are not already imported by the target's `.lean` file

MANDATORY BEFORE SUBMITTING:
- Run `{target_repair_scope_check_command}` and fix any scope violations
- Run `python3 {check_script} tablet {repo_path}` and fix any deterministic errors

WHEN DONE:
Write the raw handoff JSON to `{raw_output_path}`:
{{
  "summary": "brief description of the proof repair or the restructure request",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": [],
  "difficulty_hints": {{}},
  "feedback": "optional short note if the task/setup seems impossible, inconsistent, or poorly supported"
}}

Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase theorem_stating --repo {repo_path}

Wait for that command to finish. Do not start any other repo command after launching this final acceptance check.
If that passes, write the completion marker `{done_path}` and stop. Do not write the completion marker while that checker is still running.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
