# State Semantics

This file records the intended meaning of the three node-level notions the
supervisor tracks:

- Lean closure: `node.status`
- Correspondence: `node.correspondence_status`
- NL proof soundness: `node.soundness_status`

It is not a control-flow chart. It is the state model that the cycle flow is
supposed to preserve.

## Three Notions

```text
Lean closure
  = "does the current Lean file stand on its own?"

Correspondence
  = "does the current Lean/NL statement pair match the paper-faithful claim?"

NL proof soundness
  = "while this theorem is still open in Lean, does its NL proof follow from
     its direct children's NL statements?"
```

The intended end state for a theorem/lemma node is:

```text
closed in Lean
  +
correspondence passed
```

Soundness is a scaffold for still-open theorem nodes. Once a node is Lean-closed,
the system treats NL proof soundness as satisfied.

## Lean Closure

```text
worker changes Lean file
  |
  v
closed status becomes untrusted
  |
  v
node must pass the normal deterministic node check again
  |
  +--> pass  -> may become `closed`
  |
  +--> fail  -> remains `open`
```

Meaning:

- `closed` means the current `.lean` declaration is locally Lean-complete.
- `closed` does not mean correspondence passed.
- `closed` does not mean the whole phase is accepted.

Code:

- closure is stored on `TabletNode.status` in [state.py](../lagent_tablets/state.py)
- closure is set by [mark_node_closed](../lagent_tablets/tablet.py)
- reopening is done by [mark_node_open](../lagent_tablets/tablet.py)
- the checked Lean-file hash is stored as `closed_content_hash`
- before any runtime tablet save, [_save_runtime_tablet](../lagent_tablets/cycle.py) calls [_invalidate_closed_node_status](../lagent_tablets/cycle.py)
- that helper reopens any `closed` node whose current `.lean` hash differs from the saved closure hash

This is the key invariant:

```text
if a closed node's Lean file changes, the node must reopen
```

That invariant is global. It is not theorem-stating-specific.

## Correspondence

```text
statement-level source context changes
  |
  v
correspondence becomes unknown
  |
  v
rerun correspondence
```

Meaning:

- correspondence is about statement faithfulness, not proof text
- proof-only `.tex` edits do not reopen correspondence
- Lean-side reopening is semantic, not raw-text based

Code:

- stored on `TabletNode.correspondence_status`
- current correspondence fingerprint comes from [correspondence_fingerprint](../lagent_tablets/nl_cache.py)
- fast text invalidation comes from `correspondence_text_hash`
- application of results happens in [_apply_verification_to_tablet](../lagent_tablets/cycle.py)
- theorem-stating frontier selection is in [_theorem_stating_correspondence_frontier](../lagent_tablets/cycle.py)

The intended correspondence invalidation sources are:

- the node's own `.tex` statement block changed
- `Preamble.tex` changed
- a depended-on definition statement changed
- the semantic Lean meaning of the node's own declaration changed

## NL Proof Soundness

```text
node is still open in Lean
  |
  v
check whether its NL proof follows from its direct children's NL statements
```

Meaning:

- soundness is only needed for still-open theorem/lemma nodes
- closed nodes are treated as `soundness = pass`
- soundness is about NL proof adequacy, not correspondence

Code:

- stored on `TabletNode.soundness_status`
- current soundness fingerprint comes from [soundness_fingerprint](../lagent_tablets/nl_cache.py)
- application of results happens in [_apply_verification_to_tablet](../lagent_tablets/cycle.py)
- closed nodes force soundness display/status to pass in [viewer_state.py](../lagent_tablets/viewer_state.py)

Soundness invalidation rule:

```text
if correspondence changes, prior soundness is stale
```

This is enforced in [_apply_verification_to_tablet](../lagent_tablets/cycle.py),
which clears soundness when a node's correspondence fingerprint changed.

## Phase Semantics

### Theorem Stating

```text
worker may create or reshape the coarse DAG
  |
  v
deterministic validity gate
  |
  v
touch-changed nodes may close locally in Lean
  |
  v
correspondence gate runs first
  |
  +--> blockers open -> soundness target suspended
  |
  +--> blockers clear -> soundness runs on the held target only
```

Important consequences:

- theorem-stating may produce `closed` nodes before correspondence passes
- that is allowed
- but later Lean edits to those nodes must reopen them before state is saved

### Proof Formalization

```text
worker is node-centered
  |
  v
deterministic gate closes active/new nodes that now pass
  |
  v
verification frontier is computed from all changed nodes and their stale
semantic consequences
  |
  v
soundness runs only on nodes that remain open
```

Important consequences:

- proof-formalization can leave a node `closed` while correspondence is still failing
- that is acceptable under the current semantics
- the final accepted state still requires both Lean closure and correspondence pass

### Cleanup

```text
cleanup may restyle Lean only
  |
  v
declaration hash must stay fixed
  |
  v
correspondence fingerprint must stay fixed
```

So cleanup is the strictest phase:

- no `.tex` edits
- no semantic statement drift
- no new or deleted nodes

## Viewer Semantics

The viewer's body color tracks Lean closure, not correspondence.

```text
blue / green = Lean-closed
gray         = Lean-open
border/edge  = correspondence / soundness information
```

So a blue or green node can still be under correspondence failure. That is not,
by itself, a bug.

## Main Remaining Caveat

The semantic model is now aligned across closure, correspondence, and
soundness. The main remaining caveats are operational, not conceptual:

- old repositories may still carry legacy verification hashes until they are
  naturally refreshed by new cycles
- theorem-stating still has its own held soundness-target workflow, which is a
  phase-control concept rather than a verification-scope concept
