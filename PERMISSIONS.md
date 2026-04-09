# Permissions Model

## Users and Groups

| User | UID | Role | Group membership |
|------|-----|------|-----------------|
| `leanagent` | 1000 | Supervisor process | `leanagent` (primary) |
| `lagentworker` | 3188 | Agent process (burst_user) | `lagentworker` (primary), `leanagent` (shared group) |

Shared group: `leanagent` (GID 1000). Both users are members.

## Sudo

`leanagent` can run any command as `lagentworker` without password:
```
leanagent ALL=(lagentworker:leanagent) NOPASSWD: ALL
```

## Directory Layout and Permissions

### Repo root (`/home/leanagent/math/{project}`)
```
Owner: leanagent:leanagent
Mode:  2755 (setgid, not group-writable)
```
Workers can read and traverse the repo root, but cannot create/delete arbitrary
top-level files there.

### Tablet directory (`Tablet/`)
```
Owner: leanagent:leanagent  
Mode:  2775 (setgid, group-writable)
```
lagentworker can create new files (new tablet nodes).

### Tablet node files (`Tablet/{name}.lean`, `Tablet/{name}.tex`)

**Before each cycle, the supervisor sets permissions:**

| File | Mode | Who can write | Purpose |
|------|------|--------------|---------|
| Active node `.lean` | 0664 | Both | Worker edits proof |
| Active node `.tex` | 0664 | Both | Worker edits NL proof |
| `Preamble.lean` | 0664 | Both | Worker adds imports |
| All other `.lean` | 0644 | Supervisor only | FROZEN during cycle |
| All other `.tex` | 0644 | Supervisor only | FROZEN during cycle |

**Supervisor-generated files** (`INDEX.md`, `README.md`, `header.tex`, `Tablet.lean`):
```
Owner: leanagent:leanagent
Mode:  0664
```
These are always written by the supervisor (leanagent). If lagentworker somehow creates one (e.g., during a burst), the supervisor deletes and recreates it at cycle start.

### State directory (`.agent-supervisor/`)
```
Owner: leanagent:leanagent
Mode:  2755
```

| File/Dir | Mode | Written by | Read by |
|----------|------|-----------|---------|
| `state.json` | 0600 | supervisor | supervisor |
| `tablet.json` | 0600 | supervisor | supervisor |
| `scripts/` | 2755 (dir) | supervisor | both |
| `scripts/check.py` | 0755 | supervisor | both (executed by lagentworker, not writable) |
| `scripts/*.sh` | 0755 | supervisor | both |
| `staging/` | 2775 (dir) | both | both |
| `staging/*.raw.json` | 0664 | worker/reviewer/verifier | supervisor validates |
| `staging/*.done` | 0664 | worker/reviewer/verifier | supervisor waits on |
| `logs/` | 2775 (dir) | supervisor | both |
| `logs/cycle-NNNN/` | 2775 (dir) | supervisor creates, lagentworker writes into |
| `logs/cycle-NNNN/*.sh` | 0755 | supervisor | lagentworker executes |
| `logs/cycle-NNNN/*.txt` | 0644 | supervisor | lagentworker reads |
| `logs/cycle-NNNN/*.started` | 0664 | lagentworker | supervisor reads |
| `logs/cycle-NNNN/*.exit` | 0664 | lagentworker | supervisor reads |
| `logs/cycle-NNNN/*.log` | 0664 | both (pipe-pane + cat) | both |
| `logs/health.jsonl` | 0664 | supervisor | supervisor |
| `prompts/` | 2775 (dir) | supervisor | lagentworker reads |
| `prompts/*.txt` | 0644 | supervisor | lagentworker reads |
| `checkpoints/` | 2755 (dir) | supervisor | both read |

### Worker handoff (`worker_handoff.json` in repo root)
```
Owner: leanagent:leanagent (written by supervisor after validation)
Mode:  0600 or 0644 depending on umask
```
The worker writes `staging/worker_handoff.raw.json` and then `staging/worker_handoff.done`.
The supervisor validates the raw JSON with `.agent-supervisor/scripts/check.py`
and only then writes the canonical `worker_handoff.json`.

### Reviewer decision (`reviewer_decision.json` in repo root)
```
Owner: leanagent:leanagent (written by supervisor after validation)
Mode:  0600 or 0644 depending on umask
```
Same staging-and-validation flow as handoff.

### `.lake/` directory

**Critical: DO NOT chmod files in `.lake/packages/`.**
This is a git checkout. Changing permissions causes `git status` to detect "local changes"
and Lake refuses to build.

| Path | Mode | Who manages |
|------|------|------------|
| `.lake/` | 2775 | supervisor |
| `.lake/build/` | 2775 (recursive) | supervisor sets, both write |
| `.lake/build/**` files | 0664 | Lake writes (as either user) |
| `.lake/packages/` | DO NOT TOUCH | Lake manages (git checkout) |

The supervisor runs `fix_lake_permissions()` at cycle start, which ONLY touches `.lake/build/`.

### Lean toolchain (`.elan/`)
```
Owner: leanagent
Shared via: ACL (setfacl -R -m u:lagentworker:rX)
```
lagentworker has read+execute access. Cannot modify.

### Agent CLI installations
```
~/.local/share/claude/ -- leanagent owns, lagentworker reads via ACL
~/.nvm/ -- leanagent owns, lagentworker reads via ACL
/usr/bin/codex -- system-installed, both can execute
```

## Cycle Permission Flow

```
1. Supervisor starts cycle
   ├── fix_lake_permissions(repo)        # .lake/build/ → 2775/0664
   ├── setup_permissions(config, active) # Set Tablet/ file modes
   │   ├── Active node .lean → 0664
   │   ├── Active node .tex  → 0664
   │   ├── Preamble.lean     → 0664
   │   ├── All other .lean   → 0644
   │   └── All other .tex    → 0644
   ├── Clear stale staging/*.raw.json and staging/*.done
   ├── Write prompt to logs/cycle-NNNN/worker-prompt.txt (0644)
   ├── Write burst script to logs/cycle-NNNN/worker-burst.sh (0755)
   └── Launch: sudo -n -u lagentworker /path/to/worker-burst.sh

2. Worker runs as lagentworker
   ├── Reads prompt file (0644 → lagentworker can read)
   ├── Edits active node .lean (0664 → lagentworker can write)
   ├── May edit Preamble.lean (0664 → lagentworker can write)
   ├── CANNOT edit other .lean (0644 → lagentworker cannot write)
   ├── Writes staging/worker_handoff.raw.json
   ├── Runs .agent-supervisor/scripts/check.py
   ├── Writes staging/worker_handoff.done
   ├── May create new Tablet/{name}.lean files (Tablet/ is 2775)
   ├── Writes start marker (logs/ is 2775)
   └── Writes exit marker via trap EXIT

3. Supervisor validates
   ├── Reads modified files (leanagent owns or can read group files)
   ├── Validates staging/*.raw.json with the shared checker
   ├── Writes canonical worker_handoff.json/reviewer_decision.json
   ├── Runs lake env lean (as leanagent, reads .lake/build/ which is group-readable)
   └── regenerate_support_files (deletes and recreates supervisor-owned files)
```

## Anti-cheat Properties

1. **lagentworker cannot modify non-active node .lean files**: mode 0644, lagentworker is not owner
2. **lagentworker cannot modify .lake/packages/**: git checkout, lagentworker has no write access
3. **lagentworker cannot modify state.json/tablet.json**: mode 0600, only leanagent can read/write
4. **lagentworker cannot replace the checker or burst scripts**: `.agent-supervisor/` and `scripts/` are not group-writable
5. **lagentworker CAN create new files in Tablet/**: this is intentional (new helper nodes)
6. **lagentworker CAN modify Preamble.lean**: intentional (add imports) but supervisor validates changes
