"""Burst execution: how to talk to agents.

Uses the proven v1 pattern: a bash script wraps the agent command, writes
start/exit marker files, and the supervisor waits only for those files.
All agents run in non-interactive "print" mode (claude -p, gemini -p, codex exec).
They can still use tools (edit files, run commands) -- they just exit when done.

The bash trap EXIT guarantees the exit marker is written even if the agent crashes.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.adapters import BurstResult, ProviderConfig


# ---------------------------------------------------------------------------
# Rate limit detection
# ---------------------------------------------------------------------------

RATE_LIMIT_PATTERNS = [
    "rate limit", "rate_limit", "ratelimit", "too many requests", "429",
    "resource_exhausted", "model_capacity_exhausted", "quota exceeded",
    "usage limit", "credit balance is too low", "overloaded_error",
    "hit your limit", "exceeded retry limit",
]

AUTH_FAILURE_PATTERNS = [
    "not logged in", "authentication failed", "auth error",
    "token expired", "credentials",
]


def is_rate_limited(output: str) -> bool:
    lowered = output.lower()
    return any(p in lowered for p in RATE_LIMIT_PATTERNS)


def is_auth_failure(output: str) -> bool:
    lowered = output.lower()
    return any(p in lowered for p in AUTH_FAILURE_PATTERNS)


# ---------------------------------------------------------------------------
# tmux helpers (minimal -- only what we need for script-based bursts)
# ---------------------------------------------------------------------------

def tmux_cmd(*args: str, check: bool = False, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=timeout, check=check,
    )


def tmux_has_session(session: str) -> bool:
    return tmux_cmd("has-session", "-t", session).returncode == 0


def tmux_ensure_session(session: str) -> None:
    if not tmux_has_session(session):
        tmux_cmd("new-session", "-d", "-s", session, "-x", "220", "-y", "50")


def tmux_kill_window(session: str, window: str) -> None:
    tmux_cmd("kill-window", "-t", f"{session}:{window}")


def tmux_pane_is_dead(pane_id: str) -> bool:
    result = tmux_cmd("display-message", "-p", "-t", pane_id, "#{pane_dead}", check=False)
    if result.returncode != 0:
        return True
    return result.stdout.strip() == "1"


# ---------------------------------------------------------------------------
# Path constants for burst_user environment
# ---------------------------------------------------------------------------

WORKER_PATH = "/home/leanagent/.local/bin:/home/leanagent/.elan/bin:/home/leanagent/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"
WORKER_ELAN_HOME = "/home/leanagent/.elan"


# ---------------------------------------------------------------------------
# Burst script generation (the v1 reliable pattern)
# ---------------------------------------------------------------------------

def build_burst_script(
    config: ProviderConfig,
    *,
    prompt_file: Path,
    start_file: Path,
    exit_file: Path,
    work_dir: Path,
    burst_user: Optional[str] = None,
    log_prefix: str = "agent",
) -> Path:
    """Generate a bash script that wraps the agent command.

    The script:
    1. Reads the prompt from prompt_file
    2. Writes a start marker when it begins
    3. Runs the agent command with the prompt
    4. Writes an exit marker with the exit code (via trap EXIT)
    5. The exit marker is ALWAYS written, even if the agent crashes

    This is the proven v1 pattern for reliable agent communication.
    """
    # Build the agent command with __PROMPT__ placeholder
    cmd_parts = _build_command_parts(config)

    script_dir = start_file.parent
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / f"{log_prefix}-burst.sh"

    env_lines = [
        f"export PATH={shlex.quote(WORKER_PATH)}",
        f"export ELAN_HOME={shlex.quote(WORKER_ELAN_HOME)}",
    ]
    if burst_user:
        env_lines.append(f"export HOME=/home/{shlex.quote(burst_user)}")

    # Forward API keys from supervisor environment
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(key)
        if val:
            env_lines.append(f"export {key}={shlex.quote(val)}")

    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        "umask 0002",
        f"START_FILE={shlex.quote(str(start_file))}",
        f"EXIT_FILE={shlex.quote(str(exit_file))}",
        f"PROMPT_FILE={shlex.quote(str(prompt_file))}",
        f"WORK_DIR={shlex.quote(str(work_dir))}",
        "",
        "cleanup() {",
        "  ec=$?",
        '  printf "%s\\n" "$ec" > "$EXIT_FILE"',
        '  exit "$ec"',
        "}",
        "trap cleanup EXIT",
        "",
        *env_lines,
        "",
        'cd "$WORK_DIR"',
        'printf "%s\\n" "$(date -Is)" > "$START_FILE"',
        'PROMPT_CONTENT=$(cat "$PROMPT_FILE")',
        "",
        "# Build the agent command, replacing __PROMPT__ with prompt content",
        "cmd=(",
    ]
    for part in cmd_parts:
        lines.append(f"  {shlex.quote(part)}")
    # Determine the timeout for the agent command itself.
    # This is coreutils `timeout` as an EXTERNAL watchdog.
    # The bash trap EXIT still fires on SIGTERM, writing the exit marker.
    # This protects against:
    # - Claude -p infinite retry loops on permission denial
    # - Claude -p SIGTERM after 3-10 min (we set a longer timeout)
    # - Gemini sub-agent hangs
    # - Codex tool-call hang regression
    # - Dead websocket with 5-min detection
    agent_timeout_seconds = 3600  # 1 hour default per burst

    lines += [
        ")",
        "real_cmd=()",
        'for arg in "${cmd[@]}"; do',
        '  if [[ "$arg" == "__PROMPT__" ]]; then',
        '    real_cmd+=("$PROMPT_CONTENT")',
        "  else",
        '    real_cmd+=("$arg")',
        "  fi",
        "done",
        "",
        f'echo "[{log_prefix}-burst] provider={config.provider} start=$(date -Is)"',
        f'# External watchdog: kill agent after {agent_timeout_seconds}s if it hangs',
        f'# Redirect stdout/stderr to a log file (no tee -- tee keeps pipe alive after agent exits)',
        f'LOG_FILE={shlex.quote(str(start_file.parent / f"{log_prefix}-output.log"))}',
        f'timeout --signal=TERM --kill-after=30 {agent_timeout_seconds} "${{real_cmd[@]}}" > "$LOG_FILE" 2>&1',
        "ec=$?",
        "# timeout exit codes: 124=timed out, 137=killed",
        'if [ "$ec" -eq 124 ] || [ "$ec" -eq 137 ]; then',
        f'  echo "[{log_prefix}-burst] AGENT TIMED OUT after {agent_timeout_seconds}s"',
        "fi",
        f'echo "[{log_prefix}-burst] end=$(date -Is) exit_code=$ec"',
        'exit "$ec"',
    ]

    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def _build_command_parts(config: ProviderConfig) -> List[str]:
    """Build command parts with __PROMPT__ as the prompt placeholder.

    All agents run in non-interactive mode:
    - Claude: -p (print mode, still uses tools, exits when done)
    - Codex: exec (non-interactive, exits when done)
    - Gemini: -p (non-interactive prompt mode, exits when done)
    """
    if config.provider == "claude":
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
        if config.model:
            cmd.extend(["--model", config.model])
        if config.effort:
            cmd.extend(["--effort", config.effort])
        cmd.extend(config.extra_args or [])
        cmd.append("__PROMPT__")
        return cmd

    if config.provider == "codex":
        cmd = ["codex", "exec", "--json",
               "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox",
               "--ephemeral"]
        if config.model:
            cmd.extend(["-m", config.model])
        cmd.extend(config.extra_args or [])
        cmd.append("__PROMPT__")
        return cmd

    if config.provider == "gemini":
        # Gemini's -p mode exits immediately with code 0 on 429 rate limits
        # (no retry, no error message, just empty output). Our run_with_retry
        # handles this via empty-output detection + exponential backoff.
        cmd = ["gemini", "--approval-mode=yolo"]
        if config.model:
            cmd.extend(["--model", config.model])
        cmd.extend(config.extra_args or [])
        cmd.extend(["-p", "__PROMPT__"])
        return cmd

    raise ValueError(f"Unknown provider: {config.provider}")


# ---------------------------------------------------------------------------
# Wait for marker files (the reliable completion mechanism)
# ---------------------------------------------------------------------------

def wait_for_file(
    path: Path,
    pane_id: str,
    timeout_seconds: float,
    *,
    poll_seconds: float = 0.5,
    label: str = "marker",
) -> None:
    """Wait for a file to appear. Raises if timeout or pane dies."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if path.exists():
            return
        if tmux_pane_is_dead(pane_id):
            # Give a brief grace period for the trap to write the file
            grace_end = time.monotonic() + 2.0
            while time.monotonic() < grace_end:
                if path.exists():
                    return
                time.sleep(0.1)
            raise RuntimeError(f"Agent pane died before writing {label}: {path}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out after {timeout_seconds:.0f}s waiting for {label}: {path}")
        time.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# Run a burst (the main entry point)
# ---------------------------------------------------------------------------

def run_burst(
    config: ProviderConfig,
    prompt: str,
    *,
    role: str = "worker",
    session_name: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    startup_timeout_seconds: float = 120.0,
    burst_timeout_seconds: float = 7200.0,
    log_dir: Optional[Path] = None,
) -> BurstResult:
    """Run a single agent burst using the script-based pattern.

    1. Write prompt to a file
    2. Build a bash script that wraps the agent command
    3. Launch the script in a tmux window (as burst_user via sudo)
    4. Wait for the exit marker file
    5. Read the captured output from the log file

    This is deterministic: the exit marker is ALWAYS written via bash trap EXIT.
    """
    start = time.monotonic()

    # Set up directories
    if log_dir is None:
        log_dir = work_dir / ".agent-supervisor" / "logs" / "bursts"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Write prompt file
    prompt_file = log_dir / f"{role}-prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_file.chmod(0o644)

    # Marker files
    start_file = log_dir / f"{role}.started"
    exit_file = log_dir / f"{role}.exit"
    start_file.unlink(missing_ok=True)
    exit_file.unlink(missing_ok=True)

    # Per-cycle log
    per_cycle_log = log_dir / f"{role}-{config.provider}.ansi.log"
    per_cycle_log.parent.mkdir(parents=True, exist_ok=True)
    per_cycle_log.write_text("", encoding="utf-8")

    # Build the script
    script_path = build_burst_script(
        config,
        prompt_file=prompt_file,
        start_file=start_file,
        exit_file=exit_file,
        work_dir=work_dir,
        burst_user=burst_user,
        log_prefix=role,
    )

    # Launch in tmux
    tmux_ensure_session(session_name)
    window_name = f"{role}-burst"

    # Kill any existing window
    try:
        tmux_kill_window(session_name, window_name)
    except Exception:
        pass
    time.sleep(0.5)

    # Create new window
    proc = tmux_cmd(
        "new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}",
        "-t", session_name, "-n", window_name,
    )
    if proc.returncode != 0:
        return BurstResult(
            ok=False, exit_code=None, captured_output="",
            duration_seconds=time.monotonic() - start,
            error=f"Failed to create tmux window: {proc.stderr}",
        )
    window_id, pane_id = proc.stdout.strip().split()

    # Set remain-on-exit so we can capture output after the script finishes
    tmux_cmd("set-window-option", "-t", window_id, "remain-on-exit", "on")

    # Pipe pane output to log file
    pipe_cmd = f"cat >> {shlex.quote(str(per_cycle_log))}"
    tmux_cmd("pipe-pane", "-o", "-t", pane_id, pipe_cmd)

    # Launch the script (as burst_user if configured)
    if burst_user:
        launch_cmd = f"sudo -n -u {shlex.quote(burst_user)} {shlex.quote(str(script_path))}; exit"
    else:
        launch_cmd = f"{shlex.quote(str(script_path))}; exit"
    tmux_cmd("send-keys", "-t", pane_id, launch_cmd, "C-m")

    # Wait for start marker
    try:
        wait_for_file(start_file, pane_id, startup_timeout_seconds, label="start marker")
    except RuntimeError as e:
        output = _read_log(per_cycle_log)
        return BurstResult(
            ok=False, exit_code=None, captured_output=output,
            duration_seconds=time.monotonic() - start,
            error=str(e),
        )

    # Wait for exit marker with stall detection.
    # The agent might hang (rate limit stuck, network disconnect, OOM).
    # We detect this by monitoring file activity: if no files in Tablet/ change
    # for stall_minutes AND the exit marker hasn't appeared, the agent is stalled.
    stall_minutes = 30  # no file activity for 30 minutes = stalled
    try:
        wait_for_file_with_stall_detection(
            exit_file, pane_id, burst_timeout_seconds,
            label="exit marker",
            watch_dir=work_dir / "Tablet",
            stall_timeout_seconds=stall_minutes * 60,
            log_path=per_cycle_log,
        )
    except StallDetected as e:
        output = _read_log(per_cycle_log)
        # Kill the stalled agent and its process tree
        _kill_pane_process_tree(pane_id)
        tmux_cmd("kill-window", "-t", window_id, check=False)
        return BurstResult(
            ok=False, exit_code=None, captured_output=output,
            duration_seconds=time.monotonic() - start,
            error=f"Agent stalled: {e}",
        )
    except RuntimeError as e:
        output = _read_log(per_cycle_log)
        return BurstResult(
            ok=False, exit_code=None, captured_output=output,
            duration_seconds=time.monotonic() - start,
            error=str(e),
        )

    # Read exit code
    exit_code_text = exit_file.read_text(encoding="utf-8").strip()
    try:
        exit_code = int(exit_code_text)
    except (ValueError, TypeError):
        exit_code = 1

    # Read output -- check both the pipe-pane log and the direct output log
    time.sleep(0.5)  # let pipe flush
    output_log = log_dir / f"{role}-output.log"
    output = _read_log(output_log) or _read_log(per_cycle_log)

    # Kill the window
    tmux_cmd("kill-window", "-t", window_id, check=False)

    duration = time.monotonic() - start
    return BurstResult(
        ok=exit_code == 0,
        exit_code=exit_code,
        captured_output=output,
        duration_seconds=duration,
    )


class StallDetected(RuntimeError):
    """Raised when the agent appears stalled (no file activity for too long)."""
    pass


def wait_for_file_with_stall_detection(
    path: Path,
    pane_id: str,
    timeout_seconds: float,
    *,
    label: str = "marker",
    watch_dir: Optional[Path] = None,
    stall_timeout_seconds: float = 1800,
    log_path: Optional[Path] = None,
    poll_seconds: float = 5.0,
) -> None:
    """Wait for a file to appear, with stall detection based on filesystem activity.

    A stall is detected when:
    - No files in watch_dir have been modified for stall_timeout_seconds
    - AND the log file hasn't been modified for stall_timeout_seconds
    - AND the target file hasn't appeared

    This catches agents that hang (rate limit loops, network disconnects, OOM)
    even when the bash wrapper's trap EXIT can't fire (because the process is alive but stuck).
    """
    deadline = time.monotonic() + timeout_seconds
    last_activity = time.monotonic()

    def _latest_mtime() -> float:
        """Get the most recent mtime across watched files."""
        latest = 0.0
        if watch_dir and watch_dir.is_dir():
            for f in watch_dir.iterdir():
                if f.is_file():
                    try:
                        latest = max(latest, f.stat().st_mtime)
                    except OSError:
                        pass
        if log_path and log_path.exists():
            try:
                latest = max(latest, log_path.stat().st_mtime)
            except OSError:
                pass
        return latest

    last_seen_mtime = _latest_mtime()

    while True:
        if path.exists():
            return

        if tmux_pane_is_dead(pane_id):
            grace_end = time.monotonic() + 2.0
            while time.monotonic() < grace_end:
                if path.exists():
                    return
                time.sleep(0.1)
            raise RuntimeError(f"Agent pane died before writing {label}: {path}")

        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out after {timeout_seconds:.0f}s waiting for {label}: {path}")

        # Check for file activity
        current_mtime = _latest_mtime()
        if current_mtime > last_seen_mtime:
            last_activity = time.monotonic()
            last_seen_mtime = current_mtime

        # Stall detection
        idle_seconds = time.monotonic() - last_activity
        if idle_seconds > stall_timeout_seconds:
            raise StallDetected(
                f"No file activity for {idle_seconds:.0f}s (threshold: {stall_timeout_seconds:.0f}s). "
                f"Agent may be hung."
            )

        time.sleep(poll_seconds)


def _kill_pane_process_tree(pane_id: str) -> None:
    """Kill the process tree running in a tmux pane."""
    result = tmux_cmd("display-message", "-t", pane_id, "-p", "#{pane_pid}")
    pid_text = result.stdout.strip()
    if not pid_text:
        return
    try:
        pid = int(pid_text)
        # Kill the process group
        os.killpg(os.getpgid(pid), 9)
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        pass


def _read_log(log_path: Path) -> str:
    if log_path.exists():
        return log_path.read_text(encoding="utf-8", errors="replace")
    return ""


# ---------------------------------------------------------------------------
# Retry with backoff
# ---------------------------------------------------------------------------

def run_with_retry(
    fn,
    *,
    max_retries: int = 5,
    base_delay: float = 60.0,
    max_delay: float = 900.0,
    rate_limit_delay: float = 120.0,
) -> BurstResult:
    """Retry a burst function with exponential backoff on rate limits."""
    last_result = None
    for attempt in range(max_retries + 1):
        result = fn()
        last_result = result

        if result.ok:
            return result

        if is_rate_limited(result.captured_output) or is_rate_limited(result.error):
            if attempt >= max_retries:
                result.error = f"Rate limited after {max_retries} retries: {result.error}"
                return result
            delay = min(rate_limit_delay * (2 ** attempt), max_delay)
            print(f"  Rate limited (attempt {attempt + 1}/{max_retries}), waiting {delay:.0f}s...")
            time.sleep(delay)
            continue

        if is_auth_failure(result.captured_output) or is_auth_failure(result.error):
            result.error = f"Auth failure: {result.error}"
            return result

        if attempt >= max_retries:
            return result
        delay = min(base_delay * (2 ** attempt), max_delay)
        print(f"  Burst failed (attempt {attempt + 1}/{max_retries}), waiting {delay:.0f}s...")
        time.sleep(delay)

    return last_result or BurstResult(ok=False, exit_code=None, captured_output="",
                                       duration_seconds=0, error="No attempts made")


# ---------------------------------------------------------------------------
# High-level burst functions
# ---------------------------------------------------------------------------

def run_worker_burst(
    config: ProviderConfig,
    prompt: str,
    *,
    session_name: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    timeout_seconds: float = 7200.0,
    startup_timeout_seconds: float = 120.0,
    max_rate_limit_retries: int = 5,
    log_dir: Optional[Path] = None,
    **_kwargs,  # accept and ignore extra kwargs for compatibility
) -> BurstResult:
    """Run a worker burst with retry."""
    def _run():
        return run_burst(
            config, prompt,
            role="worker",
            session_name=session_name,
            work_dir=work_dir,
            burst_user=burst_user,
            startup_timeout_seconds=startup_timeout_seconds,
            burst_timeout_seconds=timeout_seconds,
            log_dir=log_dir,
        )
    def _run_with_empty_check():
        result = _run()
        # Detect "agent exited 0 but did nothing" (common with Gemini 429)
        if result.ok and len(result.captured_output.strip()) < 50:
            result.ok = False
            result.error = "Agent exited successfully but produced no meaningful output (possible rate limit)"
        return result

    return run_with_retry(_run_with_empty_check, max_retries=max_rate_limit_retries, rate_limit_delay=120.0)


def run_reviewer_burst(
    config: ProviderConfig,
    prompt: str,
    *,
    session_name: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    timeout_seconds: float = 300.0,
    startup_timeout_seconds: float = 60.0,
    max_rate_limit_retries: int = 3,
    log_dir: Optional[Path] = None,
    **_kwargs,
) -> BurstResult:
    """Run a reviewer burst with retry."""
    def _run():
        return run_burst(
            config, prompt,
            role="reviewer",
            session_name=session_name,
            work_dir=work_dir,
            burst_user=burst_user,
            startup_timeout_seconds=startup_timeout_seconds,
            burst_timeout_seconds=timeout_seconds,
            log_dir=log_dir,
        )
    return run_with_retry(_run, max_retries=max_rate_limit_retries, rate_limit_delay=60.0)


# ---------------------------------------------------------------------------
# JSON extraction from output
# ---------------------------------------------------------------------------

def extract_json_decision(text: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON decision object from agent output.

    Handles: Claude -p JSON wrapper, markdown code blocks, raw JSON, embedded JSON.
    """
    text = text.strip()

    # 1. Claude -p --output-format json wrapper
    try:
        wrapper = json.loads(text)
        if isinstance(wrapper, dict):
            if "decision" in wrapper:
                return wrapper
            result_text = str(wrapper.get("result", ""))
            return extract_json_decision(result_text)
    except json.JSONDecodeError:
        pass

    # 2. Markdown code fences
    code_block = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if code_block:
        try:
            parsed = json.loads(code_block.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. Find JSON objects with "decision" key
    best = None
    depth = 0
    start_idx = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start_idx >= 0:
                try:
                    parsed = json.loads(text[start_idx:i + 1])
                    if isinstance(parsed, dict) and "decision" in parsed:
                        best = parsed
                except json.JSONDecodeError:
                    pass
                start_idx = -1
    return best


# ---------------------------------------------------------------------------
# Usage parsing
# ---------------------------------------------------------------------------

def parse_codex_usage(jsonl_text: str) -> Optional[Dict[str, Any]]:
    """Extract token usage from Codex JSONL output."""
    total_input = total_output = total_cached = total_reasoning = 0
    found = False
    for line in jsonl_text.strip().splitlines():
        try:
            record = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if record.get("type") == "turn.completed":
            usage = record.get("usage")
            if isinstance(usage, dict):
                total_input += int(usage.get("input_tokens", 0))
                total_output += int(usage.get("output_tokens", 0))
                total_cached += int(usage.get("cached_input_tokens", 0))
                total_reasoning += int(usage.get("reasoning_output_tokens", 0))
                found = True
    if found:
        return {
            "input_tokens": total_input, "output_tokens": total_output,
            "cached_input_tokens": total_cached, "reasoning_tokens": total_reasoning,
            "total_tokens": total_input + total_output,
        }
    return None


def parse_claude_json_usage(json_text: str) -> Optional[Dict[str, Any]]:
    """Extract usage from Claude -p --output-format json output."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and "total_cost_usd" in data:
        return {"total_cost_usd": data.get("total_cost_usd")}
    return None
