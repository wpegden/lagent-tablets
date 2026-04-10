IMPORTANT: Before deciding, read the skill file at `{skill_path}` for evaluation guidelines, NL verification arbitration rules, and node selection strategy.

--- YOUR DECISION ---

Decide what to do next. Write your decision as JSON to the raw file `{raw_output_path}`:

{{
  "decision": "CONTINUE | ADVANCE_PHASE | STUCK | NEED_INPUT | DONE",
  "reason": "brief explanation",
  "next_prompt": "specific guidance for the worker's next cycle",
  "next_active_node": "name of the node the worker should focus on next",
  "paper_focus_ranges": [
    {{
      "start_line": 420,
      "end_line": 462,
      "reason": "main theorem statement to keep in view"
    }}
  ],
  "difficulty_assignments": {{"node_name": "easy or hard"}},
  "elevate_to_hard": ["node_name_if_easy_mode_is_insufficient"],
  "proof_edit_mode": "local | restructure"
}}

Guidelines:
- CONTINUE: the worker is making progress. Pick the most impactful node to work on next.
- ADVANCE_PHASE: all proof_formalization work is done (every node closed). Move to cleanup.
- STUCK: the worker has tried multiple approaches and is not making progress. This triggers stuck recovery.
- NEED_INPUT: a human needs to provide mathematical guidance.
- DONE: the entire project is complete.
- `proof_edit_mode` defaults to `local`. Set it to `restructure` only when you are explicitly authorizing a broader refactor around the same hard active node inside its target-centered impact region.
- When a hard-mode worker returns `STUCK` because nearby existing nodes must change, you may keep the same node active with `decision: "CONTINUE"` and `proof_edit_mode: "restructure"` instead of treating it as generic stuck recovery.
- `paper_focus_ranges` is mandatory. Include the source-paper line ranges the next worker should have inlined for focused context. Use `[]` when no specific excerpt is needed.
- Prefer short, high-signal ranges: theorem statements, notation blocks, or the exact proof paragraphs the worker should track next. Do not dump broad sections when a targeted excerpt will do.

For next_active_node: pick the node whose dependencies are already closed (it can be proved now).
Prefer the most blocking or most uncertain node.

NODE DIFFICULTY:
Each node is classified as "easy" or "hard":
- **easy**: A straightforward Lean proof from existing children. The worker can only edit the proof body -- no new imports, no new files. Use a faster/cheaper model.
- **hard**: A challenging proof that may require creating helper lemmas, refactoring imports, or other structural changes. Uses a stronger model.

Hard mode is still node-centered by default. Broader edits to nearby existing nodes require deliberate reviewer authorization via `proof_edit_mode: "restructure"`; they are not part of ordinary hard-mode freedom.

You may assign or reassign difficulty via `difficulty_assignments`. You may elevate an easy node to hard via `elevate_to_hard` if you see the worker struggling (check the "attempts" count in the tablet status). The supervisor auto-elevates after 2 failed easy attempts.

If NL verification results are shown above, review them carefully. Verification agents may
disagree. You are the final arbiter:
- If verification agents approve unanimously: accept the changes.
- If verification agents reject: you may override if you believe the rejection is wrong, but explain why.
- If agents disagree: weigh their reasoning and make a judgment call.

MANDATORY:
1. Write the JSON to `{raw_output_path}`.
2. Run `python3 {check_script} reviewer-decision {raw_output_path} --phase {phase}`.
3. If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
