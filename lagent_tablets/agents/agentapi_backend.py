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

NOTE: Requires a patched agentapi that recognizes Gemini CLI's
▀▀▀/▄▄▄ input box borders (upstream issue #209). The patched binary
is built from /tmp/agentapi with findGeminiMessageBox() added to
message_box.go.
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
# Worker: 3284, Reviewer: 3285
# Verification agents are stateless between cycles — they use start_server
# (fresh each time) rather than ensure_server, so they don't need persistent ports.
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
    """Start a fresh agentapi server for the given agent. Returns the port."""
    if port is None:
        port = PORT_MAP.get(role, 3284)

    # Kill any existing server on this port
    stop_server(port)
    time.sleep(1)

    _launch_server(config, role=role, work_dir=work_dir, burst_user=burst_user,
                   port=port, initial_prompt=initial_prompt)
    return port


def _launch_server(
    config: ProviderConfig,
    *,
    role: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    port: int,
    initial_prompt: Optional[str] = None,
) -> None:
    """Launch an agentapi server process and wait for it to be ready."""
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
    log_path = work_dir / ".agent-supervisor" / "logs" / f"agentapi-{role}-{port}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  Starting agentapi: {' '.join(cmd[:6])}... (log: {log_path})")

    # Keep the log file open for the lifetime of the process.
    # If we close it (e.g., via `with`), agentapi loses its stdout/stderr
    # and may crash or behave unpredictably.
    log_file = open(log_path, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(work_dir),
        start_new_session=True,
    )

    # Wait for server to be ready, checking if process died
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _is_server_running(port):
            # Give the agent a moment to render its initial screen
            time.sleep(5)
            return
        # Check if process exited (crashed on startup)
        ret = proc.poll()
        if ret is not None:
            log_content = ""
            try:
                log_content = log_path.read_text(errors="replace")[-500:]
            except Exception:
                pass
            raise RuntimeError(
                f"agentapi server exited with code {ret} before becoming ready.\n"
                f"Command: {' '.join(cmd)}\n"
                f"Log tail: {log_content}"
            )
        time.sleep(1)

    raise RuntimeError(f"agentapi server on port {port} did not start within 60s. Check {log_path}")


def ensure_server(
    config: ProviderConfig,
    *,
    role: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    port: Optional[int] = None,
) -> int:
    """Ensure an agentapi server is running for this role. Reuses existing sessions.

    If a server is already running on the role's port with the correct provider,
    reuse it (preserving conversation context across cycles). Restarts if the
    provider changed or the server is unresponsive.
    """
    if port is None:
        port = PORT_MAP.get(role, 3284)

    if _is_server_running(port):
        # Check that the running server matches the expected provider
        try:
            import urllib.request
            req = urllib.request.Request(f"http://localhost:{port}/status", method="GET")
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read().decode("utf-8"))
            running_type = data.get("agent_type", "")
            if running_type != config.provider:
                print(f"  Provider mismatch on port {port}: running={running_type}, want={config.provider}. Restarting.")
                stop_server(port)
                time.sleep(1)
            else:
                return port
        except Exception:
            pass

    # No server running (or wrong provider) — start one
    _launch_server(config, role=role, work_dir=work_dir, burst_user=burst_user,
                   port=port)
    return port


def stop_server(port: int) -> None:
    """Stop an agentapi server by port and wait for the port to be released."""
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # Wait until the port is actually free
    for _ in range(10):
        if not _is_server_running(port):
            return
        time.sleep(0.5)
    # Force kill with -9
    try:
        subprocess.run(
            ["fuser", "-k", "-9", f"{port}/tcp"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    time.sleep(1)


def get_screen_text(port: int) -> str:
    """Get the current screen content from the PTY.

    The /internal/screen endpoint is SSE (Server-Sent Events) — it streams
    and never closes. We use curl with --max-time to grab the first event.
    """
    try:
        result = subprocess.run(
            ["curl", "-s", "-N", f"http://localhost:{port}/internal/screen",
             "--max-time", "3"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:])
                return data.get("screen", "")
    except Exception:
        pass
    return ""


def handle_dialogs(port: int, timeout: float = 30) -> None:
    """Auto-handle any TUI dialogs (trust, bypass permissions).

    Sends raw keystrokes to navigate dialogs. Checks the screen
    via /internal/screen and responds appropriately.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            screen_text = get_screen_text(port)
            if not screen_text:
                time.sleep(2)
                continue

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
    except Exception as exc:
        print(f"  send_message failed on port {port}: {exc}")
        return False


def switch_model(port: int, model: str, *, timeout: float = 30) -> bool:
    """Switch the agent's model via /model slash command.

    Works for Gemini CLI which supports `/model <name>` in interactive mode.
    The agentapi PTY session stays alive — no server restart needed.
    """
    ok = send_message(port, f"/model {model}", timeout=timeout)
    if not ok:
        return False
    # Wait for the slash command to be processed
    time.sleep(3)
    # Poll until stable (the /model command produces brief output)
    for _ in range(10):
        try:
            import urllib.request
            req = urllib.request.Request(f"http://localhost:{port}/status")
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode())
            if data.get("status") == "stable":
                return True
        except Exception:
            pass
        time.sleep(1)
    return True  # assume it worked even if we can't confirm


def wait_for_stable(port: int, *, timeout: float = 3600, poll_interval: float = 5,
                    done_file: Optional[Path] = None) -> bool:
    """Wait for agent to finish its task.

    Primary signal: if done_file is set (e.g. worker_handoff.json), we wait
    for that file to appear — this is the agent's explicit "I'm done" signal.

    Fallback: wait for agentapi status to become 'stable' with new messages.

    The timeout is an INACTIVITY timeout, not a wall-clock limit. As long as
    the agent is actively working (status "running"), we keep waiting.
    The timeout only applies to consecutive idle/stable time.
    """
    import urllib.request
    last_activity = time.monotonic()
    idle_timeout = timeout  # how long we tolerate consecutive inactivity

    # If we have a done_file, that's the primary completion signal
    if done_file:
        stable_count = 0
        while True:
            if done_file.exists():
                return True
            try:
                req = urllib.request.Request(f"http://localhost:{port}/status")
                resp = urllib.request.urlopen(req, timeout=5)
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == "stable":
                    stable_count += 1
                    if stable_count >= 6:  # ~30s of consecutive stable
                        return True
                else:
                    stable_count = 0
                    last_activity = time.monotonic()  # agent is active
            except Exception:
                # Server down — agent crashed or exited
                return done_file.exists()
            # Only time out on inactivity
            if time.monotonic() - last_activity > idle_timeout:
                print(f"  wait_for_stable: idle timeout after {idle_timeout:.0f}s")
                return False
            time.sleep(poll_interval)

    # No done_file — use the message-count-based approach
    initial_msg_count = len(get_messages(port))

    # Phase 1: wait for status to leave 'stable' (agent starts processing)
    phase1_deadline = time.monotonic() + 30
    saw_non_stable = False
    while time.monotonic() < phase1_deadline:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/status")
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") != "stable":
                saw_non_stable = True
                last_activity = time.monotonic()
                break
        except Exception:
            pass
        time.sleep(1)

    # Phase 2: wait for status to return to 'stable'
    while True:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/status")
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "stable":
                # Only accept if the agent actually produced new messages
                current_msg_count = len(get_messages(port))
                if current_msg_count > initial_msg_count:
                    return True
                if not saw_non_stable:
                    time.sleep(2)
                    continue
                return True
            else:
                saw_non_stable = True
                last_activity = time.monotonic()  # agent is active
        except Exception:
            pass
        if time.monotonic() - last_activity > idle_timeout:
            print(f"  wait_for_stable: idle timeout after {idle_timeout:.0f}s")
            return False
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
    """Get all agent messages from the current turn.

    Returns everything the agent said after the last user message.
    This captures commentary between tool calls, not just the final output.
    """
    messages = get_messages(port)
    if not messages:
        return ""

    # Find the last user message index
    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user_idx = i

    # Collect all agent messages after the last user message
    parts = []
    start = last_user_idx + 1 if last_user_idx >= 0 else 0
    for msg in messages[start:]:
        if msg.get("role") == "agent":
            content = msg.get("content", "").strip()
            if content:
                parts.append(content)

    return "\n\n".join(parts)


def get_last_response_from_transcript(
    provider: str,
    burst_user: Optional[str],
    work_dir: Path,
) -> str:
    """Extract the agent's last text response from its native transcript.

    This is a fallback for when agentapi's message formatting strips content
    (e.g., code blocks in Gemini responses). Reads the agent CLI's own
    chat history file directly.
    """
    src = _find_latest_transcript(provider, burst_user, work_dir)
    if not src or not src.exists():
        return ""

    try:
        if burst_user:
            result = subprocess.run(
                ["sudo", "-n", "-u", burst_user, "cat", str(src)],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                return ""
            raw = result.stdout
        else:
            raw = src.read_bytes()

        data = json.loads(raw)

        if provider == "gemini":
            # Gemini: messages array, type="gemini" with content string
            msgs = data.get("messages", [])
            for m in reversed(msgs):
                if m.get("type") == "gemini":
                    content = m.get("content", "")
                    if isinstance(content, str) and content.strip():
                        return content.strip()

        elif provider == "claude":
            # Claude: JSONL with assistant messages containing text blocks
            # raw is the full file content; for JSONL, parse last assistant message
            lines = raw.decode("utf-8", errors="replace").strip().split("\n")
            for line in reversed(lines):
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "assistant":
                        content_parts = msg.get("message", {}).get("content", [])
                        texts = [p["text"] for p in content_parts if p.get("type") == "text"]
                        if texts:
                            return "\n".join(texts)
                except json.JSONDecodeError:
                    continue

    except Exception:
        pass
    return ""


def extract_usage_from_transcript(
    provider: str,
    burst_user: Optional[str],
    work_dir: Path,
) -> Optional[Dict[str, Any]]:
    """Extract token usage from the agent's native transcript.

    Claude JSONL: last assistant message has 'usage' with input/output tokens.
    Gemini JSON: 'usageMetadata' in response messages.
    """
    src = _find_latest_transcript(provider, burst_user, work_dir)
    if not src or not src.exists():
        return None

    try:
        if burst_user:
            result = subprocess.run(
                ["sudo", "-n", "-u", burst_user, "cat", str(src)],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                return None
            raw = result.stdout
        else:
            raw = src.read_bytes()

        if provider == "claude":
            # Claude JSONL: find last line with "usage" key
            lines = raw.decode("utf-8", errors="replace").strip().split("\n")
            last_usage = None
            for line in lines:
                try:
                    msg = json.loads(line)
                    if "usage" in msg.get("message", {}):
                        last_usage = msg["message"]["usage"]
                    elif msg.get("type") == "result" and "usage" in msg:
                        last_usage = msg["usage"]
                except (json.JSONDecodeError, KeyError):
                    continue
            return last_usage

        elif provider == "gemini":
            data = json.loads(raw)
            # Gemini JSON: look for usageMetadata in messages
            msgs = data.get("messages", [])
            for m in reversed(msgs):
                meta = m.get("usageMetadata")
                if meta:
                    return {
                        "input_tokens": meta.get("promptTokenCount", 0),
                        "output_tokens": meta.get("candidatesTokenCount", 0),
                        "total_tokens": meta.get("totalTokenCount", 0),
                    }

    except Exception:
        pass
    return None


def _find_latest_transcript(
    provider: str,
    burst_user: Optional[str],
    work_dir: Path,
) -> Optional[Path]:
    """Find the most recently modified chat transcript for the agent.

    Claude saves to: ~/.claude/projects/{project-slug}/{session}.jsonl
    Gemini saves to: ~/.gemini/tmp/{project}/chats/{session}.json

    Both agents save transcripts automatically — we just find the latest one.
    """
    import glob

    home = Path(f"/home/{burst_user}") if burst_user else Path.home()

    if provider == "claude":
        # Claude project slug is derived from work_dir
        slug = str(work_dir).replace("/", "-").lstrip("-")
        pattern = str(home / ".claude" / "projects" / slug / "*.jsonl")
        files = glob.glob(pattern)
    elif provider == "gemini":
        # Gemini slugifies the project name (underscores -> hyphens)
        project = work_dir.name
        files = []
        for variant in [project, project.replace("_", "-")]:
            pattern = str(home / ".gemini" / "tmp" / variant / "chats" / "*.json")
            files = glob.glob(pattern)
            if files:
                break
    else:
        return None

    if not files:
        return None

    # Return the most recently modified
    return Path(max(files, key=lambda f: Path(f).stat().st_mtime))


def _save_transcript(
    provider: str,
    burst_user: Optional[str],
    work_dir: Path,
    dest_dir: Path,
    role: str,
) -> Optional[Path]:
    """Copy the latest agent transcript to the supervisor's log directory."""
    src = _find_latest_transcript(provider, burst_user, work_dir)
    if not src or not src.exists():
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix  # .jsonl for Claude, .json for Gemini
    dest = dest_dir / f"{role}-transcript{ext}"

    try:
        # Use sudo to read if the file is owned by burst_user
        if burst_user:
            result = subprocess.run(
                ["sudo", "-n", "-u", burst_user, "cat", str(src)],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                dest.write_bytes(result.stdout)
                return dest
        else:
            import shutil
            shutil.copy2(src, dest)
            return dest
    except Exception:
        pass
    return None


def run(
    config: ProviderConfig,
    prompt: str,
    *,
    role: str = "worker",
    work_dir: Path,
    burst_user: Optional[str] = None,
    timeout: float = 3600,
    port: Optional[int] = None,
    fresh: bool = False,
    done_file: Optional[Path] = None,
) -> BurstResult:
    """Run a burst via agentapi: start server, handle dialogs, send message, wait for response.

    If fresh=True, always starts a new server (killing any existing one on the port).
    Used for verification agents which are stateless between cycles.
    Default (fresh=False) reuses an existing session if one is running.
    """
    start = time.monotonic()

    try:
        if fresh:
            port = start_server(config, role=role, work_dir=work_dir,
                               burst_user=burst_user, port=port)
        else:
            port = ensure_server(config, role=role, work_dir=work_dir,
                                burst_user=burst_user, port=port)
    except RuntimeError as e:
        return BurstResult(ok=False, exit_code=None, captured_output="",
                          duration_seconds=time.monotonic() - start, error=str(e))

    # Handle any TUI dialogs (only needed on first connection, harmless on reuse)
    handle_dialogs(port)

    # Rate limit buffer
    time.sleep(10)

    # Quick health check: verify the agent is actually alive after startup.
    # If the PTY subprocess died (3-second screen death), the status will be
    # "stable" with no messages, or the server won't respond at all.
    try:
        import urllib.request as _ur
        _req = _ur.Request(f"http://localhost:{port}/status")
        _resp = _ur.urlopen(_req, timeout=5)
        _data = json.loads(_resp.read().decode())
        if _data.get("status") == "stable":
            # Server is up but agent might be dead. Check if there are any messages
            # (a healthy agent shows a welcome screen in the messages).
            msgs = get_messages(port)
            if not msgs:
                stop_server(port)
                return BurstResult(ok=False, exit_code=None, captured_output="",
                                  duration_seconds=time.monotonic() - start,
                                  error="Agent died on startup (no messages after init)")
    except Exception:
        stop_server(port)
        return BurstResult(ok=False, exit_code=None, captured_output="",
                          duration_seconds=time.monotonic() - start,
                          error="Agent server not responding after startup")

    # Send the prompt
    ok = send_message(port, prompt, timeout=timeout)
    if not ok:
        output = get_last_agent_message(port)
        # Also check native transcript for errors (e.g., 429 at startup)
        if not output and config.provider == "gemini":
            transcript_err = get_last_response_from_transcript(
                config.provider, burst_user, work_dir,
            )
            if transcript_err:
                output = transcript_err
        # Also check the agentapi process stderr/log for the error
        if not output:
            log_path = work_dir / ".agent-supervisor" / "logs" / "agentapi-reviewer.log"
            if log_path.exists():
                try:
                    log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    if "429" in log_tail or "capacity" in log_tail.lower():
                        output = log_tail
                except Exception:
                    pass
        stop_server(port)
        return BurstResult(ok=False, exit_code=None, captured_output=output,
                          duration_seconds=time.monotonic() - start,
                          error="Failed to send message")

    # Quick check: did the agent start processing? If status is "stable"
    # within 10 seconds of sending, the agent likely died without processing.
    time.sleep(3)
    try:
        _req = _ur.Request(f"http://localhost:{port}/status")
        _resp = _ur.urlopen(_req, timeout=5)
        _data = json.loads(_resp.read().decode())
        if _data.get("status") == "stable":
            # Check if there's a new agent message (response started)
            msgs = get_messages(port)
            agent_msgs = [m for m in msgs if m.get("role") == "agent"]
            if len(agent_msgs) <= 1:  # only welcome screen, no response
                output = get_last_agent_message(port)
                stop_server(port)
                return BurstResult(ok=False, exit_code=None, captured_output=output,
                                  duration_seconds=time.monotonic() - start,
                                  error="Agent died immediately after receiving prompt")
    except Exception:
        pass

    # Wait for agent to finish
    stable = wait_for_stable(port, timeout=timeout, done_file=done_file)

    # Get the response from both agentapi and the native transcript.
    # agentapi's message formatting can strip content (e.g., code blocks),
    # so we prefer the native transcript when available.
    output = get_last_agent_message(port)
    transcript_output = get_last_response_from_transcript(
        config.provider, burst_user, work_dir,
    )
    if transcript_output:
        output = transcript_output

    duration = time.monotonic() - start

    # Save the full agent transcript (includes thinking/thoughts)
    log_dir = work_dir / ".agent-supervisor" / "logs"
    transcript_path = _save_transcript(
        config.provider, burst_user, work_dir, log_dir, role,
    )

    # Don't stop the server -- leave it running for the next message
    # (agentapi supports multi-turn conversations)

    # Extract token usage from native transcript
    usage = extract_usage_from_transcript(config.provider, burst_user, work_dir)
    if usage is not None:
        usage["provider"] = config.provider
        usage["model"] = config.model or "auto"

    return BurstResult(
        ok=stable and len(output) > 0,
        exit_code=0 if stable else None,
        captured_output=output,
        duration_seconds=duration,
        transcript_path=transcript_path,
        usage=usage,
    )
