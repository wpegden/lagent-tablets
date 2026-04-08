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

Write your decision as JSON to the file `reviewer_decision.json`:

{{
  "decision": "CONTINUE | ADVANCE_PHASE | NEED_INPUT",
  "reason": "brief explanation",
  "next_prompt": "specific guidance for the worker",
  "next_active_node": "name of the first node to prove (required for ADVANCE_PHASE)",
  "issues": ["list of specific issues to fix, or empty"]
}}

- CONTINUE: the worker needs to refine the statements or add missing nodes. Be specific about what to fix.
- ADVANCE_PHASE: the tablet is ready for proof_formalization. All main results with complete proofs in the paper are represented, NL proofs are complete, lake build passes, and the DAG structure is sound. You MUST set `next_active_node` to the node the worker should prove first — choose the node where work is most likely to change later plans (favoring hard or low-level nodes).
- NEED_INPUT: a mathematical question requires human judgment.

Do NOT advance unless ALL main results with complete proofs in the paper are represented and the decomposition genuinely covers the paper's argument.

MANDATORY: Write the JSON to `reviewer_decision.json` then stop. Do not continue after writing the file.
