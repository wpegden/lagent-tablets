IMPORTANT: Before deciding, read the skill file at `{skill_path}`.

--- YOUR DECISION ---

The cleanup phase is terminal polish over an already accepted tablet.
Do not ask for semantic changes or phase rollback. If a cleanup attempt is invalid,
either ask for a narrower cleanup attempt or stop successfully with `DONE`.

Write your decision as JSON to the raw file `{raw_output_path}`:

{{
  "decision": "CONTINUE | NEED_INPUT | DONE",
  "reason": "brief explanation",
  "next_prompt": "specific guidance for the worker's next cleanup cycle",
  "next_active_node": "name of the node to focus cleanup on, or empty if not needed",
  "paper_focus_ranges": [
    {{
      "start_line": 420,
      "end_line": 462,
      "reason": "optional paper excerpt to keep visible"
    }}
  ],
  "feedback": "optional short note if the task/setup seems impossible, inconsistent, or poorly supported"
}}

Guidelines:
- CONTINUE: the cleanup work is useful and still semantics-preserving.
- NEED_INPUT: a human should decide whether additional polish is worthwhile or specify a preferred presentation/style.
- DONE: stop successfully with the last good proof-complete state.
- `paper_focus_ranges` is mandatory. Use `[]` when no excerpt is needed.

MANDATORY:
1. Write the JSON to `{raw_output_path}`.
2. Run `python3 {check_script} reviewer-decision {raw_output_path} --phase {phase} --repo {repo_path}`.
3. If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
