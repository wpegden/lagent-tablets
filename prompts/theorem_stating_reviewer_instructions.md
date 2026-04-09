Read the skill file at `{skill_path}` for evaluation guidelines.

--- YOUR DECISION ---

Evaluate the theorem-stating work. Check:
1. Are ALL main theorems and lemmas with complete proofs in the paper represented as nodes? (Auxiliary remarks, examples, and proof sketches do not need to be included unless they help formalize the main results.)
2. Do the Lean declarations accurately capture the paper's statements?
3. Is the DAG decomposition reasonable? Are intermediate steps genuine and non-trivial?
4. Are the NL proofs in .tex files rigorous and complete (not sketches)?
5. Does `Tablet/Preamble.lean` use specific Mathlib imports (not bare `import Mathlib`)?
6. Are all `.lean` files syntactically valid (lake build passes)?
7. Would you be confident starting proof_formalization with this tablet structure?

Write your decision as JSON to the raw file `{raw_output_path}`:

{{
  "decision": "CONTINUE | ADVANCE_PHASE | NEED_INPUT",
  "reason": "brief explanation",
  "next_prompt": "specific guidance for the worker",
  "next_active_node": "name of the first node to prove (required for ADVANCE_PHASE)",
  "issues": ["list of specific issues to fix, or empty"],
  "paper_focus_ranges": [
    {{
      "start_line": 420,
      "end_line": 462,
      "reason": "main theorem statement to keep in view"
    }}
  ],
  "orphan_resolutions": [
    {{
      "node": "orphan node name",
      "action": "remove | keep_and_add_dependency",
      "reason": "why this node should be removed or where the missing downstream dependency is",
      "suggested_parents": ["node names that should import/cite it if it should stay"]
    }}
  ],
  "open_rejections": [
    {{
      "node": "node name or (global)",
      "phase": "correspondence | paper_faithfulness",
      "reason": "why this rejection is still open and what must change"
    }}
  ]
}}

- CONTINUE: the worker needs to refine the statements or add missing nodes. Be specific about what to fix.
- ADVANCE_PHASE: the tablet is ready for proof_formalization. All main results with complete proofs in the paper are represented, NL proofs are complete, lake build passes, and the DAG structure is sound. You MUST set `next_active_node` to the node the worker should prove first — choose the node where work is most likely to change later plans (favoring hard or low-level nodes).
- NEED_INPUT: a mathematical question requires human judgment.
- `paper_focus_ranges` is mandatory. Include the source-paper line ranges the next worker should have inlined for focused context. Use `[]` when no specific excerpt is needed.
- Prefer short, high-signal ranges: theorem statements, notation blocks, or the exact proof paragraphs the worker should track next. Do not dump broad sections when a targeted excerpt will do.
- `orphan_resolutions` is mandatory. Include one entry for every CURRENT ORPHAN CANDIDATE shown in the prompt. Use `[]` only when there are no orphan candidates.
- Use `remove` when the node should be deleted from the tablet.
- Use `keep_and_add_dependency` when the node is mathematically needed but the worker failed to connect it to a real parent; name the expected parent nodes in `suggested_parents` when you can.
- `open_rejections` is mandatory. Include one entry for every CURRENTLY OPEN correspondence or paper-faithfulness rejection. Use `[]` only when that list is empty.
- Every blocker you mention in `reason`, `issues`, or `next_prompt` must also appear in `open_rejections`. Do not keep a second blocker list only in prose.
- If orphan candidates remain, do NOT advance. The worker must either remove them or add the missing downstream dependency/citation first.
- Do NOT advance while `open_rejections` is non-empty.

Do NOT advance unless ALL main results with complete proofs in the paper are represented and the decomposition genuinely covers the paper's argument.

MANDATORY:
1. Write the JSON to `{raw_output_path}`.
2. Run `python3 {check_script} reviewer-decision {raw_output_path} --phase {phase}`.
3. If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
