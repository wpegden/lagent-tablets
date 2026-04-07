"""Agent communication backends.

Each backend handles one way of talking to an agent CLI.
All backends implement the same interface: send a prompt, get a result.

Backends:
- codex_headless: Script-based `codex exec` (proven reliable)
- agentapi: HTTP wrapper for Claude and Gemini via agentapi server
- script_headless: Generic script-based `-p` mode (fallback for any provider)

The supervisor doesn't know which backend is used -- it just calls
run_worker_burst() and run_reviewer_burst() from burst.py, which
dispatches to the right backend based on the provider config.
"""
