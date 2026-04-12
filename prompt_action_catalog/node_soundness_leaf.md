# node_soundness_leaf

- Source prompt: `prompt_catalog/node_soundness_leaf.md`
- Situation: Single-node soundness verification for a leaf helper node.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the displayed node's `.tex` content.
- Read any child-node `.tex` files if needed, though this leaf example has none.

**Apparently Available Actions**
- Judge the node's NL proof as SOUND, UNSOUND, or STRUCTURAL.
- Treat the task as purely mathematical; Lean is irrelevant here.
- Explain the verdict in detail and summarize the approval result.

**Prompt-Instructed Completion Steps**
- Write `nl_proof_floating_note_0.raw.json`.
- Run `check.py soundness-result ... --node floating_note`.
- Write `nl_proof_floating_note_0.done` if the checker passes.
