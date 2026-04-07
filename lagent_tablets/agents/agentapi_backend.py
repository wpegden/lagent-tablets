"""AgentAPI backend: HTTP wrapper for Claude and Gemini.

Uses agentapi (https://github.com/coder/agentapi) to run agents in
interactive PTY mode behind a clean HTTP API.

The supervisor:
1. Starts an agentapi server per agent (on unique ports)
2. POSTs messages to /message
3. Polls /status for completion (status: "stable" = agent idle)
4. GETs /messages for conversation history

This handles TUI dialogs, prompt delivery, and output capture
automatically via agentapi's PTY management.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.adapters import BurstResult, ProviderConfig

WORKER_PATH = "/home/leanagent/.local/bin:/home/leanagent/.elan/bin:/home/leanagent/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"
WORKER_ELAN_HOME = "/home/leanagent/.elan"

# Port allocation: each agent gets a unique port
# Worker: 3284, Reviewer: 3285, Verification: 3286
PORT_MAP = {"worker": 3284, "reviewer": 3285, "verification": 3286}


def _agent_env(burst_user: Optional[str]) -> str:
    """Build the env vars string for sudo."""
    parts = [f"PATH={shlex.quote(WORKER_PATH)}"]
    if burst_user:
        parts.append(f"HOME=/home/{shlex.quote(burst_user)}")
    parts.append(f"ELAN_HOME={shlex.quote(WORKER_ELAN_HOME)}")
    # Forward API keys
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(key)
        if val:
            parts.append(f"{key}={shlex.quote(val)}")
    return " ".join(parts)


def _agent_command(config: ProviderConfig) -> List[str]:
    """Build the agent CLI command."""
    if config.provider == "claude":
        cmd = ["claude", "--dangerously-skip-permissions"]
        if config.model:
            cmd.extend(["--model", config.model])
        if config.effort:
            cmd.extend(["--effort", config.effort])
        cmd.extend(config.extra_args or [])
        return cmd

    if config.provider == "gemini":
        cmd = ["gemini", "--approval-mode=yolo"]
        if config.model:
            cmd.extend(["--model", config.model])
        cmd.extend(config.extra_args or [])
        return cmd

    raise ValueError(f"agentapi backend does not support provider: {config.provider}")


def _is_server_running(port: int) -> bool:
    """Check if an agentapi server is running on the port."""
    try:
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{port}/status", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        return resp.status == 200
    except Exception:
        return False


def start_server(
    config: ProviderConfig,
    *,
    role: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    port: Optional[int] = None,
    initial_prompt: Optional[str] = None,
) -> int:
    """Start an agentapi server for the given agent. Returns the port."""
    if port is None:
        port = PORT_MAP.get(role, 3284)

    # Kill any existing server on this port
    stop_server(port)
    time.sleep(1)

    agent_cmd = _agent_command(config)
    agent_type = config.provider

    # Build the full command
    cmd = ["agentapi", "server", "-p", str(port), "-t", agent_type]
    if initial_prompt:
        cmd.extend(["-I", initial_prompt])
    cmd.append("--")

    if burst_user:
        env_str = _agent_env(burst_user)
        cmd.extend(["sudo", "-n", "-u", burst_user, "env", *env_str.split()])

    cmd.extend(agent_cmd)

    # Launch detached
    log_path = work_dir / ".agent-supervisor" / "logs" / f"agentapi-{role}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "w") as log_file:
        subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(work_dir),
            start_new_session=True,
        )

    # Wait for server to be ready
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _is_server_running(port):
            return port
        time.sleep(1)

    raise RuntimeError(f"agentapi server on port {port} did not start within 60s. Check {log_path}")


def stop_server(port: int) -> None:
    """Stop an agentapi server by port."""
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def handle_dialogs(port: int, timeout: float = 30) -> None:
    """Auto-handle any TUI dialogs (trust, bypass permissions).

    Sends raw keystrokes to navigate dialogs. Checks the screen
    via /internal/screen and responds appropriately.
    """
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/internal/screen")
            resp = urllib.request.urlopen(req, timeout=3)
            screen_text = ""
            for line in resp.read().decode("utf-8").splitlines():
                if line.startswith("data:"):
                    data = json.loads(line[5:])
                    screen_text = data.get("screen", "")
                    break

            screen_lower = screen_text.lower()

            # Bypass permissions dialog: navigate to "Yes, I accept"
            if "bypass permissions" in screen_lower and "no, exit" in screen_lower:
                _send_raw(port, "\x1b[B")  # Down arrow
                time.sleep(0.5)
                _send_raw(port, "\r")  # Enter
                time.sleep(3)
                continue

            # Workspace trust dialog: accept "Yes, I trust"
            if "trust this folder" in screen_lower or "accessing workspace" in screen_lower:
                _send_raw(port, "\r")  # Enter (default is usually "Yes")
                time.sleep(3)
                continue

            # Gemini trust dialog
            if "do you trust the files" in screen_lower:
                _send_raw(port, "\r")  # Enter
                time.sleep(3)
                continue

            # Agent is at the prompt -- dialogs are done
            if "❯" in screen_text or "type your message" in screen_lower:
                return

        except Exception:
            pass
        time.sleep(2)


def _send_raw(port: int, text: str) -> None:
    """Send raw keystrokes to the agent."""
    import urllib.request
    data = json.dumps({"content": text, "type": "raw"}).encode("utf-8")
    req = urllib.request.Request(
        f"http://localhost:{port}/message",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def send_message(port: int, content: str, *, timeout: float = 3600) -> bool:
    """Send a user message. Returns True if accepted."""
    import urllib.request
    data = json.dumps({"content": content, "type": "user"}).encode("utf-8")
    req = urllib.request.Request(
        f"http://localhost:{port}/message",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status == 200
    except Exception:
        return False


def wait_for_stable(port: int, *, timeout: float = 3600, poll_interval: float = 5) -> bool:
    """Wait for agent status to become 'stable' (idle)."""
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/status")
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "stable":
                return True
        except Exception:
            pass
        time.sleep(poll_interval)
    return False


def get_messages(port: int) -> List[Dict[str, Any]]:
    """Get all messages from the conversation."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://localhost:{port}/messages")
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("messages", [])
    except Exception:
        return []


def get_last_agent_message(port: int) -> str:
    """Get the content of the last agent message."""
    messages = get_messages(port)
    for msg in reversed(messages):
        if msg.get("role") == "agent":
            return msg.get("content", "")
    return ""


def run(
    config: ProviderConfig,
    prompt: str,
    *,
    role: str = "worker",
    work_dir: Path,
    burst_user: Optional[str] = None,
    timeout: float = 3600,
    port: Optional[int] = None,
) -> BurstResult:
    """Run a burst via agentapi: start server, handle dialogs, send message, wait for response."""
    start = time.monotonic()

    try:
        port = start_server(config, role=role, work_dir=work_dir,
                           burst_user=burst_user, port=port)
    except RuntimeError as e:
        return BurstResult(ok=False, exit_code=None, captured_output="",
                          duration_seconds=time.monotonic() - start, error=str(e))

    # Handle any TUI dialogs
    handle_dialogs(port)

    # Send the prompt
    ok = send_message(port, prompt, timeout=timeout)
    if not ok:
        output = get_last_agent_message(port)
        stop_server(port)
        return BurstResult(ok=False, exit_code=None, captured_output=output,
                          duration_seconds=time.monotonic() - start,
                          error="Failed to send message")

    # Wait for agent to finish
    stable = wait_for_stable(port, timeout=timeout)

    # Get the response
    output = get_last_agent_message(port)
    duration = time.monotonic() - start

    # Don't stop the server -- leave it running for the next message
    # (agentapi supports multi-turn conversations)

    return BurstResult(
        ok=stable and len(output) > 0,
        exit_code=0 if stable else None,
        captured_output=output,
        duration_seconds=duration,
    )
