# Post-Burst Permission Hardening Plan

## Goal

Make every worker burst safe for supervisor-side reads even when the worker uses shell write patterns that produce restrictive modes or the wrong group.

## Problem

Pre-burst permission setup constrains what the worker may edit, but it does not guarantee that newly written files remain supervisor-readable after the burst. In particular:

- direct writes usually inherit acceptable permissions
- same-directory temp-file-and-rename can preserve restrictive modes
- `/tmp` temp-file-and-move can preserve both restrictive mode and the wrong group

This can crash the supervisor before validation or verification starts.

## Writable Surfaces To Harden

Normalize only the shared surfaces the supervisor must read after a burst:

- `Tablet/`
- `Tablet.lean`
- `.agent-supervisor/staging/`
- existing `.lake/build/` roots, including package build dirs

Do not recurse across the whole repo.

## Required Post-Burst State

- directories: mode `2775`, group `leanagent`
- regular files: mode `664`, group `leanagent`

This is a post-burst normalization target, not a worker-scope restriction target. Fine-grained restrictions are reapplied before the next burst.

## Additional Sanity Checks

Before the supervisor reads these surfaces, reject:

- symlinks
- non-regular files where regular files are expected
- unexpected nested directories under `Tablet/`

Use `lstat`, not `stat`, when checking file types.

## Implementation Steps

1. Add a shared repo-surface normalization helper in `lagent_tablets/health.py`.
   - Normalize explicit roots only.
   - Reuse the same two-pass pattern as `fix_lake_permissions(...)`:
     - first try as supervisor
     - then run a second pass as `lagentworker`

2. Add a repo-surface sanity checker.
   - Validate `Tablet/`, `Tablet.lean`, and staging artifacts before any supervisor-side reads.
   - Reject symlinks and non-regular files with a clear error.

3. Call the normalization/sanity pass at the correct times.
   - after every successful burst, before `_accept_validated_artifact(...)`
   - at cycle/resume entry, before supervisor-side hashing or artifact reads
   - keep existing `.lake` normalization, but route it through the shared helper path

4. Add regression tests.
   - worker-created `0600` files in `Tablet/` are repaired
   - worker-created staging artifacts with restrictive permissions are repaired
   - `/tmp`-then-`mv` style files are repaired
   - symlink or non-regular-file artifacts are rejected

5. Validate and rerun.
   - focused tests for the new helper and cycle call sites
   - full non-live suite
   - clean-restart `extremal` and monitor again through cycle 10

## Scope Choice

This plan intentionally does not try to normalize the whole repo. The target is only the post-burst surfaces that can crash the supervisor or trick it into reading the wrong file.
