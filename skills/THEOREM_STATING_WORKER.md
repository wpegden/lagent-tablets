# Theorem-Stating Worker Skill

This skill applies only in the `theorem_stating` phase.

The emitted prompt is authoritative. In particular:

- If there is a `CURRENT SOUNDNESS TARGET`, follow the target mode in the prompt.
- In `repair` mode, stay inside the single allowed `.tex` file.
- In `restructure` mode, keep all edits inside the target's authorized impact region.
  That region includes the target itself, its prerequisites, and downstream consumers that need interface propagation because the target changed.
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

## Loogle First

Use the local Loogle server before inventing project definitions or guessing import paths.

```bash
curl --max-time 5 -s "http://127.0.0.1:8088/json?q=Submodule.span" | python3 -m json.tool
curl --max-time 5 -s "http://127.0.0.1:8088/json?q=Nat.choose" | python3 -m json.tool
```

Search one concept at a time. Do not combine unrelated names into one query.
If Loogle returns a type-mismatch or application-shape error, treat that as a bad query shape and retry with a simpler single-concept query.

## Imports

- Never use `import Mathlib`.
- Use specific submodules only.

## NL Proof Standard

- Rigorous, not sketch-level.
- At least as detailed as the relevant part of the paper.
- Cite only imported child nodes with `\noderef{name}`.

## Checks

Run the exact checker command given in the prompt before writing the handoff.
Wait for that checker command to finish before doing anything else, and only then write the done marker.

## Lean Build Hygiene

- Prefer `lake env lean <scratch-file>` for scratch declaration and import probes.
- Use the provided deterministic `check.py ...` command for the actual acceptance gate.
- You normally do not need `lake update` or `lake exe cache get` during a worker cycle, because setup already handles dependency refresh and cache provisioning.

## Scratch Work

- Use the repo-local scratch directory named in the prompt for Lean experiments.
- Do not rely on `/tmp` scratch files for package-aware `lake env lean` checks.
- The setup script seeds a trivial `example.lean` there; copy or edit that pattern instead of starting from a broken scratch file.
