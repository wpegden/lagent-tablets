# Bubblewrap Isolation Plan

## Goal

Prevent worker, reviewer, and verification agents from reading sibling projects or the source checkout while preserving the current supervisor behavior, tool access, and networked model backends.

Success means:
- agent bursts can read and write the current project normally
- agent bursts cannot read sibling tablet repos
- agent bursts do not need direct access to `/home/leanagent/src/lagent-tablets`
- all live backends used by `extremal` are covered
- supervisor semantics are unchanged unless a deliberate workflow fix is required

## Threat Model

Current failure:
- the worker can read `/home/leanagent/math/extremal_vectors_tablets`
- the worker can read `/home/leanagent/src/lagent-tablets`
- a fresh theorem-stating run copied a sibling tablet wholesale

Isolation target:
- project repo: read-write
- project-local runtime snapshot: read-only or read-write as appropriate
- worker home: read-write
- system binaries/libraries/toolchain: read-only
- sibling repos and source checkout: unavailable inside the sandbox

Non-goals for this change:
- containerizing the supervisor
- changing git/history semantics
- changing theorem/proof workflow policy except where strictly needed for sandbox compatibility

## Design Choice

Use `bwrap` for all burst backends, combined with project-local runtime snapshots.

Why this design:
- permission-only gating is too brittle
- virtualenv does not isolate filesystem access
- mounting the host source repo read-only would still let agents inspect non-project material
- project-local runtime snapshots let the sandbox expose only the project plus the minimal runtime payload

## Runtime Surface

### Inside the sandbox

Read-write:
- project repo at its real absolute path
- worker home at `/home/<burst_user>`
- `/tmp`
- `/var/tmp`

Read-only:
- `/usr`
- `/bin`
- `/sbin` if present
- `/lib`
- `/lib64`
- `/etc`
- `/opt` if present
- `/home/leanagent/.elan`
- `/home/leanagent/.local/bin`
- `/home/leanagent/.nvm/versions/node/v22.22.2/bin`

Synthetic:
- `/proc`
- `/dev`
- parent directories needed to bind the project and mounted toolchain paths

Hidden:
- `/home/leanagent/src`
- `/home/leanagent/math/<other projects>`

### Project-local runtime snapshot

Create and maintain:
- `.agent-supervisor/runtime/src/lagent_tablets/...`
- `.agent-supervisor/runtime/src/scripts/lean_semantic_fingerprint.lean`
- `.agent-supervisor/skills/...`

The worker-facing deterministic scripts must bootstrap from the runtime snapshot, not from the host source checkout.

## Code Changes

### 1. Config surface

Add an explicit sandbox config block with default-on behavior for fresh projects:
- `enabled`
- `backend`

Behavior:
- setup/reseed writes sandbox-enabled config by default
- an explicit opt-out remains available for recovery/debugging
- the default path for new work is `bwrap`, not the old unsandboxed launch

### 2. Project-local runtime snapshot

Add a new runtime materialization path that:
- copies the `lagent_tablets` package needed by worker-side scripts
- copies `scripts/lean_semantic_fingerprint.lean`
- copies worker/reviewer skill files into `.agent-supervisor/skills`

Refresh points:
- setup/reseed
- supervisor startup
- before each cycle/worker burst if needed

Setup integration requirement:
- `scripts/setup_repo.sh` must materialize the runtime snapshot and write the sandbox-enabled config as part of initial project creation

### 3. Deterministic script bootstrap

Change `.agent-supervisor/scripts/check.py` generation so it imports from:
- `.agent-supervisor/runtime/src`

Not from:
- `/home/leanagent/src/lagent-tablets`

### 4. Bubblewrap wrapper

Add a dedicated launcher helper that:
- builds a `bwrap` command for a given project and burst user
- preserves network access
- binds the project at its real path
- binds worker home and required host toolchain/bin directories
- creates missing parent directories inside the sandbox

Use this wrapper in:
- `codex_headless`
- `script_headless`
- `agentapi_backend`

### 5. Launch integration

Codex/script:
- wrap the burst shell script execution in `bwrap`

AgentAPI:
- wrap the `agentapi server ... -- <agent cmd>` process in `bwrap`
- keep the supervisor outside the sandbox

### 6. Prompt/runtime path alignment

Ensure prompts and skill references point to project-local copies when available.

The required worker-visible paths after this change:
- `.agent-supervisor/scripts/check.py`
- `.agent-supervisor/skills/*.md`
- `.agent-supervisor/scratch/...`

## Tests

### Unit tests

Add focused tests for:
- sandbox config parsing/defaults
- runtime snapshot materialization
- deterministic script bootstrap path points to runtime snapshot
- bwrap command contains the expected binds and omits sibling/source paths
- codex/script launch paths wrap with bwrap when enabled
- agentapi launch path wraps the server command with bwrap when enabled

### Integration-style local tests

Add tests that verify:
- sandboxed command can read the project
- sandboxed command cannot read a sibling repo path
- worker-side `check.py` can still run from the runtime snapshot

### Non-regression

Run:
- `tests/test_agent_dispatch.py`
- config/parser tests
- prompt tests
- cycle tests affected by runtime script generation

## Live Validation

After implementation and tests are solid:
- reseed `extremal` cleanly with sandbox enabled
- run until a clean start reaches 5 healthy cycles
- monitor each cycle in detail
- if a code issue appears, stop, fix, retest, reseed, and restart from clean state
- pause at cycle 5 for review

Health criteria for the live run:
- no agent can read sibling repos from inside the sandbox
- no sandbox-induced deterministic failures
- no broken worker/reviewer/verification prompts due to missing project-local runtime files
- cycle progression remains coherent

## Commit Checkpoints

Planned commits:
1. baseline before bwrap work
2. runtime snapshot + setup/config scaffolding
3. bwrap launcher integration
4. tests and validation hardening
5. any live-run bug fixes, each as a separate checkpoint when meaningful
