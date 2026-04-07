# lagent-supervisor: Complete System Specification

Version: 3.0 (DAG model)
Date: 2026-04-06
Status: Design specification for clean rewrite

---

## 1. Purpose

lagent-supervisor is a long-running orchestrator that drives LLM agents (Claude, Codex, Gemini) through a multi-phase workflow to formalize mathematical papers into verified Lean 4 / mathlib proofs. It manages:

- A **5-phase workflow** from paper comprehension to verified proof
- A **proof tablet**: a DAG of nodes with explicit Lean imports, each carrying an NL statement, Lean statement, and (for open nodes) an NL proof
- **Worker/reviewer/verification-model agent cycles** with structured JSON handoffs
- **Branching** to explore alternative proof strategies in parallel
- **Checkpointing and recovery** for crash resilience
- **Web viewers** for monitoring chat logs and tablet state
- **Multi-user execution** (mandatory) where the supervisor and agents run as different OS users

---

## 2. Workflow Phases

| # | Phase | Purpose |
|---|-------|---------|
| 1 | `paper_check` | Read and mathematically verify the source paper. Produce `PAPERNOTES.md`. |
| 2 | `planning` | Create a high-level proof roadmap in `PLAN.md`. |
| 3 | `theorem_stating` | Formalize paper-facing theorem statements as Lean declarations. Cut up the source paper's proofs into per-node `.tex` files with rigorous NL proofs. Produce `tablet_manifest.json`. |
| 4 | `proof_formalization` | Prove all theorems via the proof tablet. The core phase. |
| 5 | `proof_complete_style_cleanup` | Eliminate linter warnings without regressions. Supervisor enforces: every closed node stays closed. |

Phase transitions are decided by the **reviewer agent** via the `ADVANCE_PHASE` decision. The supervisor validates preconditions before allowing transitions.

### 2.1 `theorem_stating` Phase Mechanics

During `theorem_stating`, the supervisor does NOT manage the tablet. It runs standard worker/reviewer cycles. The worker works freely in the repo.

**Worker's job:**
1. Read the source paper and `PAPERNOTES.md`
2. Identify main results and intermediate results
3. Create `Tablet/*.tex` files by cutting up the source paper's proofs -- copy relevant LaTeX, fill in implicit arguments, make every proof rigorous
4. Write Lean declarations in scratch files and test with `lake build`
5. Write `Preamble.tex` listing external results used in NL proofs
6. Write `tablet_manifest.json` with structural metadata and finalized Lean statements
7. Run `validate_manifest.sh` before requesting ADVANCE_PHASE

**At transition:** The supervisor validates the deliverables, invokes the verification model, and creates the official tablet (Section 4.11).

### 2.2 `proof_complete_style_cleanup` Phase Mechanics

Style cleanup eliminates Lean linter warnings without any regression.

**Invariant:** Every node closed at the start of cleanup must remain closed throughout. Any regression triggers immediate rollback.

**Worker scope:** May edit proof bodies in any closed node. May edit `Preamble.lean` imports. No new nodes, no statement changes, no `.tex` changes.

**Supervisor enforcement:**
1. Before each cycle: record the set of closed nodes (cleanup baseline)
2. After each burst: run `check_tablet.sh` on every node in the baseline
3. If any baseline node is now broken: `git reset --hard` to `cleanup_last_good_commit`, force-push, stop
4. If all closures preserved: commit, update `cleanup_last_good_commit`, continue

---

## 3. Cycle Structure

During `proof_formalization`, each cycle has a **worker stage** and a **reviewer stage**. There is no mode distinction -- every cycle follows the same flow. The worker may prove nodes and create helpers in a single cycle.

### 3.1 Worker Stage

The reviewer has assigned an **active node** for the worker to focus on.

**Worker's options (within a single cycle):**
- **Prove the active node:** Edit the proof body to eliminate sorry. Add imports as needed.
- **Create helpers:** Write new `Tablet/{name}.lean` and `Tablet/{name}.tex` files for helper lemmas, add them as imports to the active node, update the active node's NL proof.
- **Both in one cycle:** Create a helper AND close it in the same cycle (small technical lemmas).
- **Update STRATEGY:** Document approach, blockers, failed attempts in the STRATEGY comment block.

**What the worker may edit:**
- The active node's `.lean` file: imports, STRATEGY comment, proof body. NOT the declaration signature.
- `Preamble.lean`: add imports only (allowed prefixes)
- Create new `.lean` and `.tex` files for new nodes
- The active node's `.tex` file (update NL proof to reference new helpers)
- `.tex` files for new nodes

**What the worker produces:**
- `worker_handoff.json`: summary of changes, status (NOT_STUCK / STUCK / DONE / NEED_INPUT)

### 3.2 Supervisor Validation

**Pre-burst permission setup** (multi-user mode, mandatory):
- Set the active node's `.lean` and `.tex` group-writable
- Set `Tablet/Preamble.lean` group-writable
- Set ALL other files read-only for burst_user (other nodes, `Axioms.lean`, `lakefile.toml`, `lean-toolchain`, etc.)
- The `Tablet/` directory is group-writable (worker can create new files)

This prevents scope violations mechanically -- the OS enforces it.

**Post-burst validation** (what permissions can't enforce):
1. **Active node declaration signature:** Hash-compare the declaration line against stored hash. Reject if changed.
2. **Import validation:** All imports in the active node match `Tablet.*` or allowed prefixes.
3. **Forbidden keyword scan:** Scan the active node's `.lean` (masked for comments/strings).
4. **Per-node compilation:** Delete the active node's `.olean`, run `lake env lean Tablet/{name}.lean`. Scan output for sorry warnings.
5. **Preamble check:** If `Preamble.lean` changed, verify only import additions with allowed prefixes.
6. **New node validation:** For each newly created file pair in `Tablet/`:
   - Both `.lean` AND `.tex` must exist
   - `-- [TABLET NODE: {name}]` marker matches the filename AND the declaration name
   - Declaration name is unique, valid Lean identifier
   - Lean statement compiles (with sorry)
   - `.tex` file follows format (Section 4.5)
7. **`lake build Tablet`:** Only run when new nodes were created. Skip for pure close cycles (step 4 suffices).
8. **Rollback on failure:** If validation fails and new files were created, the supervisor deletes them before bouncing back to the worker.

**If any new nodes were created that remain open**, the supervisor invokes the **verification model** (Section 5) to check:
- NL/Lean statement correspondence for new nodes
- Paper-faithfulness (are these genuine intermediate steps, not churn?)
- NL proof soundness for open nodes (the active node's updated NL proof, plus new open nodes' NL proofs)

**Exception:** Nodes created AND closed (sorry-free) in the same cycle do not require NL proofs or NL verification. The Lean proof suffices.

**If all checks pass:**
- New nodes are registered in `tablet.json`
- Nodes whose sorry was eliminated are marked `closed`
- Commit, checkpoint, proceed to reviewer

**If checks fail:** The burst is bounced back to the worker with corrective feedback (up to `validation_retry_limit`).

### 3.3 Reviewer Stage

The reviewer sees:
- Worker handoff (summary, status)
- Terminal output (trimmed)
- Tablet status: all nodes with status, dependency structure, STRATEGY summaries
- Validation results
- Verification model results (if new nodes were created)

The reviewer produces `reviewer_decision.json`:
- `decision`: CONTINUE | ADVANCE_PHASE | STUCK | NEED_INPUT | DONE
- `confidence`: float
- `reason`: string
- `next_prompt`: guidance for the worker's next cycle
- `next_active_node`: which node the worker should focus on next
- `suggest_branch`: bool (explicit signal, no keyword parsing)

### 3.4 Deterministic CLOSE Bypass

When a cycle succeeds (active node closed, no new nodes created, purely mechanical), the supervisor MAY auto-advance to the next open node without consulting the reviewer. The reviewer is invoked every `close_bypass.reviewer_interval` successful closes (configurable, default 5) to assess strategic direction.

---

## 4. The Proof Tablet

### 4.1 Core Concept

The proof tablet is a collection of **nodes**, each represented by a pair of files:

- `Tablet/{name}.lean` -- a single Lean declaration with either a complete proof or `sorry`
- `Tablet/{name}.tex` -- the corresponding NL statement and (for open nodes) NL proof

Dependencies between nodes are expressed as **Lean imports**. Lean enforces acyclicity and resolves transitive dependencies. The supervisor does not maintain any graph data structure -- Lean/Lake IS the dependency manager.

### 4.2 Invariants

1. **Paired statements:** Every node has both an NL statement (`.tex`) and a Lean statement (`.lean`). The verification model has confirmed these correspond.
2. **NL proofs for open nodes:** Every open node (has sorry) has an NL proof in its `.tex` file that argues from the NL statements of its imported nodes.
3. **Import-tracked NL proofs:** Each NL proof may only reference NL statements of nodes whose `.lean` file it imports. The verification model checks this.
4. **Exception for same-cycle closures:** A node created and closed in the same cycle does not require an NL proof.
5. **Closed nodes are locally sound:** A closed node's `.lean` compiles, has no sorry, no forbidden keywords. Closure is purely local -- imported dependencies may still be open.
6. **Build succeeds:** `lake build Tablet` always succeeds.

### 4.3 Closure Semantics

A node is **closed** when its own `.lean` file compiles, contains no `sorry`, and no forbidden keywords. Closure is purely local: a closed node MAY import open nodes. "Theorem A holds assuming Lemma B" is a valid proof of A. The project is done when ALL nodes are closed.

Workers can close nodes in any order -- top-down or bottom-up.

### 4.4 Node File Format (`.lean`)

```lean
import Tablet.compactness_of_K
import Tablet.compact_operator_bound
import Mathlib.Topology.Basic

-- [TABLET NODE: uniqueness_thm]
-- Do not rename or remove the declaration below.

/- STRATEGY
  Planned approach: Use compactness_of_K to extract convergent subsequence,
  then apply compact_operator_bound for the fixed-point condition.
  
  [Waiting for]
  - compact_operator_bound: need the L^2 bound
  
  [Failed attempts]
  - Direct epsilon-delta (cycle 34): couldn't control error near boundary
-/

theorem uniqueness_thm (X : ConvexSet α) (hX : StrictConvex X) : ... :=
sorry
```

**Worker-editable:** Imports, STRATEGY comment, proof body.

**Supervisor-controlled:** `-- [TABLET NODE: {name}]` marker and the declaration line.

To modify a statement, the worker requests it through the handoff. The supervisor regenerates the declaration line, resets proof to sorry, and the verification model re-checks NL/Lean correspondence.

### 4.5 Node File Format (`.tex`)

**Open nodes:** Statement environment + proof environment. `\noderef{name}` must reference imported nodes. No placeholder language.

**Closed nodes:** Statement required, proof optional (kept if present, not verified).

**Preamble:** Zero or more `proposition` environments listing external results.

### 4.6 `\noderef` Validation

For OPEN nodes: referenced name must exist in tablet AND be in the node's import closure.

### 4.7 STRATEGY Comment Block

Optional `/- STRATEGY ... -/`. Included raw in the reviewer prompt. Not parsed structurally by the supervisor.

### 4.8 Preamble (`Tablet/Preamble.lean`)

Always closed. Workers add imports during any cycle. No declarations.

### 4.9 Approved Axioms (`Tablet/Axioms.lean`)

Read-only. Generated at setup. Any `axiom`/`constant` not in this file is forbidden.

### 4.10 Dependency-Aware Invalidation

When a statement is modified: regenerate declaration, reset to sorry, `lake build Tablet` finds failures in the import cone. Failed nodes marked open.

### 4.11 Initial Tablet Seeding

At transition: supervisor validates `.tex` files and manifest, invokes verification model, generates `.lean` files (marker + imports + declaration + sorry), writes `tablet.json`, generates support files, verifies `lake build Tablet`.

### 4.12-4.17

Node naming (valid Lean identifiers, unique). Node kinds (preamble, paper_main_result, paper_intermediate, helper_lemma). Active node selection (reviewer chooses, fallback alphabetical). INDEX.md and README.md (supervisor-generated, read-only). Metrics. LaTeX header with `\noderef`.

---

## 5. NL Verification

Strongest available model, explicit thinking directives, configured independently.

### 5.1 When Invoked

When new open nodes are created, when open NL proofs are updated, at seeding. NOT for mechanical closures or same-cycle closures.

### 5.2 Checks (combined into single LLM call)

**A: NL/Lean correspondence.** **B: Paper-faithfulness** (churn prevention). **C: NL proof soundness** -- checks proofs only reference NL statements of imported nodes.

### 5.3 Context Management

Priority-ordered budget (`verification.max_context_tokens`): (1) checked nodes, (2) direct imports' NL statements, (3) transitive imports, (4) importing nodes, (5) paper sections, (6) closed nodes' Lean proofs, (7) STRATEGY blocks.

### 5.4 Output

```json
{"correspondence": {"decision": "PASS|FAIL", "issues": [...]}, "paper_faithfulness": {"decision": "PASS|FAIL", "issues": [...]}, "soundness": {"decision": "PASS|FAIL", "issues": [...]}, "overall": "APPROVE|REJECT", "summary": "..."}
```

---

## 6. Branching

Reviewer sets `suggest_branch: true`. Branch strategy review -> episode creation (git worktrees, child supervisors) -> monitoring -> auto-pruning -> selection review -> winner adopted. Post-MVP feature.

---

## 7. Stuck Recovery

STUCK -> record attempt -> recovery LLM burst -> suggestion injected into next prompt -> exhausted = stop -> non-STUCK clears counter.

---

## 8. Provider Adapters

| Provider | Mode | Stall Recovery |
|----------|------|----------------|
| Codex | Non-interactive (`codex exec --json`) | Process timeout |
| Claude | Interactive tmux (`--dangerously-skip-permissions --model --effort`) | Esc -> reprompt -> kill+resume |
| Gemini | Interactive tmux (`--approval-mode=yolo --model`) | Esc -> reprompt -> kill+resume |

Stall threshold: 15 min no output. Max 3 recoveries per burst. Config re-read each cycle; provider changes kill old session.

---

## 9. Burst Execution

Non-interactive: spawn -> poll -> capture JSONL. Interactive: tmux window -> load-buffer prompt -> monitor completion+stalls -> capture. Retries: budget errors (15-min, max N), productive failures (fast), stalls (resume), other (escalating). Validation retries: corrective prompt -> relaunch.

---

## 10. Configuration

### 10.1 Main Config

`burst_user` required. Providers per role with model/effort. Verification model configured independently. `allowed_import_prefixes`, `forbidden_keyword_allowlist`. Policy path.

### 10.2 Policy

Hot-reloadable. Stuck recovery limits, branching params, timing (burst timeout, stall threshold, retry delays), budget pause, close bypass interval, prompt notes per role.

### 10.3 Hot-Reloadability

Policy, config, prompts, scripts, adapter params: re-read from disk each cycle. Code changes: cycle-boundary restart.

---

## 11. State

`state.json`: cycle, phase, active_node, handoff, review, review_log, validation, stuck recovery, human input, cleanup commit, token usage.

`tablet.json`: nodes dict keyed by name (kind, status, title, provenance, statement hash, closed_at_cycle), active_node, seeded_at_cycle, metrics.

Atomic writes, file locking, permission normalization.

---

## 12-17. Infrastructure

**Validation:** Declaration hash, import patterns, forbidden keywords, per-node build, preamble check, new node validation, full build (when structural changes). NL verification conditional.

**Checkpointing:** Per-cycle snapshots. Atomic restore. Interrupted burst recovery.

**Chat events:** Chunked JSONL. Static web viewer.

**Tablet viewer:** Dependency table, expandable cards, history slider.

**Permissions:** Multi-user mandatory. State dirs 0o2775, state files 0o640, logs 0o664, checkpoints 0o2755, repo 0o2775.

**Git:** Per-cycle commit+push. Cleanup rollback.

---

## 18. Prompt Design

**Worker:** Goal, plan, tasks, reviewer guidance, active node context (.tex + .lean + STRATEGY), tablet status, tool paths (check_node.sh, check_tablet.sh), rules, validation summary.

**Reviewer:** Goal, plan, tasks, handoff, output, tablet status+metrics, validation/verification results, decision schema (decision, next_active_node, next_prompt, suggest_branch).

**Verification:** Combined call. Node NL+Lean statements, imported NL statements, paper sections, STRATEGY blocks. Thinking directive. No operational context.

---

## 19. Lean Source Analysis

Sorry scan (masked source + build output). Forbidden keywords (configurable allowlist). Comment/string masking (state machine with nesting). Import validation. Per-node `.olean` clearing.

---

## 20-21. Budget Monitoring and Scripts

Codex budget monitoring with configurable pause. Worker-facing scripts: `check_node.sh`, `check_tablet.sh`, `validate_manifest.sh`. Operational: monitor, health check, restart, checkpoint restore.

---

## 22. Design Considerations

**Lean as dependency manager:** No supervisor graph logic. Lake handles acyclicity and incremental builds. Zero-disruption node creation. Efficient invalidation (dependency cone only).

**Anti-cheat:** Permissions for scope. Content checks for keywords/imports/signatures. File pair integrity. Filesystem authoritative.

**Import-tracked dependencies:** Workers track deps via imports even for open nodes. `paper_main_result` imports its `paper_intermediate` deps from the start. NL proofs may only reference imported nodes' NL statements. Orphan detection: non-main-result nodes must be imported by something.

**Progress metrics (informational):** Close rate, consecutive failures, open/closed/orphan counts, cycles since last closure. Context for reviewer, not hard gates.

---

## 23. Rewrite Goals

1. Modular architecture: tablet.py, verification.py, prompts.py, burst.py, cycle.py, branching.py, web.py, config.py, state.py, cli.py
2. Typed state (dataclasses)
3. Explicit imports (no wildcards)
4. Subprocess safety (timeouts)
5. Atomic operations
6. Testable (dependency injection)
7. Budget retry limits
8. Lean as dependency manager
