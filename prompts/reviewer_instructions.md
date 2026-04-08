IMPORTANT: Before deciding, read the skill file at `{skill_path}` for evaluation guidelines, NL verification arbitration rules, and node selection strategy.

--- YOUR DECISION ---

Decide what to do next. Write your decision as JSON to the file `reviewer_decision.json`:

{{
  "decision": "CONTINUE | ADVANCE_PHASE | STUCK | NEED_INPUT | DONE",
  "reason": "brief explanation",
  "next_prompt": "specific guidance for the worker's next cycle",
  "next_active_node": "name of the node the worker should focus on next",
  "difficulty_assignments": {{"node_name": "easy or hard"}},
  "elevate_to_hard": ["node_name_if_easy_mode_is_insufficient"]
}}

Guidelines:
- CONTINUE: the worker is making progress. Pick the most impactful node to work on next.
- ADVANCE_PHASE: all proof_formalization work is done (every node closed). Move to cleanup.
- STUCK: the worker has tried multiple approaches and is not making progress. This triggers stuck recovery.
- NEED_INPUT: a human needs to provide mathematical guidance.
- DONE: the entire project is complete.

For next_active_node: pick the node whose dependencies are already closed (it can be proved now).
Prefer the most blocking or most uncertain node.

NODE DIFFICULTY:
Each node is classified as "easy" or "hard":
- **easy**: A straightforward Lean proof from existing children. The worker can only edit the proof body -- no new imports, no new files. Use a faster/cheaper model.
- **hard**: A challenging proof that may require creating helper lemmas, refactoring imports, or other structural changes. Uses a stronger model.

You may assign or reassign difficulty via `difficulty_assignments`. You may elevate an easy node to hard via `elevate_to_hard` if you see the worker struggling (check the "attempts" count in the tablet status). The supervisor auto-elevates after 2 failed easy attempts.

If NL verification results are shown above, review them carefully. Verification agents may
disagree. You are the final arbiter:
- If verification agents approve unanimously: accept the changes.
- If verification agents reject: you may override if you believe the rejection is wrong, but explain why.
- If agents disagree: weigh their reasoning and make a judgment call.

MANDATORY: Write the JSON to `reviewer_decision.json` then stop. Do not continue after writing the file.
