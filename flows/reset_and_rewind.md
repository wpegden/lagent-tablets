# Reset And Rewind

There are two distinct operations.

## Reviewer-Directed Reset

Applies inside theorem-stating after an `INVALID` attempt.

```text
INVALID theorem-stating attempt
  |
  v
reviewer sees dirty worktree
  |
  +--> no reset
  |      |
  |      v
  |   continue from current dirty worktree
  |
  +--> CONTINUE + reset_to_checkpoint = valid committed ref
         |
         v
      supervisor enforces target is valid
         |
         v
      git reset --hard <ref>
      git clean -fdx  (main repo, preserving nested chats dir entry)
      rewind nested chats repo to matching ref
      clean chats repo
      delete future cycle tags
         |
         v
      retry from clean checkpoint
```

## Explicit Rewind

Applies when an operator runs the rewind tooling.

```text
operator chooses exact committed ref
  |
  v
stop live processes
  |
  v
reset to exact ref
  |
  v
rewind nested chats repo to exact ref
  |
  v
clean worktree
  |
  v
clear provider session directories
  |
  v
delete future cycle tags
  |
  v
resume from that committed state only
```

## Notes

- Resets and rewinds use exact committed states only.
- Valid reset targets exclude invalid attempts and arbitrary dirty worktrees.
- There is no branching model here; after rewind/reset the project should be clean.
