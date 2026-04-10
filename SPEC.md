# lagent-tablets: System Specification

Version: 4.0
Date: 2026-04-08
Status: Production — actively running formalizations

---

## 1. Purpose

lagent-tablets orchestrates LLM agents (Claude, Codex, Gemini) to formalize mathematical papers into verified Lean 4 / mathlib proofs using a "proof tablet" model.

---

## 2. Core Concepts

### Proof Tablet
A DAG of **nodes**, each a `.lean` + `.tex` pair in `Tablet/`. Children of node X are those nodes Y whose `.lean` file is imported by X's `.lean` file.

**Invariants** (enforced by verification agents):
- Every node has a Lean statement and a corresponding NL statement (.tex)
- Every theorem/lemma node has either a complete Lean proof (no sorry) OR a rigorous NL proof in its .tex from the NL statements of its children
- `Preamble.lean` contains only imports, no definitions
- All definitions must be concrete (no sorry, opaque, axiom)

### Node Difficulty: Easy vs Hard
Each node is classified as **easy** or **hard**:
- **Easy**: prove using only existing children. Only the active `.lean` proof body may change. No `.tex` edits, import changes, or new files. Filesystem-enforced (Tablet/ dir read-only, active `.tex` read-only, Preamble read-only).
- **Hard**: full flexibility — can create helper nodes, add imports, refactor.

Classification is proposed by the theorem_stating worker and finalized by the reviewer. After `easy_max_retries` (default 2) failed easy attempts, auto-elevates to hard.

Different agent configs per difficulty: `easy_worker` and `hard_worker` in config.

### Verification Pipeline
Two stages, correspondence is a **gate** for soundness:

1. **Correspondence + Paper Faithfulness** (multi-agent, parallel, one call per agent)
   - Single prompt checks both: does Lean match NL? Is each node faithful to the paper?
   - Single result file per agent with `correspondence` and `paper_faithfulness` sections
   - Agents read .lean/.tex files from disk (prompt lists nodes, doesn't inline content)
   - If ANY agent rejects, soundness is skipped — reviewer gets rejection details
   - Correspondence caching is Lean-aware: it hashes the node's `.tex` statement plus the elaborated semantic meaning of its own declaration, including definition/inductive context actually used by the statement. Proof-only changes and imported theorem churn do not invalidate it.

2. **NL Proof Soundness** (per-node, multi-agent, parallel) — only if correspondence passes
   - Each node checked individually with its children's .tex as context
   - Verdicts: SOUND, UNSOUND (proof fixable), STRUCTURAL (DAG needs restructuring)
   - Scheduled one node at a time in deterministic deepest-first DAG order
   - If `A` imports `B`, then `B` is checked before `A`
   - In theorem_stating, each cycle holds on one current soundness target until that node is accepted
   - The 3 soundness agents still run concurrently on that one node

### Theorem-Stating Target Edit Modes
When theorem_stating is holding on a current soundness target, the supervisor tracks a target edit mode:
- **repair**: default. Modeled on proof-formalization easy mode. Only `Tablet/{target}.tex` is writable, and any broader change must be escalated as restructure.
- **restructure**: explicitly authorized by the reviewer when the current target needs paper-faithful DAG enrichment, dependency changes, or statement changes.

Newly selected targets reset to `repair`.

Both stages use 3 agents in parallel (configurable via `correspondence_agents` and `soundness_agents`).

### Verification Context Continuity
Verification agents receive previous cycle's results in their prompt ("Last cycle you flagged these issues — check if they're genuinely fixed"). This prevents workers from gaming verifiers with superficial fixes, since each agent knows what was previously flagged and must independently verify the fix.

### Per-Node Verification Status
Status stored on each `TabletNode` in tablet.json:
- `correspondence_status`: "?", "pass", "fail"
- `soundness_status`: "?", "pass", "fail", "structural"
- `verification_content_hash`: hash of .lean+.tex when status was set
- Status persists until content changes (hash mismatch resets to "?")
- Closed nodes (Lean proof complete) automatically get soundness=pass

---

## 3. Workflow Phases

| Phase | Purpose |
|-------|---------|
| `theorem_stating` | Build and refine the tablet DAG. Run correspondence on changed nodes and NL-proof soundness on one current target node at a time until every target is accepted. |
| `proof_formalization` | Eliminate sorry from nodes. Easy nodes are locked to one active `.lean` proof body; hard nodes get broader refactoring freedom. |
| `proof_complete_style_cleanup` | Final cleanup after all nodes closed. |

Phase transitions require human approval via the web viewer (ADVANCE_PHASE → awaiting_human_input).

---

## 4. Agent Roles

| Role | Purpose | Session |
|------|---------|---------|
| **Worker** | Writes Lean code and NL content | Persistent (keeps context across cycles) |
| **Reviewer** | Evaluates worker output, selects next node, assigns difficulty | Persistent |
| **Correspondence Agent** | Checks Lean/NL match + paper faithfulness | Fresh each cycle |
| **Soundness Agent** | Checks NL proof rigor per-node | Fresh each cycle |

---

## 5. Agent Backends

### Codex (`codex_headless.py`)
- Runs `codex exec --json` in a tmux window
- Marker file completion (`.started`, `.exit`)
- No hard timeout — runs until done
- Effort via `-c reasoning_effort=xhigh`

### Claude/Gemini (`agentapi_backend.py`)
- Runs via agentapi HTTP wrapper around CLI in PTY
- Message delivery via POST `/message`
- Completion via a separate `*.done` marker plus status liveness
- Agents write `*.raw.json`, run the shared checker, then write the matching `*.done`
- The supervisor reruns the same checker and only then writes the canonical tracked JSON
- Liveness-based timeout: resets while status="running", only fires on sustained inactivity
- Claude effort via `--effort max`, Gemini has no effort concept

### Key Reliability Rules
- **No hard wall-clock timeouts** for any agent
- **done_file must be the correct completion marker** per call site (not hardcoded)
- **effort must be passed** from config through CorrespondenceAgentConfig to ProviderConfig
- Log files are per-port to prevent concurrent write conflicts
- Log file handles kept open (not via `with` block) for process lifetime

---

## 6. Configuration

### Config JSON (`configs/*.json`)
```json
{
  "worker": {"provider": "codex", "model": "gpt-5.4", "effort": "xhigh"},
  "easy_worker": {"provider": "gemini", "model": "auto", "fallback_models": [...]},
  "hard_worker": {"provider": "codex", "model": "gpt-5.4", "effort": "xhigh"},
  "reviewer": {"provider": "codex", "model": "gpt-5.4", "effort": "xhigh"},
  "verification": {
    "correspondence_agents": [
      {"provider": "claude", "model": "claude-opus-4-6", "effort": "max"},
      {"provider": "gemini", "model": "gemini-3.1-pro-preview", "fallback_models": [...]},
      {"provider": "codex", "model": "gpt-5.4", "effort": "xhigh"}
    ],
    "soundness_agents": [/* same format */]
  }
}
```

### Policy JSON (hot-reloadable)
Runtime tuning: retry delays, stuck recovery, prompt notes, difficulty settings.

Verification rosters can also be hot-set:
- `verification.correspondence_agent_selectors`
- `verification.soundness_agent_selectors`
- `verification.soundness_disagree_bias`

---

## 7. Git Versioning

Each cycle = one git commit + lightweight tag `cycle-N`.
- `git_ops.py`: init_repo, commit_cycle, get_cycle_history, get_cycle_diff, rewind_to_cycle
- `cycle_meta.json` stored in each commit with phase, outcome, token usage
- CLI: `--rewind-to-cycle N` for clean rewind
- Web viewer reads history from git tags

---

## 8. Mid-Cycle Resume

`state.resume_from` field enables restarting from a specific stage:
- `""` — full cycle (worker → verification → reviewer)
- `"verification"` — skip worker, run verification + reviewer
- `"reviewer"` — skip worker + verification, run reviewer with saved results

CLI: `--resume-from verification|reviewer`

When resuming from reviewer, correspondence results are loaded from saved files.

---

## 9. Model Fallback

When a model returns 429 MODEL_CAPACITY_EXHAUSTED:
1. Parse exhausted model name from error
2. Mark unavailable in `ModelAvailability` tracker (cooldown 5 min)
3. Try next model from `fallback_models` list
4. Switch via `/model` command (Gemini) or config mutation
5. Retry immediately (no backoff for model switch)

---

## 10. Token & Usage Tracking

Per-burst tracking in `BurstResult.usage`:
- Codex: parsed from `turn.completed` JSON event
- Claude: extracted from JSONL transcript
- Gemini: extracted from session JSON `usageMetadata`

Accumulated per-role in `state.agent_token_usage` with per-model breakdown.
Walltime tracked on all multi-agent results.

---

## 11. Web Viewer

Node.js server at port 3300, nginx serves static files.

### Features
- DAG visualization with node status colors
- Difficulty: rounded corners (easy) vs sharp (hard)
- Verification: border style (solid=C pass, dashed=C unknown, dotted=C fail), edge style (same for P)
- Closed nodes: P=pass automatic
- Cycle history slider (loads state from git per cycle)
- Human feedback panel (approve/feedback at phase boundaries)
- Tablet snapshot download (.zip with README)
- Mobile responsive (pinch zoom, slide-up detail sheet)
- Pan/zoom on DAG (mouse wheel + drag, touch gestures)

### Static file generation
`writeStatic()` every 30s generates `api/viewer-state.json`, `api/cycles.json`, `api/state-at/N.json`.

---

## 12. Management Scripts

| Script | Purpose |
|--------|---------|
| `scripts/setup_repo.sh <path> <paper.tex>` | Create new formalization repo |
| `scripts/pause.sh [repo]` | Graceful stop after current cycle |
| `scripts/stop.sh [repo]` | Force kill all processes |
| `scripts/rewind.sh <cycle> [stage] [repo]` | Rewind to cycle+stage, clean artifacts |
| `scripts/resume.sh <config> [args...]` | Start supervisor in tmux |
| `scripts/status.sh [repo]` | Show all agents, results, thoughts |

---

## 13. Usage Examples

### Setting up a new formalization
```bash
./scripts/setup_repo.sh /home/leanagent/math/my_paper_tablets /path/to/paper.tex
```

### Starting a run
```bash
# First run (starts from theorem_stating)
./scripts/resume.sh configs/my_config.json --stop-at-phase-boundary

# Check status
./scripts/status.sh /home/leanagent/math/my_paper_tablets
```

### Pausing and resuming
```bash
# Graceful pause after current cycle
./scripts/pause.sh /home/leanagent/math/my_paper_tablets

# Resume from saved state
./scripts/resume.sh configs/my_config.json --stop-at-phase-boundary
```

### Rewinding to a previous cycle
```bash
# Rewind to cycle 3, resume from verification stage
./scripts/rewind.sh 3 verification /home/leanagent/math/my_paper_tablets
./scripts/resume.sh configs/my_config.json --resume-from verification

# Rewind to cycle 1, re-run everything (worker + verification + reviewer)
./scripts/rewind.sh 1 worker /home/leanagent/math/my_paper_tablets
./scripts/resume.sh configs/my_config.json
```

### Mid-cycle resume (skip stages)
```bash
# Skip worker, re-run verification + reviewer on current tablet
./scripts/resume.sh configs/my_config.json --resume-from verification

# Skip worker + verification, re-run reviewer with saved correspondence results
./scripts/resume.sh configs/my_config.json --resume-from reviewer
```

### Emergency stop
```bash
./scripts/stop.sh /home/leanagent/math/my_paper_tablets
```

### Viewing the web dashboard
The viewer runs in a tmux session:
```bash
tmux new-session -d -s viewer "REPO_PATH=/path/to/repo node viewer/server.js"
```
Then visit `http://your-server/lagent-tablets/` (or localhost:3300).

### CLI direct usage
```bash
# Dry run (validate config)
python3 -m lagent_tablets.cli --config configs/my_config.json --dry-run

# Run N cycles then stop
python3 -m lagent_tablets.cli --config configs/my_config.json --cycles 5

# Rewind via CLI
python3 -m lagent_tablets.cli --config configs/my_config.json --rewind-to-cycle 2
```

---

## 14. File Structure

```
lagent_tablets/
  cli.py              — CLI entry point, main loop
  cycle.py            — Cycle logic: worker → verification → reviewer
  config.py           — Config/policy loading
  state.py            — TabletNode, TabletState, SupervisorState
  tablet.py           — Tablet operations, support file generation
  prompts.py          — Prompt builders for all roles
  burst.py            — Agent dispatch (routes to backends)
  git_ops.py          — Git versioning (commit, tag, history, rewind)
  model_availability.py — Model fallback tracking
  nl_cache.py         — NL verification content-addressed cache
  agents/
    agentapi_backend.py — Claude/Gemini via agentapi HTTP
    codex_headless.py   — Codex via headless JSON mode
  check.py            — Deterministic Lean checks
  verification.py     — Script generation for worker self-checks

prompts/              — Prompt templates (.md)
skills/               — Agent skill files
configs/              — Run configurations
scripts/              — Management scripts
viewer/               — Web viewer (Node.js + static HTML)
tests/                — Unit + regression tests
```

---

## 14. Testing

168+ tests covering:
- State serialization, config parsing, tablet operations
- Difficulty tiering, auto-elevation
- Git versioning (commit, history, diff, rewind)
- Multi-agent correspondence reconciliation
- Model fallback and availability tracking
- **Agent dispatch regression tests** (16 tests):
  - done-marker routing per provider
  - effort passthrough (codex xhigh, claude max)
  - No hard timeout in codex script
  - Prompt doesn't inline content
  - End-to-end config load verification

Run: `python3 -m pytest tests/ --ignore=tests/test_adapters_live.py`

---

## 15. Known Issues / Future Work

- Mathlib olean cache needs `lake build` before first compilation check
- Dangling imports (nodes importing nonexistent nodes) should be flagged in viewer
- Token tracking from Claude/Gemini transcripts is implemented but lightly tested
- Gemini agentapi reliability depends on model capacity (429 errors common)
