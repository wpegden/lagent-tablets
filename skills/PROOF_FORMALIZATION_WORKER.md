# Proof-Formalization Worker Skill

This skill applies only in the `proof_formalization` phase.

The emitted prompt is authoritative.

## Core Job

- Prove one assigned Lean node at a time.
- Do not pick your own node.
- Run the exact checker command from the prompt before writing the handoff.

## Loogle First

Use the local Loogle server before inventing helper statements or imports.

```bash
curl -s "http://127.0.0.1:8088/json?q=Real.exp_neg" | python3 -m json.tool
```

Prefer Mathlib lemmas and definitions over project-local wrappers whenever possible.

## Imports

- Never use `import Mathlib`.
- Use specific submodules only.

## Easy vs Hard

- In `easy` mode, stay within the frozen easy-mode scope from the prompt.
- In `hard` mode, you may add imports/helpers only as permitted by the prompt.

## Lean Workflow

- Start with automation (`simp`, `norm_num`, `ring`, `omega`, `exact?`, `apply?`).
- Break complex goals into named intermediate claims.
- Use `calc` blocks for algebraic or order-sensitive derivations.

## Common Failure Modes

- editing the frozen declaration line
- changing imports when the prompt forbids it
- creating helpers without matching `.tex` files in hard mode
- skipping the deterministic checker before handoff
