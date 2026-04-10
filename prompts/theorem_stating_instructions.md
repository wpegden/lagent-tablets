--- INSTRUCTIONS ---

PHASE: theorem_stating
YOUR GOAL: Build and refine the proof tablet until it gives a complete DAG of Lean 4 declarations covering the paper's provable results.

If the tablet is still missing major parts, create the needed nodes and decomposition. If the prompt includes a `CURRENT SOUNDNESS TARGET`, keep the cycle centered on that target and follow the target-mode rules below. If there is no current target, work in deterministic deepest-first DAG order and keep the cycle focused on one coherent unresolved slice rather than broad opportunistic rewrites across unrelated parts of the tablet.

Read the skill file at `{skill_path}` for Loogle usage and Lean tips.

SCOPE:
- ALL main theorems and lemmas with complete proofs in the paper must be included as nodes.
- Auxiliary discussions, remarks, examples, and sections with only proof sketches (not full proofs) do not need to be formalized unless doing so helps formalize the main results.
- If the paper proves it rigorously, it goes in the tablet. If the paper only sketches it or mentions it in passing, include it only if it supports the main results.

DEFINITIONS:
- Always prefer existing Mathlib definitions over creating new ones. Use Loogle to search for standard concepts before defining anything yourself.
- If the paper uses a standard mathematical object (graphs, probability measures, filters, etc.), find and use the Mathlib version. Only create a new definition if Mathlib genuinely doesn't have one.
- All definitions must be concrete — no `opaque`, no `axiom`, no `sorry` in definitions.

DECOMPOSITION STRATEGY:
- Start with the paper's main theorem(s) as top-level nodes
- Work backwards: what intermediate results does each theorem need?
- Each node should be a single, self-contained mathematical statement
- Aim for 5-15 nodes depending on the paper's complexity
- Leaf nodes should be provable directly from Mathlib or basic arguments
- Think about what order you would prove these in -- the node DAG should reflect this

For each node, create two files:

1. **`Tablet/{{name}}.lean`** -- The Lean declaration with `sorry`:
   ```lean
   import Tablet.Preamble
   -- import Tablet.{{dependency}} for nodes this result depends on

   -- [TABLET NODE: {{name}}]
   -- Do not rename or remove the declaration below.

   theorem {{name}} (args...) : statement := sorry
   ```

2. **`Tablet/{{name}}.tex`** -- The NL statement AND a complete NL proof:
   ```latex
   \begin{{theorem}}[Title]
   NL statement matching the Lean declaration.
   \end{{theorem}}

   \begin{{proof}}
   By \noderef{{dependency1}} and \noderef{{dependency2}}, ...
   (Rigorous NL proof from the NL statements of imported nodes.)
   \end{{proof}}
   ```

3. **`Tablet/Preamble.lean`** -- ONLY import statements. No definitions allowed here:
   ```lean
   import Mathlib.Analysis.SpecialFunctions.Log.Basic
   import Mathlib.Topology.Order.Basic
   -- NEVER write `import Mathlib`
   -- NEVER put `def` or `noncomputable def` here
   ```
   Use Loogle at `http://127.0.0.1:8088/json?q=...` to find which module contains each lemma you need.

   Paper-facing imported Mathlib definitions or notation may be documented in `Tablet/Preamble.tex` using `definition` or `proposition` environments, but `Preamble.lean` itself must still contain only imports. Every project-specific definition you introduce must be its own node with a `.lean` + `.tex` pair. The `.tex` for a definition node should state in natural language what the definition means.

IMPORTANT RULES:
- If the prompt includes a `CURRENT OPEN REJECTIONS` section, theorem-stating is NOT complete yet. Prioritize resolving every listed correspondence and paper-faithfulness rejection before treating the tablet as finished.
- Theorem-stating continues until the open-rejection list is empty.
- If the prompt includes a `CURRENT SOUNDNESS TARGET` section, do not switch to a different soundness target on your own.
- When there is a `CURRENT SOUNDNESS TARGET`, follow the target mode shown in the prompt.
- In target mode `repair`, you are hard-locked to the target node's `.tex` file only. If you think the proof needs the DAG to be enriched with additional dependencies or meaningful intermediate nodes first, do NOT do that inside the cycle; instead write the handoff with status `STUCK` and explain the restructure needed.
- In target mode `restructure`, every node you create, delete, or edit must end up in that target's authorized impact region by the end of the cycle. That region includes the target itself, its prerequisites, and downstream consumers that need interface propagation because the target changed. Do not touch unrelated nodes.
- If there is no `CURRENT SOUNDNESS TARGET`, prefer the deepest unresolved theorem/helper slice in DAG order. Avoid editing unrelated nodes just because they also look improvable; keep the cycle local unless the prompt explicitly asks for a broader cleanup.
- If the prompt includes an `ORPHAN NODE ACTIONS` section, carry out those reviewer decisions before treating the tablet structure as complete. A non-main orphan should either be removed or given a real downstream dependency/citation.
- If your currently authorized theorem-stating scope lets you edit a node's `.lean` file and you can completely prove that node immediately from its current children, you may do that now instead of only improving the NL proof. If you take this Lean shortcut, run `python3 {check_script} node <node_name> {repo_path}` and only rely on it if that exact deterministic check passes.
- In the coarse early theorem-stating stage only (that is, when there is no `CURRENT SOUNDNESS TARGET`), if you conclude that the source paper appears to contain a genuine fundamental gap that cannot honestly be repaired by local DAG restructuring, you may stop with status `CRISIS`. Use this only for a serious paper-level issue, not for ordinary local proof trouble.
- Every `.lean` must have a matching `.tex` with NL statement AND NL proof
- Imports between nodes define the DAG: if node B uses node A, then B imports A
- The `-- [TABLET NODE: name]` marker line is MANDATORY in every node .lean file
- NEVER use `import Mathlib` -- only specific submodule imports
- `sorry` is allowed ONLY as a proof body for theorems/lemmas. NEVER use `sorry` in definitions. All definitions must be concrete — no `opaque`, no axioms, no `sorry`'d definitions. If you need a mathematical object, define it using Mathlib types or build it from scratch.
- `sorry` is expected for theorem proofs in this phase -- you are stating theorems, not proving them
- Do not use theorem/lemma/corollary nodes as disguised definitions. If the paper is introducing a concept, model that as an actual definition node (or, for imported Mathlib concepts, in `Preamble.tex`) rather than smuggling it into a result statement.
- The NL proof in each .tex must be rigorous, not a sketch or placeholder. Proofs here should be at least as detailed as those in the paper and generally moreso. In this theorem stating phase, it is natural to copy/paste the appropriate proofs from the paper into the node .tex files, carefully check them, and augment them with details.
- In theorem_stating, paper-faithful DAG enrichment is generally good when it reflects real paper structure and will make later Lean work more tractable. Do not invent gratuitous helpers, but do request restructure when a richer intermediate-step decomposition is genuinely needed.
- Use `\noderef{{name}}` to cite other nodes in NL proofs
- Run `python3 {check_script} tablet {repo_path}` to verify the tablet structure and build state (sorry warnings are expected)
- The supervisor auto-generates `Tablet.lean` -- do NOT create or edit it

NODE NAMING: use snake_case names that describe the mathematical content.
Example: `expected_isolated_vertices`, `threshold_limit`, `first_moment_bound`

DIFFICULTY CLASSIFICATION:
For each node, classify it as "easy" or "hard":
- **easy**: A leaf node or straightforward consequence of its children that can likely be proved in Lean directly from the existing imports with no structural changes.
- **hard**: A challenging formalization that may require creating additional helper lemmas, refactoring imports, or non-trivial proof engineering.

Include your classification in the handoff file as `difficulty_hints`.

STRUCTURAL ROLE CLASSIFICATION:
For each genuinely new theorem-stating node, classify it as either:
- `paper_main_result`: only for an actual top-level paper theorem/result that should never be treated as an orphanable intermediate
- `paper_intermediate`: everything else

Do NOT guess from the filename. Use `paper_main_result` sparingly and only when the node is truly a main paper result.
Include this in the handoff file as `kind_hints`.

WHEN YOU HAVE FINISHED THE CYCLE'S TABLET EDITS: Write the raw handoff JSON to `{raw_output_path}` listing every node you created this cycle:
{{
  "summary": "Created N tablet nodes covering the paper's main results and key lemmas",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT | CRISIS",
  "new_nodes": ["node1", "node2", "...every node you created..."],
  "difficulty_hints": {{"node1": "easy", "node2": "hard", "..."}},
  "kind_hints": {{"node1": "paper_intermediate", "node2": "paper_main_result", "..."}}
}}
Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase theorem_stating --repo {repo_path}
If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
Do NOT write the raw handoff file until you have finished the cycle's intended tablet edits and verified them with the checker.
