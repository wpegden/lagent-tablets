# Cleanup Phase

Applies when `state.phase == "proof_complete_style_cleanup"`.

```text
enter cleanup from an already accepted proof-complete state
  |
  v
cleanup worker burst
  |
  v
cleanup deterministic gate
  |
  +--> INVALID
  |      |
  |      v
  |   restore last good proof-complete cleanup checkpoint
  |   reviewer may continue or stop
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
