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
