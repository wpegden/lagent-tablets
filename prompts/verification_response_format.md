=== YOUR RESPONSE ===

Return a JSON object:
{
  "correspondence": {
    "decision": "PASS" or "FAIL",
    "issues": [{"node": "name", "description": "..."}]
  },
  "paper_faithfulness": {
    "decision": "PASS" or "FAIL",
    "issues": [{"node": "name", "description": "..."}]
  },
  "soundness": {
    "decision": "PASS" or "FAIL",
    "issues": [{"node": "name", "description": "..."}]
  },
  "overall": "APPROVE" or "REJECT",
  "summary": "brief overall assessment",
  "feedback": "optional short note if the task/setup seems impossible, inconsistent, or poorly supported"
}
