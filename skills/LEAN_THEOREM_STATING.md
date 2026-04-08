# Lean 4 Theorem-Stating Skill File

You are creating the proof tablet structure for a mathematical paper. Your job is to decompose the paper into a DAG of Lean declarations with rigorous NL proofs.

## Your Task in One Sentence

Read the paper, create ALL nodes (.lean + .tex pairs) covering every main result with a complete proof, set up the Preamble with concrete definitions, verify with `lake build Tablet`, then write `worker_handoff.json`.

---

## 1. Loogle: Find Definitions Before You Write

A local Loogle server is running. Use it to find existing Mathlib definitions and lemmas.

```bash
curl -s "http://127.0.0.1:8088/json?q=SimpleGraph" | python3 -m json.tool
curl -s "http://127.0.0.1:8088/json?q=MeasureTheory.Measure" | python3 -m json.tool
```

**Always prefer Mathlib definitions.** If Mathlib has `SimpleGraph`, `Filter.Tendsto`, `MeasureTheory.Measure`, etc., use them. Only create new definitions if Mathlib genuinely doesn't have what you need.

---

## 2. Definitions Must Be Their Own Nodes

Every definition gets its own node (`Tablet/{name}.lean` + `Tablet/{name}.tex`), just like theorems and lemmas. `Tablet/Preamble.lean` contains ONLY import statements — no definitions.

The `.tex` for a definition node should state in NL what the definition means:
```latex
\begin{definition}[Expected isolated vertex count]
For the random graph $G(n,p)$, define $X_n$ to be the number of isolated vertices.
\end{definition}
```

Definitions must be concrete — no `sorry`, no `opaque`, no `axiom`:
```lean
-- GOOD: concrete definition using Mathlib types
noncomputable def expected_isolated (n : ℕ) (p : unitInterval) : ℝ :=
  ∫ G, (∑ v : Fin n, if G.degree v = 0 then 1 else 0) ∂(SimpleGraph.binomialRandom (Fin n) p)

-- BAD: sorry'd definition
def expected_isolated (n : ℕ) (p : ℝ) : ℝ := sorry
```

---

## 3. Imports: Be Specific

**NEVER use `import Mathlib`.** Only specific submodules:

```lean
import Mathlib.Combinatorics.SimpleGraph.Basic          -- good
import Mathlib.MeasureTheory.Measure.ProbabilityMeasure -- good
import Mathlib                                          -- FORBIDDEN
```

Use the `module` field from Loogle results to find the right import.

---

## 4. Node Structure

Each node needs two files:

**`Tablet/{name}.lean`:**
```lean
import Tablet.Preamble  -- or import Tablet.{dependency}

-- [TABLET NODE: {name}]
-- Do not rename or remove the declaration below.

theorem {name} (args...) : statement := sorry
```

**`Tablet/{name}.tex`:**
```latex
\begin{lemma}[Title]
NL statement matching the Lean declaration exactly.
\end{lemma}

\begin{proof}
Rigorous NL proof from the NL statements of child nodes.
By \noderef{child1}, ... By \noderef{child2}, ...
\end{proof}
```

---

## 5. NL Proofs Must Be Rigorous

The NL proof in each .tex file is checked by a verification agent. It must be:
- At least as detailed as the paper's proof, and generally moreso
- A complete logical argument from the NL statements of imported nodes
- Free of hand-waving ("obvious", "trivial", "by standard arguments")

It is natural to copy proofs from the paper, then augment with details.

---

## 6. The Full Workflow

1. Read the paper completely
2. Plan the decomposition: which results, what order, what dependencies
3. Use Loogle to find Mathlib definitions for all key concepts
4. Write `Tablet/Preamble.lean` with concrete definitions and specific imports
5. Create ALL node .lean files (with sorry proofs)
6. Create ALL node .tex files (with rigorous NL proofs)
7. Run `lake build Tablet` — sorry warnings are expected, errors are not
8. Fix any compilation errors
9. Write `worker_handoff.json` listing every node

Do NOT write the handoff until ALL nodes exist and `lake build` passes.

The supervisor auto-generates `Tablet.lean` — do NOT create or edit it.
