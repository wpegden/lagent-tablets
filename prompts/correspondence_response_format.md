=== YOUR RESPONSE ===

Write your assessment as JSON to the file `correspondence_result.json`:

{
  "correspondence": {
    "decision": "PASS" or "FAIL",
    "issues": [{"node": "name", "description": "..."}]
  },
  "paper_faithfulness": {
    "decision": "PASS" or "FAIL",
    "issues": [{"node": "name", "description": "..."}]
  },
  "overall": "APPROVE" or "REJECT",
  "summary": "brief overall assessment"
}

MANDATORY: Write the JSON to `correspondence_result.json` then stop.
