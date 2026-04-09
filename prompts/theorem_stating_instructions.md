--- INSTRUCTIONS ---

PHASE: theorem_stating
YOUR GOAL: Create the COMPLETE proof tablet in this cycle -- a DAG of Lean 4 declarations that decompose ALL of the paper's results into provable nodes.

You must create ALL nodes in a single cycle. Do not stop after creating one node. Read the entire paper, plan the full decomposition, then create every node (.lean + .tex pair) before writing the raw handoff file `{raw_output_path}`.

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

   Every definition you need must be its own node with a `.lean` + `.tex` pair, just like theorems and lemmas. The `.tex` for a definition node should state in natural language what the definition means.

IMPORTANT RULES:
- If the prompt includes a `CURRENT OPEN REJECTIONS` section, theorem-stating is NOT complete yet. Prioritize resolving every listed correspondence and paper-faithfulness rejection before treating the tablet as finished.
- Theorem-stating continues until the open-rejection list is empty.
- If the prompt includes an `ORPHAN NODE ACTIONS` section, carry out those reviewer decisions before treating the tablet structure as complete. A non-main orphan should either be removed or given a real downstream dependency/citation.
- Every `.lean` must have a matching `.tex` with NL statement AND NL proof
- Imports between nodes define the DAG: if node B uses node A, then B imports A
- The `-- [TABLET NODE: name]` marker line is MANDATORY in every node .lean file
- NEVER use `import Mathlib` -- only specific submodule imports
- `sorry` is allowed ONLY as a proof body for theorems/lemmas. NEVER use `sorry` in definitions. All definitions must be concrete — no `opaque`, no axioms, no `sorry`'d definitions. If you need a mathematical object, define it using Mathlib types or build it from scratch.
- `sorry` is expected for theorem proofs in this phase -- you are stating theorems, not proving them
- The NL proof in each .tex must be rigorous, not a sketch or placeholder. Proofs here should be at least as detailed as those in the paper and generally moreso. In this theorem stating phase, it is natural to copy/paste the appropriate proofs from the paper into the node .tex files, carefully check them, and augment them with details.
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

WHEN ALL NODES ARE CREATED: Write the raw handoff JSON to `{raw_output_path}` listing every node you created:
{{
  "summary": "Created N tablet nodes covering the paper's main results and key lemmas",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": ["node1", "node2", "...every node you created..."],
  "difficulty_hints": {{"node1": "easy", "node2": "hard", "..."}}
}}
Then run:
  python3 {check_script} worker-handoff {raw_output_path} --phase theorem_stating --repo {repo_path}
If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
Do NOT write the raw handoff file until you have created ALL nodes. Do not create one node and stop — create the entire tablet structure first, verify it with the checker, then write the handoff.
