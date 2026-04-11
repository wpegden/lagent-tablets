# Change Log

Track every source change made during the bwrap isolation effort.

## Entries

- Added planning artifacts for the bwrap effort:
  - `plans/bwrap_implementation_plan.md`
  - `plans/extremal_5_cycle_validation_plan.md`
  - `plans/change_log.md`
  - `plans/workflow_semantics_changes.md`
- Added default-on sandbox config support:
  - `lagent_tablets/config.py`
  - `configs/extremal_vectors_run.json`
- Added project-local runtime snapshotting for sandboxed bursts:
  - `lagent_tablets/runtime_snapshot.py`
  - `lagent_tablets/project_paths.py`
  - `lagent_tablets/check.py`
  - `lagent_tablets/prompts.py`
  - `scripts/setup_repo.sh`
  - `lagent_tablets/cli.py`
- Added bubblewrap launcher support:
  - `lagent_tablets/sandbox.py`
  - `lagent_tablets/burst.py`
  - `lagent_tablets/agents/codex_headless.py`
  - `lagent_tablets/agents/script_headless.py`
  - `lagent_tablets/agents/agentapi_backend.py`
  - `lagent_tablets/cycle.py`
- Added sandbox/runtime test coverage:
  - `tests/test_sandbox.py`
  - `tests/test_verification.py`
  - `tests/test_agent_dispatch.py`
  - `tests/test_config.py`
  - `tests/test_prompts.py`
  - `tests/test_history_replay.py`
- Added setup/startup sandbox preflight so bwrap host failures are caught immediately:
  - `scripts/setup_repo.sh`
  - `lagent_tablets/cli.py`
- Fixed DNS inside the bwrap namespace by mounting escaped config symlink targets such as `/etc/resolv.conf -> /run/systemd/resolve/stub-resolv.conf`:
  - `lagent_tablets/sandbox.py`
  - `tests/test_sandbox.py`
