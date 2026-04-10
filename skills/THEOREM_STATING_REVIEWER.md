# Theorem-Stating Reviewer Skill

This skill applies only in the `theorem_stating` phase.

The emitted prompt is authoritative.

## What You Are Optimizing

- A paper-faithful DAG of statements.
- Rigorous NL proofs from imported child statements.
- A structure that will make later Lean formalization tractable.

## Guidance Principles

- Keep guidance local to the current target or the deepest unresolved slice.
- Treat `STRUCTURAL` objections seriously.
- Prefer richer DAG structure when it reflects real paper structure.
- Do not recommend broad opportunistic cleanup across unrelated nodes.

## Target Modes

- `repair`: the worker is hard-locked to the target `.tex` proof.
- `restructure`: you are explicitly authorizing broader target-local DAG changes.

If prerequisite structure is missing for the same target, authorize `restructure` rather than informally asking for broader edits under `repair`.

## Correspondence / Faithfulness

- Open correspondence and paper-faithfulness rejections must be resolved before phase advance.
- Every blocker you rely on should be represented in the structured reviewer output.

## Next Target Choice

When theorem-stating is free to move on, prefer the deepest unresolved slice rather than jumping to top-level statements.
