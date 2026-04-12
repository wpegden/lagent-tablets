# correspondence_single_changed_node

- Source prompt: `prompt_catalog/correspondence_single_changed_node.md`
- Situation: Correspondence verification for one node with old-vs-new context and prior findings.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the listed node's `.lean` and `.tex` files and follow imports.
- Read the cited paper excerpt and the full paper on disk as needed.
- Use the old-vs-new change context and previous-cycle findings as context while still verifying independently.

**Apparently Available Actions**
- Judge correspondence and paper-faithfulness for the listed node.
- Check whether the new version really fixes the prior issue or just rephrases it.
- Report only currently open failures and give an overall APPROVE/REJECT result.

**Prompt-Instructed Completion Steps**
- Write `correspondence_result_1.raw.json`.
- Run `check.py correspondence-result ...`.
- Write `correspondence_result_1.done` if the checker passes.
