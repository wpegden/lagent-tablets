# node_soundness_with_children_and_previous_issues

- Source prompt: `prompt_catalog/node_soundness_with_children_and_previous_issues.md`
- Situation: Single-node soundness verification with children, source-paper context, and prior issues.
- Perspective: cold-reader audit of what the prompt appears to make available.
- Source enforcement note: the supervisor actually enforces only a valid raw artifact plus matching `.done`, then reruns validation itself. Local checker commands listed below are prompt-instructed rather than directly source-proven.

**May Read / Consult**
- Read the displayed node's `.tex` content and its child-node `.tex` statements.
- Read the relevant source-paper excerpt.
- Use the previous-cycle issue note as context while still verifying independently.

**Apparently Available Actions**
- Judge the node's NL proof as SOUND, UNSOUND, or STRUCTURAL.
- Decide whether the prior soundness issue is genuinely fixed.
- Explain the verdict in detail and summarize the approval result.

**Prompt-Instructed Completion Steps**
- Write `nl_proof_main_result_part_b_0.raw.json`.
- Run `check.py soundness-result ... --node main_result_part_b`.
- Write `nl_proof_main_result_part_b_0.done` if the checker passes.
