# verification_wrapper_compat

- Source prompt: `prompt_catalog/verification_wrapper_compat.md`
- Situation: Backward-compatible combined verification wrapper for correspondence/paper-faithfulness.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the listed node `.lean` and `.tex` files and follow imports.
- Read the cited source-paper excerpts and the full paper on disk as needed.
- Use Loogle to check for duplicated Mathlib concepts if needed.

**Apparently Available Actions**
- Judge correspondence and paper-faithfulness for the listed nodes.
- Check provenance for paper-anchored nodes and for any definition node with structured provenance.
- Report only currently open failures and give an overall APPROVE/REJECT result.

**Prompt-Instructed Completion Steps**
- Write `correspondence_result.raw.json`.
- Run `check.py correspondence-result ...`.
- Write `correspondence_result.done` if the checker passes.
