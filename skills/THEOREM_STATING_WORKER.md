# Theorem-Stating Worker Skill

This skill applies only in the `theorem_stating` phase.

The emitted prompt is authoritative. In particular:

- If there is a `CURRENT SOUNDNESS TARGET`, follow the target mode in the prompt.
- In `repair` mode, stay inside the single allowed `.tex` file.
- In `restructure` mode, keep all edits inside the target's prerequisite slice.
- Only when there is no current target should you think in terms of broader DAG construction.

## Goals

- Build a paper-faithful tablet DAG.
- Prefer real intermediate steps that will make later Lean formalization easier.
- Do not invent gratuitous helpers.

## Definitions

- Prefer existing Mathlib definitions over project wrappers.
- Every real definition should be its own node with matching `.lean` and `.tex`.
- Do not let theorem/lemma/corollary nodes double as hidden definitions.
- `Tablet/Preamble.lean` contains imports only.

## Imports

- Never use `import Mathlib`.
- Use specific submodules only.

## NL Proof Standard

- Rigorous, not sketch-level.
- At least as detailed as the relevant part of the paper.
- Cite only imported child nodes with `\noderef{name}`.

## Checks

Run the exact checker command given in the prompt before writing the handoff.
