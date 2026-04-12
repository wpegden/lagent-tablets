# correspondence_basic

- Source prompt: `prompt_catalog/correspondence_basic.md`
- Situation: Basic correspondence / paper-faithfulness verification for one node.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the listed node's `.lean` and `.tex` files and follow its imports.
- Read the cited source-paper excerpt and the full paper on disk as needed.
- Use Loogle to check whether a project-specific definition duplicates a Mathlib concept.

**Apparently Available Actions**
- Judge Lean/NL correspondence for the listed node.
- Judge paper-faithfulness for the listed node relative to the configured targets.
- Check structured provenance for paper-anchored nodes and for any definition node that carries provenance.
- Report current open failures in `correspondence.issues` and `paper_faithfulness.issues`.
- Summarize whether the node should be APPROVE or REJECT overall.

**Prompt-Instructed Completion Steps**
- Write `correspondence_result_0.raw.json`.
- Run `check.py correspondence-result ...`.
- Write `correspondence_result_0.done` if the checker passes.
