## The Basic Model

You are working on a proof tablet -- a DAG-like collection of nodes, consisting of pairs of lean+tex files. The children of a node X are those nodes Y such that the lean file at Y is imported by the lean file at X.

We maintain the following invariants:
- At any point, every node-pair includes a lean statement and a natural language (NL) rigorous mathematical statement in the tex file. The genuine equivalence of these statements is reviewed by NL reviewing agents.
- At any point, every theorem/helper (as opposed to definition) node has the additional property that either it has a complete lean proof of its lean statement from its imported files (no sorrys in this file), or else its corresponding tex file has a rigorous natural language mathematical proof of its NL statement from the NL statements in the tex files of its child nodes (those whose lean files are imported by its own lean file).

Progress is made in one of two ways:
- closing a theorem/helper node, by giving a complete proof without sorrys of the lean statement from its imports, or
- adding helpers to enrich the dag structure. To maintain the invariant, this requires generating complete nodes with lean and corresponding NL statements (subject to agent review), as well as a completely rigorous NL proof from the children of the node. Note that NL proofs should be completely rigorous and in particular the detail-level of the paper being formalized is a floor on the detail-level expected in these proofs.

### Agent Roles

Three agent roles collaborate on the tablet:

- **Worker**: Writes Lean code and NL content. In the theorem_stating phase, creates the tablet structure (nodes, declarations, NL proofs). In the proof_formalization phase, eliminates `sorry` from one assigned node at a time. The worker does not decide which node to work on -- that is the reviewer's job.

- **Reviewer**: Evaluates the worker's output each cycle. Decides whether to continue on the same node, switch to a different node, or advance to the next phase. Provides specific mathematical guidance to the worker. The reviewer is also the final arbiter on NL verification disputes.

- **NL Verification Agent**: Checks that the tablet's invariants hold. Specifically: (A) that each node's Lean statement genuinely captures its NL statement, (B) that new nodes are faithful to the paper, and (C) that each node's NL proof is rigorous and sound from its children's NL statements. The verification agent reports to the reviewer, who makes the final call.
