# Prompt Action Catalog

This directory records, for each generated prompt in `prompt_catalog/`, the actions that the prompt appears to make available to a cold agent reading it with no extra context.

These files are intentionally about *apparent affordances*, not a second-pass semantic correction. They are meant to help audit whether the prompts are steering agents toward the intended behavior.

Regenerate with:

```bash
python3 scripts/generate_prompt_action_catalog.py
```

Files:
- [correspondence_basic.md](correspondence_basic.md)
- [correspondence_full_context_multiple_changed_nodes.md](correspondence_full_context_multiple_changed_nodes.md)
- [correspondence_single_changed_node.md](correspondence_single_changed_node.md)
- [nl_proof_batch.md](nl_proof_batch.md)
- [node_soundness_leaf.md](node_soundness_leaf.md)
- [node_soundness_with_children_and_previous_issues.md](node_soundness_with_children_and_previous_issues.md)
- [proof_reviewer_cleanup.md](proof_reviewer_cleanup.md)
- [proof_reviewer_standard.md](proof_reviewer_standard.md)
- [proof_worker_cleanup.md](proof_worker_cleanup.md)
- [proof_worker_easy_local.md](proof_worker_easy_local.md)
- [proof_worker_hard_coarse_restructure.md](proof_worker_hard_coarse_restructure.md)
- [proof_worker_hard_local.md](proof_worker_hard_local.md)
- [proof_worker_hard_restructure.md](proof_worker_hard_restructure.md)
- [theorem_reviewer_invalid_with_reset_options.md](theorem_reviewer_invalid_with_reset_options.md)
- [theorem_reviewer_target_resolved.md](theorem_reviewer_target_resolved.md)
- [theorem_reviewer_with_main_result_target_issues.md](theorem_reviewer_with_main_result_target_issues.md)
- [theorem_reviewer_with_unsupported_nodes.md](theorem_reviewer_with_unsupported_nodes.md)
- [theorem_worker_broad_initial_empty.md](theorem_worker_broad_initial_empty.md)
- [theorem_worker_broad_with_blockers_and_retry.md](theorem_worker_broad_with_blockers_and_retry.md)
- [theorem_worker_target_repair.md](theorem_worker_target_repair.md)
- [theorem_worker_target_restructure.md](theorem_worker_target_restructure.md)
- [verification_wrapper_compat.md](verification_wrapper_compat.md)
