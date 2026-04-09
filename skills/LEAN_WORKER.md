# Lean 4 Worker Skill File

You are working in a Lean 4 proof tablet project. Check the repo's `lean-toolchain` for the exact Lean version.

---

## 1. Loogle: Find Definitions and Lemmas Before You Write

A local Loogle server is running. Use it to find existing Mathlib definitions and lemmas before writing anything from scratch. Always prefer Mathlib's definitions over creating new ones — search for standard concepts (graphs, probability, filters, etc.) before defining them yourself.

**Search by name:**
```bash
curl -s "http://127.0.0.1:8088/json?q=Real.exp_neg" | python3 -m json.tool
```

**Search by type signature:**
```bash
curl -s "http://127.0.0.1:8088/json?q=(_ -> Real) -> Filter _ -> Prop" | python3 -m json.tool
```

**Response format:**
```json
{
  "count": 1,
  "hits": [
    {
      "name": "Real.exp_neg",
      "type": "(x : ℝ) : Real.exp (-x) = (Real.exp x)⁻¹",
      "module": "Mathlib.Analysis.SpecialFunctions.ExpDeriv",
      "doc": null
    }
  ]
}
```

Use the `name` field in your proof. The `module` field tells you which import you need.

**When to search:**
- Before attempting any nontrivial lemma
- When you get "unknown identifier" errors
- When `simp` or `exact?` fails -- search for the rewrite lemma you need
- URL-encode special characters: `%E2%84%9D` for `ℝ`, `+` for space

---

## 2. Imports: Be Specific

**NEVER use `import Mathlib`.** Import only the specific modules you need. Use Loogle to find which module contains the lemma (the `module` field in results).

```lean
import Mathlib.Analysis.SpecialFunctions.Log.Basic  -- good
import Mathlib  -- FORBIDDEN, causes 20+ minute rebuilds
```

---

## 3. Proof Strategies

### Start with automation
- `simp`, `norm_num`, `ring`, `omega` -- algebraic/numeric goals
- `exact?`, `apply?` -- let Lean search for the right lemma
- `gcongr` -- monotonicity/congruence goals
- `positivity` -- `0 < ...` or `0 ≤ ...` goals

### Break complex goals apart
```lean
theorem foo : ... := by
  have h1 : intermediate_claim := by ...
  have h2 : another_claim := by ...
  exact final_step h1 h2
```

### Filter and topology goals
- `filter_upwards [h1, h2] with x hx1 hx2` -- the workhorse for eventually-goals
- `Tendsto.comp` -- compose tendsto results
- `Tendsto.add`, `Tendsto.mul`, `Tendsto.div` -- arithmetic on limits

### Calc blocks
```lean
calc expression
    = step1 := by ring
  _ ≤ step2 := by gcongr; exact ...
  _ = step3 := by ...
```

---

## 4. Workflow by Phase

### During theorem_stating
You are creating the tablet structure. For each node:
1. Write `Tablet/{name}.lean` with the declaration + sorry
2. Write `Tablet/{name}.tex` with rigorous NL statement and proof
3. Set up imports between nodes to define the DAG
4. Set up `Tablet/Preamble.lean` with specific Mathlib imports
5. Run `python3 .agent-supervisor/scripts/check.py tablet .` -- sorry warnings are expected, errors are not
6. Write the raw handoff file to the path given in the prompt, run the same checker on that raw JSON, then write the completion marker from the prompt
7. The supervisor auto-generates `Tablet.lean` -- do NOT create or edit it

### During proof_formalization
You are proving one node at a time:
1. Read `Tablet/{node}.lean` (declaration) and `Tablet/{node}.tex` (NL proof)
2. Search Loogle for relevant Mathlib lemmas
3. Write the proof body (everything after `:=`)
4. Run `python3 .agent-supervisor/scripts/check.py node {node} .` and iterate until it passes
5. Write the raw handoff file to the path given in the prompt, run the same checker on that raw JSON, then write the completion marker from the prompt

### check.py node passing output
```
=== Checking node: {name} ===
  Declaration: OK
  Imports: OK
  Keywords: OK
  Compiles: OK
  Status: CLOSED (all checks pass)
=== Done ===
```

### Creating helper lemmas
If you need an intermediate result, create a new node:
- `Tablet/{helper_name}.lean` -- with `-- [TABLET NODE: helper_name]` marker
- `Tablet/{helper_name}.tex` -- with NL statement and proof
- Import it in your active node
- List it in the raw handoff JSON under `new_nodes`

---

## 5. Common Pitfalls

1. **Modifying the frozen declaration line.** The supervisor hashes it. Any change = rejection.
2. **Editing other nodes' `.lean` files.** Only your active node is writable.
3. **Forgetting the `.tex` file** for new helpers. Every `.lean` needs a paired `.tex`.
4. **Missing the `-- [TABLET NODE: name]` marker** in new node files.
5. **Using `import Mathlib`** instead of specific imports. Causes 20+ minute rebuilds and will be rejected.
6. **Not running the shared checker** before writing the handoff.
7. **Spinning without progress.** If stuck after several attempts, write the handoff with `"status": "STUCK"` and explain what you tried. The reviewer will guide you.
