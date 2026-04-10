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
  "target_edit_mode": "repair | restructure",
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
  "open_blockers": [
    {{
      "node": "node name or (global)",
      "phase": "correspondence | paper_faithfulness | soundness",
      "reason": "why this blocker is still open and what must change"
    }}
  ]
}}

- CONTINUE: the worker needs another theorem_stating cycle. Be specific about what to fix. In `repair` mode this usually means refining the target node's `.tex` proof only; use `restructure` when you want to authorize broader DAG or statement changes for the same target.
- `target_edit_mode` is mandatory whenever theorem_stating has a CURRENT SOUNDNESS TARGET. Use `repair` by default. If the current target is still unresolved, that means the next worker may edit only the target `.tex` proof. If the current target has already passed soundness in this cycle, leaving `target_edit_mode` at `repair` means the next cycle will move on automatically to the next unresolved target. Use `restructure` only when you are explicitly authorizing broader paper-faithful edits because this same target should be reopened for richer dependencies, meaningful intermediate nodes, or other prerequisite work before it is really settled.
- ADVANCE_PHASE: the tablet is ready for proof_formalization. All main results with complete proofs in the paper are represented, NL proofs are complete, lake build passes, and the DAG structure is sound. You MUST set `next_active_node` to the node the worker should prove first — choose the node where work is most likely to change later plans (favoring hard or low-level nodes).
- NEED_INPUT: a mathematical question requires human judgment.
- `paper_focus_ranges` is mandatory. Include the source-paper line ranges the next worker should have inlined for focused context. Use `[]` when no specific excerpt is needed.
- Prefer short, high-signal ranges: theorem statements, notation blocks, or the exact proof paragraphs the worker should track next. Do not dump broad sections when a targeted excerpt will do.
- `orphan_resolutions` is mandatory. Include one entry for every CURRENT ORPHAN CANDIDATE shown in the prompt. Use `[]` only when there are no orphan candidates.
- Use `remove` when the node should be deleted from the tablet.
- Use `keep_and_add_dependency` when the node is mathematically needed but the worker failed to connect it to a real parent; name the expected parent nodes in `suggested_parents` when you can.
- `open_blockers` is mandatory. Include one entry for every CURRENTLY OPEN theorem-stating blocker. Use `[]` only when that list is empty.
- Every blocker you mention in `reason`, `issues`, or `next_prompt` must also appear in `open_blockers`. Do not keep a second blocker list only in prose.
- The supervisor chooses theorem-stating soundness targets deterministically in deepest-first DAG order. If the prompt shows a CURRENT SOUNDNESS TARGET, keep your guidance focused on that node. If that target needs richer dependencies, meaningful intermediate nodes, or other prerequisite work, keep the focus on the same target but authorize `restructure` and describe the prerequisite work concretely rather than inventing a different target.
- If there is a CURRENT SOUNDNESS TARGET in `repair` mode, the worker is hard-locked to the target node's `.tex` file. Do not tell it to edit other files unless you set `target_edit_mode` to `restructure`.
- If there is a CURRENT SOUNDNESS TARGET in `restructure` mode, treat edits outside that target's prerequisite chain as off-target drift.
- If there is no CURRENT SOUNDNESS TARGET, keep your guidance local to the deepest unresolved DAG slice rather than inviting broad opportunistic rewrites across unrelated nodes.
- In theorem_stating, richer DAG structure is generally good when it reflects real paper structure and will make later Lean formalization more tractable. Do not invent gratuitous helpers, but do recommend restructuring when the paper naturally breaks the argument into meaningful intermediate steps.
- If the soundness feedback includes `STRUCTURAL` objections, take them seriously. When they point to clear paper-facing intermediate steps, missing real dependencies, or a materially richer proof decomposition, prefer a restructuring recommendation over repeated local proof polishing. You may override a `STRUCTURAL` objection when it is not convincing relative to the child statements and the rest of the verification record, but do so deliberately.
- When you recommend restructuring, be concrete: identify the missing intermediate claim(s), dependency changes, or paper-facing substeps that should be added, rather than just saying "needs more structure."
- If orphan candidates remain, do NOT advance. The worker must either remove them or add the missing downstream dependency/citation first.
- Do NOT advance while `open_blockers` is non-empty.
- Do NOT advance while any soundness-eligible theorem-stating node remains unresolved.

Do NOT advance unless ALL main results with complete proofs in the paper are represented and the decomposition genuinely covers the paper's argument.

MANDATORY:
1. Write the JSON to `{raw_output_path}`.
2. Run `python3 {check_script} reviewer-decision {raw_output_path} --phase {phase}`.
3. If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
