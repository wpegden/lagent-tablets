# correspondence_full_context_multiple_changed_nodes

- Source prompt: `prompt_catalog/correspondence_full_context_multiple_changed_nodes.md`
- Situation: Correspondence verification with multiple changed nodes, previous results, and preamble items.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the listed node files plus `Preamble.lean` and `Preamble.tex`.
- Read the cited source-paper excerpts and the full paper on disk as needed.
- Use the old-vs-new change blocks and previous-cycle findings as context while still verifying independently.

**Apparently Available Actions**
- Judge correspondence and paper-faithfulness for each listed node.
- Treat listed `Preamble[...]` items as first-class correspondence targets.
- Decide whether previously flagged issues are genuinely fixed or only superficially changed.
- Report only currently open failures and give an overall APPROVE/REJECT result.

**Prompt-Instructed Completion Steps**
- Write `correspondence_result_2.raw.json`.
- Run `check.py correspondence-result ...`.
- Write `correspondence_result_2.done` if the checker passes.
