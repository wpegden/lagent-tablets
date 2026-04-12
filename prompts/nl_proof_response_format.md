=== YOUR RESPONSE ===

Write your assessment as JSON to the raw file `{raw_output_path}`:

{{
  "soundness": {{
    "decision": "PASS" or "FAIL",
    "issues": [{{"node": "name", "description": "..."}}]
  }},
  "overall": "APPROVE" or "REJECT",
  "summary": "brief overall assessment",
  "feedback": "optional short note if the task/setup seems impossible, inconsistent, or poorly supported"
}}

MANDATORY:
1. Write the JSON to `{raw_output_path}`.
2. Run `python3 {check_script} soundness-batch-result {raw_output_path}`.
3. If that passes, write the completion marker `{done_path}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{canonical_output_path}`.
