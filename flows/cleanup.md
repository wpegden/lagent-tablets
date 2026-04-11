# Cleanup Phase

Applies when `state.phase == "proof_complete_style_cleanup"`.

```text
enter cleanup from an already accepted proof-complete state
  |
  v
cleanup worker burst + handoff validation
  |
  +--> burst failed / handoff invalid
  |      |
  |      v
  |   immediate INVALID return
  |   no reviewer
  |   no cycle commit
  |
  v
cleanup deterministic gate
  |
  +--> NO_PROGRESS
  |      |
  |      v
  |   reviewer runs
  |   final cleanup commit/tag
  |
  +--> INVALID
  |      |
  |      v
  |   restore last good proof-complete cleanup checkpoint (if recorded)
  |   reviewer runs
  |   final cleanup commit/tag
  |
  +--> PROGRESS
         |
         v
      reviewer
         |
         +--> CONTINUE
         +--> DONE
         |
         v
      final cleanup commit/tag
```

## Notes

- Cleanup is polish-only over an already accepted proof.
- Semantic drift is out of scope.
- If cleanup never lands usefully, the system may still stop successfully at the last good proof-complete state.
