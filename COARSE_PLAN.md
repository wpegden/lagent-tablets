# Coarse DAG Gating Plan

## Goal

Add a new proof-formalization invariant:

- when theorem-stating is accepted and the project enters `proof_formalization`,
  every existing non-preamble node becomes part of an accepted **coarse package**
- ordinary proof-formalization may prove those coarse nodes in Lean and may add
  non-coarse helper nodes under them
- ordinary proof-formalization may **not** mutate the accepted coarse package
- mutating the accepted coarse package requires a distinct reviewer-authorized
  mode: `coarse_restructure`

This is intentionally stricter than the existing hard-mode `restructure`.

## Core semantics

### 1. What counts as a coarse node

On the theorem-stating -> proof-formalization phase transition:

- every existing non-preamble node is marked `coarse = true`
- each coarse node gets a persisted coarse-package fingerprint

Later, after a successful `coarse_restructure`:

- all previously coarse nodes remain coarse
- every node newly created during that successful `coarse_restructure` becomes coarse
- existing non-coarse helpers do **not** automatically become coarse just because a
  coarse node imports them

This keeps coarse membership explicit and monotone, without making the first helper
lemma under a coarse node require a coarse restructure.

### 2. What ordinary proof-formalization may still do

Ordinary proof-formalization on coarse nodes remains useful and should stay easy to
reason about.

Allowed in ordinary `local` / `restructure` proof-formalization:

- edit Lean proof bodies of coarse nodes
- add imports from a coarse node to new or existing **non-coarse** helpers
- create new non-coarse helper nodes
- refactor non-coarse helper structure subject to the existing hard-mode scope rules

Not allowed without `coarse_restructure`:

- change the Lean declaration interface of a coarse node
- change the `.tex` statement block of a coarse node
- rename or delete a coarse node
- change the coarse-to-coarse dependency structure
- otherwise mutate the accepted coarse paper-facing package

### 3. How to detect coarse-package mutation

Persist, per coarse node, a fingerprint of the accepted coarse interface. That
fingerprint should be stable under Lean proof-body changes and helper-import
additions, but should change when the accepted coarse package changes.

Fingerprint inputs per coarse node:

- node kind
- correspondence / declaration-level semantic fingerprint of the node itself
- `.tex` statement block (not proof body)
- direct imports to other coarse nodes only

This means:

- Lean proof-body edits do not change the coarse fingerprint
- adding a helper import does not change it if the helper is non-coarse
- changing statement/interface or coarse-to-coarse structure does change it

### 4. New proof edit mode

Extend `proof_edit_mode`:

- `local`
- `restructure`
- `coarse_restructure`

Rules:

- `coarse_restructure` is only valid for hard nodes
- it must be explicitly authorized by the reviewer
- it should remain centered on the current active node and use the same
  target-centered impact region machinery as ordinary proof `restructure`
- it is the only mode allowed to mutate the accepted coarse package

### 5. Additional verification for coarse_restructure

After the usual proof-worker deterministic checks and ordinary node-level NL
verification complete successfully:

- run an additional **coarse-wide correspondence / paper-faithfulness sweep**
  over the full new coarse package
- this sweep should check:
  - all previously coarse nodes
  - plus all nodes newly created in this coarse_restructure cycle
- do **not** automatically run full coarse-wide soundness; keep this gate focused
  on paper-facing package integrity

Only after that sweep is accepted should the coarse package be refreshed and the
newly created nodes be promoted to `coarse = true`.

### 6. Interaction with human-reviewed paper statements

The existing human-reviewed paper-statement gate remains stronger and separate.

If a `coarse_restructure` causes a trusted paper-anchored `theorem`/`lemma`/`corollary` node to lose
correspondence, the existing human re-review gate must reopen after the automated
coarse-wide correspondence sweep.

## Implementation steps

### Step A. Persist coarse metadata on tablet nodes

Files:

- `lagent_tablets/state.py`
- `lagent_tablets/tablet.py`

Changes:

- add `coarse: bool`
- add `coarse_content_hash: str`
- optionally add `coarse_verified_at_cycle: Optional[int]` for debugging/history
- ensure JSON round-trips preserve these fields

Helpers in `tablet.py`:

- `coarse_node_names(tablet) -> set[str]`
- `coarse_interface_fingerprint(tablet, repo_path, node_name, coarse_names=None) -> str`
- `freeze_current_coarse_package(tablet, repo_path, cycle)`
- `refresh_coarse_package_hashes(tablet, repo_path, cycle, new_coarse=None)`
- `coarse_package_node_names_after_restructure(tablet, newly_created)`

### Step B. Freeze the coarse package on theorem-stating exit

Files:

- `lagent_tablets/cli.py`

Changes:

- when human approval advances `theorem_stating -> proof_formalization`,
  freeze the current coarse package before changing phases
- save the updated tablet/state immediately

### Step C. Extend proof edit modes and prompts

Files:

- `lagent_tablets/state.py`
- `lagent_tablets/check.py`
- `lagent_tablets/prompts.py`
- `prompts/reviewer_instructions.md`
- `prompts/worker_instructions.md`
- `prompts/worker_restructure_instructions.md`
- new template: `prompts/worker_coarse_restructure_instructions.md`
- `AGENTS.md`

Changes:

- allow reviewer decisions to use `proof_edit_mode = coarse_restructure`
- update reviewer instructions so this is high-bar and only for mutating the
  accepted coarse package itself
- update worker prompts:
  - ordinary hard local/restructure must say coarse-node statement/package changes
    require `coarse_restructure`
  - coarse-restructure prompt must explain:
    - accepted coarse package mutation is authorized
    - scope is still the active node’s impact region
    - success will trigger a coarse-wide correspondence sweep

### Step D. Add canonical coarse-package deterministic checks

Files:

- `lagent_tablets/check.py`
- `lagent_tablets/cycle.py`

Changes:

- extend the proof scope payload to include:
  - coarse nodes
  - saved coarse fingerprints
- add a canonical `check_coarse_package_guard(...)` in `check.py`
- call it from:
  - `check_proof_hard_scope(...)`
  - `check_proof_worker_delta(...)`

Ordinary `local` / `restructure` behavior:

- reject `.tex` edits to coarse nodes
- reject mutation of any non-active coarse node
- reject active-node coarse-package mutation unless it is merely proof-body /
  helper-import safe

`coarse_restructure` behavior:

- skip the immutability rejection
- still enforce the existing impact-region scope rules

### Step E. Run the additional coarse-wide correspondence sweep

Files:

- `lagent_tablets/cycle.py`

Changes:

- in proof-formalization, after the normal proof worker validation and ordinary
  NL verification, if `proof_edit_mode == coarse_restructure` and the worker made
  progress:
  - compute the candidate new coarse set:
    - existing coarse nodes
    - plus nodes newly created this cycle
  - run `_run_nl_verification(...)` again with:
    - `correspondence_node_names = candidate coarse set`
    - `soundness_node_names = []`
  - append those results to the cycle’s verification results
- only if that coarse-wide sweep is acceptable should the supervisor refresh
  coarse fingerprints and promote newly created nodes to coarse

If the sweep rejects:

- leave the old coarse fingerprints in place
- do not promote new nodes to coarse yet
- let the reviewer see the failure and decide what to do next

### Step F. Make the new mode sticky only when explicitly authorized

Files:

- `lagent_tablets/cycle.py`

Changes:

- reviewer application logic should:
  - set `state.proof_target_edit_mode = coarse_restructure` only when:
    - requested by reviewer
    - same active node
    - hard node
  - otherwise fall back to `local` or `restructure` as appropriate

### Step G. Tests

#### State / serialization

- `tests/test_state.py`
  - round-trip `TabletNode.coarse`
  - round-trip `TabletNode.coarse_content_hash`
  - round-trip `SupervisorState.proof_target_edit_mode = coarse_restructure`

#### Prompt tests

- `tests/test_prompts.py`
  - reviewer prompt advertises `coarse_restructure`
  - hard worker prompt for ordinary mode says coarse package changes require
    `coarse_restructure`
  - hard worker prompt in `coarse_restructure` mode loads the new instructions

#### Deterministic checker tests

- `tests/test_check.py`
  - active coarse node may change Lean proof and add helper import without failing
  - active coarse node `.tex` change is rejected in ordinary mode
  - non-active coarse node modification is rejected in ordinary `restructure`
  - `coarse_restructure` allows coarse-node package edits inside impact region
  - reviewer decision validator accepts `proof_edit_mode = coarse_restructure`

#### Cycle tests

- `tests/test_cycle.py`
  - theorem-stating -> proof-formalization freeze marks all current nodes coarse
  - proof reviewer can authorize `coarse_restructure`
  - proof cycle in `coarse_restructure` triggers a second coarse-wide
    correspondence sweep
  - newly created nodes become coarse only after a successful coarse-wide sweep
  - trusted main-result human gate still reopens if a coarse-restructure changes
    a trusted main result

## Definition of done

This item is done only if all of the following are true:

- theorem-stating exit freezes the coarse package automatically
- ordinary hard proof work can still add helper nodes under coarse nodes
- ordinary hard proof work cannot silently mutate the accepted coarse package
- `coarse_restructure` exists, is reviewer-authorized, and is clearly surfaced
  in prompts/state
- successful coarse-restructure triggers a coarse-wide correspondence sweep
- newly created nodes become coarse only after that successful sweep
- the focused and broader regression suites pass
