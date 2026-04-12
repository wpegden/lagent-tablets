# Proof-Formalization Reviewer Skill

This skill applies only in the `proof_formalization` phase.

The emitted prompt is authoritative.

## What You Are Evaluating

- real proof progress on one assigned node
- whether the current node should stay `easy` or be elevated to `hard`
- what node choice is most likely to change later plans

## Signs of Progress

- fewer `sorry`s
- narrower remaining goals
- correct Mathlib lemmas identified
- helper nodes that materially reduce the main proof burden
- a newly introduced paper-anchored statement only when it is genuinely needed and comes with clear paper provenance

## Signs of Churn

- repeated invalid edits to frozen declarations/imports
- repeated unproductive search with no proof progress
- helpers that restate the difficulty without reducing it
- new paper-anchored statements that should really have been theorem-stating work or that try to mutate the accepted coarse package without `coarse_restructure`

## Node Choice

Prefer nodes whose completion or failure is likely to inform later planning.
That usually means hard nodes or low-level blocking nodes.

## NL Verification

When verification agents disagree, weigh the technical reasoning, not the count alone.
You are the final arbiter, but make overrides explicit and concrete.

## Coarse Package Protection

Treat the accepted coarse theorem-stating package as protected. Ordinary proof-formalization may prove within it and add non-coarse helpers beneath it, but changing accepted coarse-node statements, `.tex`, or coarse-to-coarse structure requires explicit `proof_edit_mode: "coarse_restructure"` authorization.
