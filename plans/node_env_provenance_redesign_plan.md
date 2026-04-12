# Node Environment / Provenance Redesign Plan

## Goals

- Replace theorem-stating `paper_main_result` / `paper_intermediate` guidance with environment-driven node semantics.
- Add ordinary-node TeX environment `helper`.
- Require structured paper provenance for paper-anchored statement nodes.
- Keep helper nodes paper-faithful even though they are not tied to one exact cited paper statement.
- Apply the same node-policy guidance in theorem-stating and proof-formalization via a shared node spec.

## Design

### 1. Environment semantics

- Ordinary nodes may use:
  - `definition`
  - `helper`
  - `lemma`
  - `theorem`
  - `corollary`
- `Preamble.tex` may use:
  - `definition`
  - `proposition`

Interpretation:
- `definition`: concept/interface node
- `helper`: structural auxiliary statement, still paper-faithful
- `lemma` / `theorem` / `corollary`: paper-anchored statement nodes

### 2. Paper provenance

- Structured provenance is stored on tablet nodes as:
  - `start_line`
  - `end_line`
  - optional `tex_label`
- `lemma` / `theorem` / `corollary` require provenance.
- `helper` may optionally carry provenance when that is useful for review context, but it does not require provenance.
- `definition` may have provenance when it really corresponds to a paper definition.

Validation rule:
- if the cited paper line range contains one clear `\label{...}`, `tex_label` must match it

### 3. Orphan policy

- `lemma` / `theorem` / `corollary` are not orphan candidates merely for being leaf nodes
- `helper` and leaf `definition` nodes are orphan candidates unless they gain real downstream use

### 4. Prompting / review

- Shared node rules live in `prompts/node_spec.md`
- Theorem-stating worker/reviewer prompts reference the node spec
- Proof-formalization worker/reviewer prompts reference the node spec
- Proof-formalization may introduce a new paper-anchored statement node when genuinely needed, but that is unusual and must satisfy the full node spec plus coarse-package protections
- Correspondence prompts explicitly verify paper provenance for paper-anchored nodes

### 5. Trust gate

- Human-review trust is attached to paper-anchored statements with structured provenance, not to `paper_main_result`

## Implementation steps

1. Extend TeX env validation and orphan detection with `helper` plus env-based exemption.
2. Replace theorem-stating kind hints/assignments with provenance hints/assignments.
3. Upgrade prompts and skills to the shared node spec.
4. Make correspondence prompts provenance-aware.
5. Move the human trust gate from `paper_main_result` to paper-anchored statements with provenance.
6. Update tests and docs to the new model.
