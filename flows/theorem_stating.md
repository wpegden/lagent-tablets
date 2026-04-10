# Theorem Stating

Applies when `state.phase == "theorem_stating"`.

```text
start theorem-stating attempt
  |
  v
worker burst
  |
  v
deterministic worker validation
  |
  +--> INVALID
  |      |
  |      v
  |   keep dirty worktree visible to reviewer
  |      |
  |      v
  |   reviewer sees:
  |     - current worktree
  |     - invalid blocker detail
  |     - valid reset checkpoints
  |      |
  |      +--> CONTINUE, no reset
  |      |      |
  |      |      v
  |      |   same in-flight cycle retries
  |      |   same worktree persists
  |      |
  |      +--> CONTINUE + reset_to_checkpoint
  |      |      |
  |      |      v
  |      |   supervisor resets to that valid checkpoint
  |      |   and cleans the repo/chats
  |      |      |
  |      |      v
  |      |   same in-flight cycle retries from clean checkpoint
  |      |
  |      +--> NEED_INPUT
  |             |
  |             v
  |          pause for human input
  |
  +--> VALID
         |
         v
      commit worker checkpoint
         |
         v
      correspondence / paper-faithfulness frontier
         |
         +--> REJECT on correspondence
         |      |
         |      v
         |   skip soundness
         |
         +--> APPROVE on correspondence
                |
                v
             soundness on current target only
         |
         v
      reviewer
         |
         +--> CONTINUE
         +--> ADVANCE_PHASE
         +--> NEED_INPUT
         |
         v
      final cycle commit/tag
```

## Notes

- `state.cycle` is the last committed cycle, not the current dirty attempt.
- The live viewer may show `meta.in_flight_cycle` while `state.cycle` is still the previous committed value.
- Worker `DONE` is advisory. The reviewer still decides whether theorem-stating is complete enough to advance.
- Every 5th consecutive theorem-stating `INVALID`, the reviewer prompt explicitly nudges the reviewer to consider resetting to a valid checkpoint.
- Resets are reviewer-controlled only. There is no automatic rollback on theorem-stating `INVALID`.
