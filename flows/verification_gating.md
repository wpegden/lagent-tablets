# Verification Gating

These rules apply inside cycle execution after a valid worker checkpoint.

```text
correspondence + paper-faithfulness
  |
  +--> REJECT
  |      |
  |      v
  |   block soundness
  |   carry/open blockers forward
  |
  +--> APPROVE
         |
         v
      soundness
```

## Theorem Stating

```text
valid theorem-stating worker checkpoint
  |
  v
compute correspondence frontier
  |
  +--> includes unknown / changed / failed correspondence targets
  +--> includes first-class Preamble targets when `Tablet/Preamble.tex` exists
  |
  v
run correspondence on frontier only
  |
  +--> if any correspondence blocker remains
  |      |
  |      v
  |   suspend soundness target
  |
  +--> else
         |
         v
      run soundness on the held target only
```

## Proof Formalization

```text
valid proof worker checkpoint
  |
  v
run correspondence on new nodes + changed active-node interfaces
  |
  v
run soundness only on nodes that remain open
```

## Notes

- Correspondence is the gate for soundness.
- Closed nodes persist `soundness_status = "pass"`.
- Per-node verifier continuity comes from each verifier seeing its own prior results for the same check.
