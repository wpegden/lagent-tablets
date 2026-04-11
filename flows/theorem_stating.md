# Theorem Stating

Applies when `state.phase == "theorem_stating"`.

```text
normalize persisted theorem-stating state
  |
  v
preflight consistency check
  |
  +--> INVALID
  |      |
  |      v
  |   return immediately
  |   no worker burst
  |   no reviewer
  |   no cycle commit
  |
  v
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
  |   no worker checkpoint
  |   no NL verification
  |   keep dirty worktree visible to reviewer
  |      |
  |      v
  |   reviewer sees current worktree, blocker detail,
  |   and valid reset checkpoints
  |      |
  |      +--> CONTINUE
  |      |      |
  |      |      +--> no reset: same in-flight cycle retries, dirty worktree persists
  |      |      |
  |      |      +--> reset_to_checkpoint:
  |      |               supervisor resets to that valid checkpoint,
  |      |               cleans main repo + nested chats repo,
  |      |               then retries same in-flight cycle
  |      |
  |      +--> NEED_INPUT / other reviewer decision
  |             |
  |             v
  |          saved in state; no cycle commit
  |
  +--> VALID worker attempt
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
             soundness on current held target, if any
         |
         v
      reviewer
         |
         +--> CONTINUE
         +--> ADVANCE_PHASE requested
         |      |
         |      v
         |   run full verification gate on all nodes
         |      |
         |      +--> blockers remain: force CONTINUE
         |      +--> clean: advance phase
         +--> NEED_INPUT
         |
         v
      final cycle commit/tag
```

## Notes

- `state.cycle` is the last committed cycle, not the current dirty attempt.
- The live viewer may show `meta.in_flight_cycle` while `state.cycle` is still the previous committed value.
- Worker `DONE` is advisory. The reviewer still decides whether theorem-stating is complete enough to advance.
- Worker `CRISIS` is a special broad theorem-stating status: if accepted, NL verification is skipped and the reviewer sees the escalation directly.
- Every 5th consecutive theorem-stating `INVALID`, the reviewer prompt explicitly nudges the reviewer to consider resetting to a valid checkpoint.
- Resets are reviewer-controlled only. There is no automatic rollback on theorem-stating `INVALID`.
