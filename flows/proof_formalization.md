# Proof Formalization

Applies when `state.phase == "proof_formalization"`.

```text
select active node
  |
  v
route to easy or hard worker
  |
  v
worker burst
  |
  v
deterministic proof-worker validation
  |
  +--> INVALID
  |      |
  |      v
  |   stay on same committed cycle state
  |   easy-mode attempt counter increments
  |   escalate to reviewer only after repeated INVALIDs
  |
  +--> NO_PROGRESS
  |      |
  |      v
  |   reviewer always sees it
  |   easy-mode attempt counter increments
  |
  +--> PROGRESS
         |
         v
      commit worker checkpoint
         |
         v
      NL verification on changed/new relevant nodes
         |
         +--> verification APPROVE
         |      |
         |      v
         |   cycle outcome stays PROGRESS
         |
         +--> verification REJECT
                |
                v
             cycle outcome becomes REJECTED
         |
         v
      reviewer
         |
         +--> CONTINUE
         +--> choose next active node
         +--> adjust difficulty
         +--> authorize restructure / coarse_restructure
         |
         v
      final cycle commit/tag
```

## Notes

- `INVALID` means deterministic failure.
- `NO_PROGRESS` means deterministically acceptable but no useful movement.
- `REJECTED` means deterministic validation passed but verification/reviewer-level acceptance failed.
- Easy mode is tightly scoped to one Lean proof body.
- Hard mode defaults to local edits on the active node.
- `proof_edit_mode: "restructure"` widens scope around the active node.
- `proof_edit_mode: "coarse_restructure"` is the only mode that may mutate the accepted coarse package, and it triggers a coarse-wide correspondence sweep before the package is refreshed.
