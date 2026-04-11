# Extremal 5-Cycle Run Notes

## Host / setup

- `bwrap` setuid host fix applied.
- Verified sandbox behavior before supervisor start:
  - project readable
  - sibling tablet blocked
  - source checkout blocked

## Cycle notes

### Cycle 1

- first clean start reached `cycle-0001/worker_handoff_attempt_0001`
- aborted before meaningful worker progress
- worker log showed repeated DNS lookup failures to `chatgpt.com`
- root cause: inside `bwrap`, `/etc/resolv.conf` existed only as a symlink, but its target `/run/systemd/resolve/stub-resolv.conf` was not mounted
- fixed in source:
  - `lagent_tablets/sandbox.py`
  - `tests/test_sandbox.py`
- regression confirmed after fix:
  - `/etc/resolv.conf` readable inside sandbox
  - `getent hosts chatgpt.com` succeeds inside sandbox as `lagentworker`
- this attempt does not count as a validation cycle; it was stopped before any real theorem-stating work

### Cycle 1 restart: aborted on hidden setup-cache failure

- fresh setup completed and the worker began healthy local theorem-stating work
- no sibling-tablet access occurred; isolation held
- the worker created a real first batch of paired node files
- first Lean build pass revealed that compiled `Mathlib` modules were not available in the fresh project search path
- the worker had to run `lake exe cache get` inside the cycle to make the project usable
- root cause in setup:
  - `scripts/setup_repo.sh` was swallowing `lake exe cache get` failure with `|| true`
  - the later empty-tablet checks did not catch missing mathlib cache because seeded `Preamble.lean` imported nothing
- fix made in source:
  - setup now requires `lake exe cache get` to succeed
  - setup now also runs `lake env lean .agent-supervisor/scratch/example.lean` as part of the worker-side prewarm validation
- this start does not count as a clean validation cycle run because the worker had to repair the environment mid-cycle
