# Prompt Catalog

This folder is fully generated from the live prompt builders in `lagent_tablets/prompts.py`.

Regenerate it with:

```bash
python3 scripts/generate_prompt_catalog.py
```

Conventions:
- Absolute example-repo paths are normalized to `/EXAMPLE_PROJECT`.
- Bracketed text such as `[worker terminal output excerpt from the prior burst]` marks dynamic runtime text whose exact contents depend on prior agent or human activity.
- Each Markdown file corresponds to one branch-representative situation, not an arbitrary sample.

Scenarios:
- [proof_worker_easy_local.md](proof_worker_easy_local.md): Proof-formalization worker on an easy local node with prior invalid feedback and targeted paper focus.
- [proof_worker_hard_local.md](proof_worker_hard_local.md): Proof-formalization worker on a hard local node with reviewer guidance and prior verification rejection.
- [proof_worker_hard_restructure.md](proof_worker_hard_restructure.md): Proof-formalization worker in reviewer-authorized restructure mode for the active target's impact region.
- [proof_worker_hard_coarse_restructure.md](proof_worker_hard_coarse_restructure.md): Proof-formalization worker in reviewer-authorized coarse-restructure mode with accepted coarse-package mutation allowed.
- [proof_worker_cleanup.md](proof_worker_cleanup.md): Proof-complete style cleanup worker prompt.
- [theorem_worker_broad_initial_empty.md](theorem_worker_broad_initial_empty.md): Theorem-stating worker at cycle start with an empty tablet.
- [theorem_worker_broad_with_blockers_and_retry.md](theorem_worker_broad_with_blockers_and_retry.md): Theorem-stating worker in broad mode with reviewer guidance, open blockers, support actions, and a preserved invalid retry.
- [theorem_worker_target_repair.md](theorem_worker_target_repair.md): Theorem-stating worker locked to a current soundness target in repair mode.
- [theorem_worker_target_restructure.md](theorem_worker_target_restructure.md): Theorem-stating worker on a current soundness target with reviewer-authorized restructure and scoped checks.
- [proof_reviewer_standard.md](proof_reviewer_standard.md): Proof-formalization reviewer with worker output, invalid history, disagreement in verification, and unsupported-node warning.
- [proof_reviewer_cleanup.md](proof_reviewer_cleanup.md): Proof-complete style cleanup reviewer prompt.
- [theorem_reviewer_with_unsupported_nodes.md](theorem_reviewer_with_unsupported_nodes.md): Theorem-stating reviewer with current verification results, a held soundness target, and unsupported-node decisions to make.
- [theorem_reviewer_with_main_result_target_issues.md](theorem_reviewer_with_main_result_target_issues.md): Theorem-stating reviewer prompt when configured main-result targets are still missing or helper-only.
- [theorem_reviewer_invalid_with_reset_options.md](theorem_reviewer_invalid_with_reset_options.md): Theorem-stating reviewer on an invalid attempt with a worker crisis report and supervisor-approved reset checkpoints.
- [theorem_reviewer_target_resolved.md](theorem_reviewer_target_resolved.md): Theorem-stating reviewer after the current soundness target has already passed this cycle.
- [correspondence_basic.md](correspondence_basic.md): Basic correspondence / paper-faithfulness verification for one node.
- [correspondence_single_changed_node.md](correspondence_single_changed_node.md): Correspondence verification with old-vs-new context for one node that reopened the frontier.
- [correspondence_full_context_multiple_changed_nodes.md](correspondence_full_context_multiple_changed_nodes.md): Correspondence verification including preamble items, provenance excerpts, previous results, and multiple changed nodes.
- [nl_proof_batch.md](nl_proof_batch.md): Batch NL-proof soundness verification prompt.
- [node_soundness_with_children_and_previous_issues.md](node_soundness_with_children_and_previous_issues.md): Single-node soundness prompt for a node with children, paper context, and prior issues.
- [node_soundness_leaf.md](node_soundness_leaf.md): Single-node soundness prompt for a leaf node.
- [verification_wrapper_compat.md](verification_wrapper_compat.md): Backward-compatible combined verification wrapper prompt.
