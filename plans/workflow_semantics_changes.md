# Workflow Semantics Change Log

Track only changes that may affect supervisor or workflow semantics rather than pure isolation/runtime containment.

If a change is strictly about filesystem isolation, runtime snapshotting, or launch plumbing, it does not belong here.

## Entries

- Replaced theorem-stating node classification guidance (`paper_main_result` / `paper_intermediate`) with environment-driven semantics plus structured paper provenance.
- `helper` nodes may now carry optional paper provenance; it is not required, but when present it is line-range/label validated like other provenance.
- Proof-formalization no longer hard-bans new paper-anchored theorem/lemma/corollary nodes. They are still unusual, must satisfy the full node spec with structured provenance, and remain subject to the coarse-package guard.
- The accepted coarse theorem-stating package remains specially protected in proof_formalization:
  - ordinary proof work may add non-coarse helpers beneath it
  - mutating accepted coarse-node statements, `.tex`, or coarse-to-coarse structure still requires `proof_edit_mode: "coarse_restructure"`
  - newly created proof-phase nodes do not join the coarse package unless a successful coarse-wide refresh occurs in `coarse_restructure`
- Superseded the recent env-based human gate over all paper-anchored theorem/lemma/corollary nodes.
  - Human review now keys off explicit `workflow.main_result_labels`.
  - Each configured label may be covered by one or more non-`helper` nodes via `paper_provenance.tex_label`.
  - Trusted human review snapshots are now stored and compared per configured label, not per env bucket.
- Superseded the recent env-based orphan protection.
  - DAG necessity is now judged by support closure from the configured main-result targets.
  - Non-target nodes are justified only if they lie in the dependency closure of at least one covered target.
  - Support-closure pruning is suspended until every configured target label has at least one non-`helper` covering node.
- Setup now resolves and persists the target label set.
  - `scripts/setup_repo.sh` accepts explicit `--main-result-labels`.
  - If omitted, setup infers all labeled paper `theorem`/`corollary` statements and writes the resolved list into config.
