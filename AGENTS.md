# AGENTS.md — Guide for Agents Working on lagent-tablets

This document is for LLM agents (and humans) maintaining, debugging, or extending the lagent-tablets system. It covers the architecture, common pitfalls, and everything you need to know to work effectively.

---

## 1. What This System Does

lagent-tablets orchestrates multiple LLM agents to formalize mathematical papers into verified Lean 4 proofs. It manages a "proof tablet" — a DAG of nodes where each node is a `.lean` + `.tex` pair representing one mathematical result.

The system runs in cycles:
1. **Worker** creates/modifies nodes (writes Lean code and NL proofs)
2. **Verification agents** check the work (correspondence, faithfulness, soundness)
3. **Reviewer** decides next steps based on verification results
4. Repeat until the paper is fully formalized

---

## 2. The Proof Tablet Model

### Nodes
Each node in `Tablet/` has:
- `{name}.lean` — Lean 4 declaration (theorem, lemma, or definition)
- `{name}.tex` — Natural language statement + rigorous NL proof

### Imports = DAG edges
If `A.lean` has `import Tablet.B`, then A depends on B. B's NL statement can be cited in A's NL proof.

### Invariants (enforced by verification)
- Every node has both a Lean statement and an NL statement
- Lean and NL statements must correspond (checked by correspondence agents)
- Every theorem/lemma has either:
  - A complete Lean proof (no `sorry`), OR
  - A rigorous NL proof from its children's NL statements (checked by soundness agents)
- `Preamble.lean` contains ONLY imports — no definitions
- All definitions must be concrete (no `sorry`, `opaque`, `axiom`)
- No bare `import Mathlib` — only specific submodule imports
- Project definitions should not duplicate Mathlib concepts

### Node Difficulty
- **Easy**: straightforward proof from existing children. Only the active `.lean` proof body may change. No `.tex` edits, import changes, or new files. Filesystem-enforced.
- **Hard**: may require new helper nodes, import changes, refactoring.
- Auto-elevates from easy to hard after 2 failed attempts.
- Different agent configs per difficulty (e.g., Gemini for easy, Codex for hard).

---

## 3. Verification Pipeline — Critical Details

### Three checks, two stages
**Stage 1: Correspondence + Paper Faithfulness** (one call per agent)
- Does each node's Lean statement genuinely capture its NL statement?
- Is each node a faithful intermediate step from the paper?
- 3 agents run in parallel, each independently reading files from disk
- Each agent gets its OWN previous cycle's results as context (not other agents')
- Correspondence is a **GATE** — if rejected, soundness is skipped entirely
- Correspondence invalidation is statement-level: proof-only `.tex` edits do not reopen it; `.tex` statement changes propagate upward through importing nodes only for definition nodes; Lean-side reopening uses the semantic correspondence fingerprint rather than raw text diffs
- Correspondence caching is Lean-aware: it tracks the node's `.tex` statement plus the elaborated semantic meaning of its own Lean declaration. Proof-only changes and imported theorem churn do not invalidate it; imported definition changes that the statement actually depends on do.

**Stage 2: NL Proof Soundness** (per-node, only if correspondence passes)
- Is each node's NL proof rigorous from its children's NL statements?
- Each node checked individually with 3 agents
- Verdicts: SOUND, UNSOUND (proof fixable), STRUCTURAL (DAG needs restructuring)
- Scheduled one node at a time in deepest-first DAG order: if `A` imports `B`, then `B` is checked before `A`
- In theorem_stating, each cycle holds on a single current soundness target until that node is accepted, unless correspondence/paper-faithfulness blockers reopen first; while correspondence is open, the soundness target is suspended
- The 3 soundness agents still run concurrently on that one node
- Accepted per-node results can be reused across verification restarts when the node's `.tex` proof context is unchanged

### Theorem-Stating Target Modes
- **repair**: modeled on proof-formalization easy mode. Only `Tablet/{target}.tex` is writable. If broader changes are needed, the worker should return `STUCK` and explain the restructure needed.
- **restructure**: explicitly authorized by the reviewer when paper-faithful DAG enrichment or dependency changes are needed for the same target. Broader edits are allowed, but only inside that target-centered authorized impact region (the target, prerequisites, and downstream consumers).

New or changed theorem-stating targets default back to `repair`.

### Verification Status Persistence
- Stored per-node in `tablet.json`: `correspondence_status`, `soundness_status`
- **Sticky**: persists until node content changes (tracked via `verification_content_hash`)
- Closed nodes (Lean proof complete) automatically get soundness=pass
- All result files are **tracked in git** — complete history preserved

### Context Continuity
Verifiers are stateless (fresh each cycle) but receive their own previous results in the prompt. This now applies to both correspondence and per-node soundness. Each agent sees only its own prior findings for the same check, so workers cannot game verification with superficial fixes.

---

## 4. Agent Backend Details

### Three backends, critical differences

| Backend | Provider | Session | Completion Signal | Timeout |
|---------|----------|---------|-------------------|---------|
| `codex_headless.py` | Codex | Persisted thread for stateful worker/reviewer bursts; fresh for verification | `.exit` marker file | No completion timeout (startup timeout only) |
| `agentapi_backend.py` | Claude, Gemini | PTY via agentapi HTTP | `done_file` (separate `.done` marker) | Liveness-based (inactivity only) |
| `script_headless.py` | Fallback | `-p` mode | Process exit | No completion timeout (startup timeout only) |

### Critical Rules (LEARNED THE HARD WAY)

1. **done_file is a completion marker, not the canonical result file**
   - Agents now write `*.raw.json`, run the shared checker, then write a matching `*.done`
   - The supervisor waits on `done_file`, reruns the same checker, and only then writes the canonical tracked JSON
   - Correspondence agent 0 uses `correspondence_result_0.done`
   - Per-node soundness uses `nl_proof_{node}_{i}.done`
   - Default `reviewer_decision.done` is ONLY for the actual reviewer
   - **Regression test**: `TestCorrespondenceAgentDoneFiles`, `TestWorkerBurstDoneFile`

2. **No hard wall-clock timeouts**
   - Codex/script headless: no completion watchdog or `timeout` wrapper; only startup failure detection
   - Agentapi: liveness-based — resets while status="running", only fires on sustained inactivity
   - Extended thinking (Claude max effort) can run 15+ minutes — NEVER kill an active agent
   - **Regression test**: `TestCodexNoHardTimeout`

3. **effort MUST be passed through**
   - `CorrespondenceAgentConfig.effort` → `ProviderConfig.effort` → backend command
   - Codex: `-c reasoning_effort=xhigh`
   - Claude: `--effort max`
   - Gemini: no effort concept (ignored)
   - **Regression test**: `TestEffortPassthrough`

4. **Log file handles must stay open**
   - `_launch_server` keeps the log file open for the process lifetime
   - Do NOT use `with open() as f:` — the fd closes when the block exits, killing agentapi's stdout
   - Per-port log files prevent concurrent write conflicts

5. **Prompts reference files, don't inline content**
   - Correspondence prompt is ~9K (was 70K before fix)
   - Agents read .lean/.tex from disk via tool calls
   - **Regression test**: `TestPromptNoInlineContent`

6. **Canonical result files are tracked in git; raw/done staging artifacts are not**
   - tracked: `correspondence_result_*.json`, `nl_proof_result_*.json`, `reviewer_decision.json`, `worker_handoff.json`
   - staging only: `*.raw.json`, `*.done`
   - NEVER delete the canonical tracked files — they provide verification context continuity
   - `git show cycle-N:correspondence_result_0.json` retrieves any cycle's results

### Port Allocation
| Port Range | Purpose |
|------------|---------|
| 3284 | Worker (agentapi) |
| 3285 | Reviewer (agentapi) |
| 3286, 3288, 3290 | Correspondence agents |
| 3300 | Web viewer (RESERVED) |
| 3310, 3312, 3314 | Soundness agents |

### wait_for_stable Logic
The unified poll loop in agentapi_backend:
- Requires seeing status="running" at least once before accepting "stable" as completion
- Resets inactivity timer every time status != "stable"
- Only times out on SUSTAINED inactivity (agent idle, not just between tool calls)
- Server unreachable → agent crashed → return False

---

## 5. Configuration

### Config JSON structure
Each project now keeps its live config in `lagent.config.json` at the repo root. `configs/extremal_vectors_run.json` remains the source template/example. Key fields:
- `worker`, `easy_worker`, `hard_worker` — ProviderConfig per difficulty
- `reviewer` — ProviderConfig for the reviewer agent
- `verification.correspondence_agents` — list of agents for correspondence checks
- `verification.soundness_agents` — list of agents for soundness checks
- Each agent: `provider`, `model`, `effort`, `fallback_models`, `label`

### Policy JSON (hot-reloadable)
Runtime tuning at `lagent.policy.json` in the project root. Editable while supervisor runs:
- `timing.burst_timeout_seconds` — burst budget hint passed through to backends; it is not a hard completion kill for Codex/script headless
- `difficulty.easy_max_retries` — attempts before auto-elevation
- `prompt_notes.worker` — ad-hoc instructions injected into prompts
- `verification.correspondence_agent_selectors` — ordered hot-settable subset of configured correspondence agents
- `verification.soundness_agent_selectors` — ordered hot-settable subset of configured soundness agents
- `verification.soundness_disagree_bias` — reviewer default on 2-agent soundness splits (`reject` or `approve`)

---

## 6. Cycle Flow in Detail

### Theorem Stating Phase
```
Cycle N:
  1. Worker creates/modifies nodes (.lean + .tex pairs)
    - If theorem_stating is holding on a current soundness target in `repair` mode, only `Tablet/{target}.tex` is writable
    - If correspondence/paper-faithfulness blockers are open, there is no active soundness target for that cycle; the worker is addressing the correspondence frontier instead
     - Broader paper-faithful DAG changes require reviewer-authorized `restructure` mode
  2. Register new nodes in tablet, apply difficulty hints and explicit theorem-stating kind hints (`paper_main_result` vs `paper_intermediate`)
  3. Correspondence + Faithfulness check (3 agents, gate)
     - If REJECT → skip soundness, go to reviewer
     - If APPROVE → proceed to soundness
  4. NL Proof Soundness check for the current target node only (3 agents on that node)
     - Target chosen deterministically in deepest-first DAG order
     - The same target stays active across cycles until its NL proof is accepted, but broad correspondence blockers suspend target-hold behavior until those are resolved
  5. Apply verification results to tablet (sticky per-node status)
  6. Reviewer evaluates, provides guidance
  7. Git commit with cycle tag
  8. If ADVANCE_PHASE → human approval via web viewer
```

### Proof Formalization Phase
```
Cycle N:
  1. Select active node (reviewer's choice from previous cycle)
  2. Route to easy_worker or hard_worker based on difficulty
  3. Worker eliminates sorry from one node
  4. Validation (compilation, imports, declaration integrity)
  5. If easy mode: reject any edit outside the active `.lean` proof body; auto-elevate after 2 fails
  6. Hard mode is still node-centered by default; broader edits to nearby existing nodes require explicit reviewer authorization (`proof_edit_mode: restructure`) for that same active node
  7. NL verification on modified/new nodes
  8. Reviewer evaluates, picks next node
  9. Git commit
```

### Mid-Cycle Resume
`state.resume_from` can be set to:
- `""` — full cycle
- `"verification"` — skip worker
- `"reviewer"` — skip worker + verification (loads saved results)

---

## 7. Management Scripts

```bash
./scripts/status.sh [repo]           # Show everything
./scripts/pause.sh [repo]            # Graceful stop after cycle
./scripts/stop.sh [repo]             # Kill everything
./scripts/rewind.sh 3 verification   # Rewind to cycle 3, verification stage
./scripts/resume.sh /path/to/repo --stop-at-phase-boundary
./scripts/setup_repo.sh /path paper.tex  # New formalization
```

---

## 8. Web Viewer

Dashboard at port 3300. Project-specific viewer JSON now lives under `.agent-supervisor/viewer/` inside each repo; the static web route symlinks into that project-local data.

### Visual encoding
- **Node border**: solid=C pass, dashed=C unknown, dotted=C fail
- **Edge style**: solid=P pass, dashed=P unknown, dotted=P fail (edges from the importing node)
- **Corner shape**: rounded=easy, sharp=hard
- **Prefix**: T:=theorem, D:=definition, L:=lemma
- **Color**: blue=closed, green=recursively closed, yellow=active, grey=open

### Features
- Cycle history slider (loads state from git)
- Pan/zoom (mouse wheel + drag, pinch on mobile)
- Mobile responsive (timeline at top, slide-up detail sheet)
- Human feedback panel at phase boundaries
- Tablet snapshot download (.zip)

---

## 9. Common Debugging

### Agent not producing results
1. Check `scripts/status.sh` — is it running?
2. Check the screen: `curl -s "http://localhost:PORT/internal/screen" -H "Accept: text/event-stream" --max-time 3`
3. Check the agentapi log: `.agent-supervisor/logs/agentapi-reviewer-PORT.log`
4. Check the completion marker: is the agent writing the expected `*.done` file, and did it leave a matching `*.raw.json` in `.agent-supervisor/staging/`?

### Verification always rejects
1. Check the specific issues in `correspondence_result_N.json`
2. Common: Mathlib duplicates, missing quantifiers, weakened statements
3. The reviewer prompt includes verification results — check if guidance is specific enough

### Worker ignoring verification feedback
1. Check the reviewer decision — is it specific about what to fix?
2. The worker doesn't see verification results directly — only the reviewer's guidance
3. Consider human feedback via the web viewer

### Tests failing
```bash
python3 -m pytest tests/ --ignore=tests/test_adapters_live.py -v
```
Key test files:
- `test_agent_dispatch.py` — done_file, effort, timeout regression tests
- `test_difficulty.py` — tiering, git, multi-agent, model fallback
- `test_state.py`, `test_config.py` — serialization, parsing

---

## 10. Extending the System

### Adding a new agent provider
1. Create `agents/new_provider.py` with a `run()` function returning `BurstResult`
2. Add dispatch in `burst.py` `run_worker_burst` and `run_reviewer_burst`
3. Add command building in `agentapi_backend._agent_command` (if using agentapi)
4. Add regression tests in `test_agent_dispatch.py`

### Adding a new verification check
1. Add prompt builder in `prompts.py`
2. Add runner in `cycle.py` (follow `_run_single_soundness_agent` pattern)
3. Wire into `_run_nl_verification` with appropriate gating
4. Add status field to `TabletNode` in `state.py`
5. Update viewer to display the new status

### Modifying the DAG structure
- Nodes are registered via `register_new_node` in `tablet.py`
- Support files (Tablet.lean, INDEX.md) are auto-regenerated
- Git commits capture the full state at each cycle
