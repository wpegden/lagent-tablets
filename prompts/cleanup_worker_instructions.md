--- INSTRUCTIONS (CLEANUP PHASE) ---

YOUR CURRENT FOCUS: `{node_name}`
YOUR SINGLE GOAL: Make semantics-preserving cleanup edits only.

IMPORTANT: Before starting, read the skill file at `{skill_path}`.

WORKFLOW:
1. The proof tablet is already accepted as mathematically complete.
2. You may do polish only: Lean proof refactors, formatting, comments, import tidying, or similarly harmless cleanup.
3. Do NOT create new nodes, delete nodes, change theorem/definition statements, or modify any `.tex` file.
4. When you are done, stop and write the raw handoff file `{raw_output_path}`.

MANDATORY BEFORE SUBMITTING: Run the cleanup-preservation self-check and fix any errors:
  {cleanup_check_command}

WHEN DONE -- write the raw handoff JSON to `{raw_output_path}`:
{{
  "summary": "brief description of the cleanup work",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": []
}}
Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase proof_complete_style_cleanup --repo {repo_path}
Wait for that command to finish. Do not start any other repo command after launching this final acceptance check.
If that passes, write the completion marker `{done_path}` and stop. Do not write the completion marker while that checker is still running.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
