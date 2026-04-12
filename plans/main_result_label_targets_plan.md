# Main-Result Label Target Plan

## Why this supersedes recent changes

Two recent semantics changes are being replaced rather than extended:

- env-based human trust gating over all paper-anchored `theorem` / `lemma` / `corollary` nodes
- env-based orphan protection for leaf `theorem` / `lemma` / `corollary` nodes

Those are too coarse. Human review significance and DAG necessity should be driven by an explicit selected target set, not by node environment alone.

The structured provenance model stays. The `helper` environment stays. Coarse-package protection stays.

## New model

- `workflow.main_result_labels` defines the paper targets that matter for human review.
- A target label is covered by one or more non-`helper` nodes whose `paper_provenance.tex_label` matches it.
- `helper` nodes may carry provenance for context, but they may not count as target-covering nodes.
- Human review gates on the selected target labels only, not on all paper-anchored nodes.
- Non-target nodes are justified only if they lie in the dependency closure of at least one target-covering node.

## Important operational nuance

Support-closure pruning is suspended until every configured target label has at least one non-`helper` covering node.

Reason:
- during early theorem-stating, the worker may still be assembling the target layer
- if we prune too early, we risk deleting preparatory structure before the target nodes exist

Once every target label is covered, unsupported nodes become real structural debt.

## Implementation steps

1. Add `workflow.main_result_labels` to config/setup.
- setup accepts an explicit list
- if omitted, setup infers all labeled paper `theorem` / `corollary` statements and writes the resolved list into config

2. Add paper-label parsing and main-result coverage helpers.
- infer default target labels from the paper
- compute coverage by label
- forbid `helper` as a target-covering env

3. Replace the human-review gate.
- snapshot trusted state per target label, not per node env bucket
- trigger renewed human review only when target-label coverage/fingerprint changes

4. Replace env-based orphan logic with support-closure logic.
- target-covering nodes are the protected roots
- after all targets are covered, any node outside their dependency closure is unsupported
- theorem-stating reviewer guidance should use this unsupported-node set

5. Update prompts/docs.
- theorem-stating should talk about selected target labels, not “all main theorems/lemmas”
- proof-formalization should still respect the protected coarse package and the target-label gate

6. Update tests.
- setup inference
- helper exclusion
- multi-node coverage for one label
- support-closure behavior
- human gate keyed by labels, not env

## Status

Implemented.

Key outcomes:
- setup now persists a resolved `workflow.main_result_labels` list
- human review snapshots are keyed by configured labels, not by env bucket
- support-closure enforcement replaced env-based orphan protection
- theorem-stating and proof-formalization prompts now describe the selected target set rather than “all main theorems/lemmas”
