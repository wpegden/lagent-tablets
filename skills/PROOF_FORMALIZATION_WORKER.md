# Proof-Formalization Worker Skill

This skill applies only in the `proof_formalization` phase.

The emitted prompt is authoritative.

## Core Job

- Prove one assigned Lean node at a time.
- Do not pick your own node.
- Run the exact checker command from the prompt before writing the handoff.
- Wait for that checker command to finish before doing anything else, and only then write the done marker.

## Loogle First

Use the local Loogle server before inventing helper statements or imports.

```bash
curl --max-time 5 -s "http://127.0.0.1:8088/json?q=Real.exp_neg" | python3 -m json.tool
curl --max-time 5 -s "http://127.0.0.1:8088/json?q=Submodule.span" | python3 -m json.tool
```

Search one concept at a time. Do not combine several unrelated identifiers into one query.
If Loogle returns a type-mismatch or application-shape error, treat that as a bad query shape and retry with a simpler single-concept query.

Prefer Mathlib lemmas and definitions over project-local wrappers whenever possible.

## Imports

- Never use `import Mathlib`.
- Use specific submodules only.

## Easy vs Hard

- In `easy` mode, stay within the frozen easy-mode scope from the prompt.
- In `hard` mode, you may add imports/helpers only as permitted by the prompt.
- If you create a new structural node in proof_formalization, `helper` and `definition` are the normal `.tex` environments.
- New paper-anchored `theorem`/`lemma`/`corollary` nodes are unusual but allowed when they are genuinely needed; if you create one, follow the full node spec and include structured `paper_provenance_hints` in the handoff.
- Ordinary proof_formalization must not mutate the accepted coarse package. Changing accepted coarse-node statements, `.tex`, or coarse-to-coarse structure requires reviewer-authorized `proof_edit_mode: "coarse_restructure"`.

## Lean Workflow

- Start with automation (`simp`, `norm_num`, `ring`, `omega`, `exact?`, `apply?`).
- Break complex goals into named intermediate claims.
- Use `calc` blocks for algebraic or order-sensitive derivations.

## Lean Build Hygiene

- Prefer `lake env lean <scratch-file>` for scratch declaration and import probes.
- Use the provided deterministic `check.py ...` command for the actual acceptance gate.
- You normally do not need `lake update` or `lake exe cache get` during a worker cycle, because setup already handles dependency refresh and cache provisioning.

## Common Failure Modes

- editing the frozen declaration line
- changing imports when the prompt forbids it
- creating helpers without matching `.tex` files in hard mode
- skipping the deterministic checker before handoff
