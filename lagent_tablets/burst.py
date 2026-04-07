"""Burst execution: how to talk to agents.

Handles tmux management, interactive/non-interactive execution, completion
detection, stall detection/recovery, output capture, and JSON extraction.

Execution strategy per provider and role:

| Provider | Worker           | Reviewer/Verification      |
|----------|-----------------|---------------------------|
| Claude   | Interactive+marker | Non-interactive (`-p`)     |
| Codex    | Non-interactive    | Non-interactive (`exec`)   |
| Gemini   | Interactive+marker | Interactive+marker         |

Gemini is ALWAYS interactive per CLI_NOTES.md recommendation: its non-interactive
mode has open production-hardening gaps, while interactive mode is more reliable.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.adapters import BurstResult, ProviderConfig, UsageSnapshot


# ---------------------------------------------------------------------------
# Rate limit detection patterns
# ---------------------------------------------------------------------------

RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "429",
    "resource_exhausted",
    "model_capacity_exhausted",
    "quota exceeded",
    "usage limit",
    "credit balance is too low",
    "overloaded_error",
    "hit your limit",
    "exceeded retry limit",
    "retryable",
]

# Known auth issues that require session restart (not just retry)
AUTH_FAILURE_PATTERNS = [
    "not logged in",
    "authentication failed",
    "auth error",
    "oauth",
    "token expired",
    "credentials",
]


def is_rate_limited(output: str) -> bool:
    """Check if output indicates a rate limit error."""
    lowered = output.lower()
    return any(pattern in lowered for pattern in RATE_LIMIT_PATTERNS)


def is_auth_failure(output: str) -> bool:
    """Check if output indicates an auth failure requiring session restart."""
    lowered = output.lower()
    return any(pattern in lowered for pattern in AUTH_FAILURE_PATTERNS)


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
    log_path: Optional[Path] = None,
) -> BurstResult:
    """Retry a burst function with exponential backoff on rate limits.

    The built-in retry logic in all three CLIs is unreliable:
    - Claude: session permanently wedges on rate limit
    - Codex: limited retry count, loses context on resume
    - Gemini: infinite retry loop ignoring Retry-After header

    We implement our own external retry wrapper.
    """
    last_result = None
    for attempt in range(max_retries + 1):
        result = fn()
        last_result = result

        if result.ok:
            return result

        # Check if this is a rate limit we should retry
        if is_rate_limited(result.captured_output) or is_rate_limited(result.error):
            if attempt >= max_retries:
                result.error = f"Rate limited after {max_retries} retries: {result.error}"
                return result
            delay = min(rate_limit_delay * (2 ** attempt), max_delay)
            print(f"  Rate limited (attempt {attempt + 1}/{max_retries}), waiting {delay:.0f}s...")
            time.sleep(delay)
            continue

        # Auth failure: don't retry (needs human intervention or session restart)
        if is_auth_failure(result.captured_output) or is_auth_failure(result.error):
            result.error = f"Auth failure: {result.error}"
            return result

        # Other failures: retry with shorter backoff
        if attempt >= max_retries:
            return result
        delay = min(base_delay * (2 ** attempt), max_delay)
        print(f"  Burst failed (attempt {attempt + 1}/{max_retries}), waiting {delay:.0f}s...")
        time.sleep(delay)

    return last_result or BurstResult(ok=False, exit_code=None, captured_output="",
                                       duration_seconds=0, error="No attempts made")


# ---------------------------------------------------------------------------
# tmux helpers
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


def tmux_window_exists(session: str, window: str) -> bool:
    result = tmux_cmd("list-windows", "-t", session, "-F", "#{window_name}")
    return window in result.stdout.splitlines()


def tmux_create_window(session: str, window: str, *, cwd: Optional[Path] = None) -> str:
    args = ["new-window", "-t", session, "-n", window, "-P", "-F", "#{pane_id}"]
    if cwd:
        args.extend(["-c", str(cwd)])
    tmux_cmd(*args)
    return f"{session}:{window}"


def tmux_kill_window(session: str, window: str) -> None:
    tmux_cmd("kill-window", "-t", f"{session}:{window}")


def tmux_send_escape(target: str) -> None:
    tmux_cmd("send-keys", "-t", target, "Escape")


def tmux_capture_pane(target: str, *, lines: int = 5000) -> str:
    return tmux_cmd("capture-pane", "-t", target, "-p", "-S", f"-{lines}").stdout


def tmux_pipe_pane(target: str, log_path: Path) -> None:
    tmux_cmd("pipe-pane", "-t", target, f"cat >> {shlex.quote(str(log_path))}")


def tmux_pane_is_dead(target: str) -> bool:
    return tmux_cmd("display-message", "-t", target, "-p", "#{pane_dead}").stdout.strip() == "1"


def tmux_pane_pid(target: str) -> Optional[int]:
    """Get the PID of the process running in a tmux pane."""
    result = tmux_cmd("display-message", "-t", target, "-p", "#{pane_pid}")
    text = result.stdout.strip()
    try:
        return int(text) if text else None
    except ValueError:
        return None


def kill_agent_session(session_name: str, window_name: str, *, burst_user: Optional[str] = None) -> None:
    """Reliably kill an agent session: send Ctrl-C, kill the process tree, kill the window.

    Just killing the tmux window doesn't kill child processes. We need to:
    1. Send Ctrl-C to interrupt the agent
    2. Find and kill the process tree
    3. Kill the tmux window
    """
    target = f"{session_name}:{window_name}"

    if not tmux_has_session(session_name) or not tmux_window_exists(session_name, window_name):
        return

    # Get the pane PID before we kill anything
    pid = tmux_pane_pid(target)

    # Send Ctrl-C to interrupt gracefully
    tmux_cmd("send-keys", "-t", target, "C-c")
    time.sleep(1)

    # Kill the process tree
    if pid:
        try:
            # Kill the process group (all children)
            if burst_user:
                subprocess.run(
                    ["sudo", "-n", "-u", burst_user, "kill", "--", f"-{pid}"],
                    capture_output=True, timeout=5,
                )
            else:
                os.killpg(os.getpgid(pid), 9)
        except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
            pass

    # Kill any agent processes by name -- both as burst_user AND as current user.
    # Old sessions from previous runs can survive tmux kills as the supervisor user.
    if burst_user:
        for agent in ("gemini", "claude"):
            subprocess.run(
                ["sudo", "-n", "-u", burst_user, "pkill", "-f", agent],
                capture_output=True, timeout=5,
            )
    # Also kill our own orphaned agent processes
    for agent in ("gemini",):  # Only gemini tends to orphan; claude is managed differently
        subprocess.run(["pkill", "-f", agent], capture_output=True, timeout=5)

    time.sleep(1)

    # Kill the tmux window
    tmux_kill_window(session_name, window_name)


def tmux_send_prompt(target: str, prompt: str) -> None:
    """Send a prompt to a tmux pane. Uses load-buffer for reliability with long text."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(prompt)
        buf_path = f.name
    try:
        tmux_cmd("load-buffer", buf_path)
        tmux_cmd("paste-buffer", "-t", target, "-d")
        time.sleep(1)  # let TUI process the paste
        tmux_cmd("send-keys", "-t", target, "Enter")
    finally:
        os.unlink(buf_path)


# ---------------------------------------------------------------------------
# Provider-specific ready detection (only used at startup)
# ---------------------------------------------------------------------------

def _claude_is_ready(pane_text: str) -> bool:
    """Claude shows '❯' as its input prompt."""
    lines = [line.strip() for line in pane_text.strip().splitlines()]
    if not lines:
        return False
    last_prompt_idx = -1
    for i in range(len(lines) - 1, max(len(lines) - 20, -1), -1):
        if lines[i] == "❯" or lines[i].startswith("❯ "):
            last_prompt_idx = i
            break
    if last_prompt_idx < 0:
        return False
    after = " ".join(lines[last_prompt_idx + 1:]).lower()
    if "thinking" in after or "queued" in after or "cerebrating" in after:
        return False
    return True


def _gemini_is_ready(pane_text: str) -> bool:
    """Gemini shows 'Type your message' when ready. Auto-dismiss trust dialog."""
    return "type your message" in pane_text.lower()


def _gemini_has_trust_dialog(pane_text: str) -> bool:
    return "do you trust the files" in pane_text.lower()


def _agent_is_ready(provider: str, pane_text: str) -> bool:
    if provider == "claude":
        return _claude_is_ready(pane_text)
    if provider == "gemini":
        return _gemini_is_ready(pane_text)
    return False


# ---------------------------------------------------------------------------
# Interactive session management
# ---------------------------------------------------------------------------

WORKER_PATH = "/home/leanagent/.local/bin:/home/leanagent/.elan/bin:/home/leanagent/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"
WORKER_ELAN_HOME = "/home/leanagent/.elan"


def ensure_interactive_window(
    provider: str,
    command: List[str],
    *,
    session_name: str,
    window_name: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    startup_timeout: float = 120,
) -> str:
    """Ensure a tmux window exists with the agent CLI running. Returns pane target.

    If burst_user is set, the agent runs as that user via sudo. This is the
    primary mechanism for enforcing file permissions (the agent can only write
    to files that are group-writable for the shared group).
    """
    tmux_ensure_session(session_name)
    target = f"{session_name}:{window_name}"

    if tmux_window_exists(session_name, window_name):
        if tmux_pane_is_dead(target):
            tmux_kill_window(session_name, window_name)
        else:
            pane_text = tmux_capture_pane(target, lines=50)
            if _agent_is_ready(provider, pane_text):
                return target

    if not tmux_window_exists(session_name, window_name):
        tmux_create_window(session_name, window_name, cwd=work_dir)
        target = f"{session_name}:{window_name}"

        if burst_user:
            # Start a shell as burst_user with necessary env vars
            # Pass through API keys so the agent CLI can authenticate
            env_vars = f"PATH={shlex.quote(WORKER_PATH)} HOME=/home/{shlex.quote(burst_user)} ELAN_HOME={shlex.quote(WORKER_ELAN_HOME)}"
            # Forward API keys from the supervisor's environment
            for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
                val = os.environ.get(key)
                if val:
                    env_vars += f" {key}={shlex.quote(val)}"
            sudo_prefix = f"sudo -n -u {shlex.quote(burst_user)} env {env_vars}"
            cmd_str = f"{sudo_prefix} bash -c {shlex.quote('cd ' + shlex.quote(str(work_dir)) + ' && ' + ' '.join(shlex.quote(c) for c in command))}"
        else:
            cmd_str = " ".join(shlex.quote(c) for c in command)
        tmux_cmd("send-keys", "-t", target, cmd_str, "Enter")

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        pane_text = tmux_capture_pane(target, lines=50)
        if provider == "gemini" and _gemini_has_trust_dialog(pane_text):
            tmux_cmd("send-keys", "-t", target, "Enter")
            time.sleep(3)
            continue
        if _agent_is_ready(provider, pane_text):
            return target

    return target  # proceed even if not confirmed ready


# ---------------------------------------------------------------------------
# Interactive burst execution
# ---------------------------------------------------------------------------

def _write_prompt_file(prompt: str, work_dir: Path) -> Path:
    """Write prompt to a temp file in the work directory (accessible to burst_user)."""
    prompt_dir = work_dir / ".agent-supervisor" / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "current_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    # Make it readable by the burst user
    import grp
    try:
        gid = grp.getgrnam("leanagent").gr_gid
        os.chown(str(prompt_path), -1, gid)
        os.chmod(str(prompt_path), 0o664)
    except (KeyError, PermissionError):
        pass
    return prompt_path


def run_interactive_burst(
    provider: str,
    command: List[str],
    prompt: str,
    *,
    session_name: str,
    window_name: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    timeout_seconds: float = 14400,
    stall_threshold_seconds: float = 900,
    max_stall_recoveries: int = 3,
    log_dir: Optional[Path] = None,
    completion_marker: Optional[Path] = None,
) -> BurstResult:
    """Execute an interactive burst via tmux.

    Completion: ONLY via marker file. Pane scraping is not used for completion.
    Stall detection: via file mtimes across Tablet/ directory.
    """
    start = time.monotonic()
    recovery_log: List[str] = []
    stall_recoveries = 0

    # For Gemini: always start a fresh window with -i flag for reliable prompt delivery.
    # Gemini's TUI doesn't reliably accept pasted text, so we pass the prompt
    # as a CLI argument each time.
    if provider == "gemini":
        # Kill any existing session completely -- process tree and all.
        # Gemini's process can survive tmux window death and keep writing files.
        kill_agent_session(session_name, window_name, burst_user=burst_user)
        prompt_path = _write_prompt_file(prompt, work_dir)
        # Use shell expansion to read the prompt file into the -i argument
        gemini_cmd = list(command) + ["-i", f"$(cat {shlex.quote(str(prompt_path))})"]
        target = ensure_interactive_window(
            provider, gemini_cmd,
            session_name=session_name, window_name=window_name, work_dir=work_dir,
            burst_user=burst_user,
        )
    else:
        target = ensure_interactive_window(
            provider, command,
            session_name=session_name, window_name=window_name, work_dir=work_dir,
            burst_user=burst_user,
        )

    # Set up logging
    log_path = None
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{provider}_burst.log"
        log_path.write_text("", encoding="utf-8")
        tmux_pipe_pane(target, log_path)

    # Clean stale marker
    if completion_marker:
        completion_marker.parent.mkdir(parents=True, exist_ok=True)
        completion_marker.unlink(missing_ok=True)

    # Build watch list: all files in Tablet/ + marker
    watch_files: List[Path] = []
    tablet_dir = work_dir / "Tablet"
    if tablet_dir.is_dir():
        watch_files.extend(p for p in tablet_dir.iterdir() if p.is_file())
    if completion_marker:
        watch_files.append(completion_marker)

    def _snapshot_mtimes() -> Dict[str, float]:
        return {str(p): p.stat().st_mtime if p.exists() else 0 for p in watch_files}

    last_activity = time.monotonic()
    last_mtimes = _snapshot_mtimes()

    # Send prompt (Claude: paste into existing session; Gemini: already passed via -i)
    if provider != "gemini":
        tmux_send_prompt(target, prompt)
    time.sleep(3)

    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout_seconds:
            return BurstResult(
                ok=False, exit_code=None,
                captured_output=_read_log(log_path, target),
                duration_seconds=elapsed,
                stall_recoveries=stall_recoveries,
                error=f"Burst timed out after {timeout_seconds:.0f}s",
                recovery_log=recovery_log,
            )

        time.sleep(5)

        # Pane died?
        if tmux_pane_is_dead(target):
            return BurstResult(
                ok=False, exit_code=1,
                captured_output=_read_log(log_path, target),
                duration_seconds=time.monotonic() - start,
                stall_recoveries=stall_recoveries,
                error="Agent process died",
                recovery_log=recovery_log,
            )

        # Completion: marker file exists
        if completion_marker and completion_marker.exists():
            time.sleep(2)  # let agent finish final writes
            return BurstResult(
                ok=True, exit_code=0,
                captured_output=_read_log(log_path, target),
                duration_seconds=time.monotonic() - start,
                stall_recoveries=stall_recoveries,
                recovery_log=recovery_log,
            )

        # Fallback completion: agent is idle at prompt for an extended period.
        # Some agents complete work but don't write the marker file.
        # Use a generous threshold -- hard problems can take 10+ minutes of thinking.
        # We only trigger this after no file activity for a while AND agent looks idle.
        if elapsed > 120 and time.monotonic() - last_activity > 60:
            pane_text = tmux_capture_pane(target, lines=50)
            if _agent_is_ready(provider, pane_text):
                # Agent looks idle. Confirm by waiting and checking again.
                time.sleep(5)
                pane_text2 = tmux_capture_pane(target, lines=50)
                if _agent_is_ready(provider, pane_text2):
                    recovery_log.append("Fallback completion: agent idle, no marker written")
                    return BurstResult(
                        ok=True, exit_code=0,
                        captured_output=_read_log(log_path, target),
                        duration_seconds=time.monotonic() - start,
                        stall_recoveries=stall_recoveries,
                        recovery_log=recovery_log,
                    )

        # File activity tracking
        current_mtimes = _snapshot_mtimes()
        if current_mtimes != last_mtimes:
            last_activity = time.monotonic()
            last_mtimes = current_mtimes
            # Re-scan for new files created by agent
            if tablet_dir.is_dir():
                new_files = [p for p in tablet_dir.iterdir() if p.is_file() and str(p) not in last_mtimes]
                watch_files.extend(new_files)

        # Stall?
        if time.monotonic() - last_activity > stall_threshold_seconds:
            stall_recoveries += 1
            if stall_recoveries > max_stall_recoveries:
                return BurstResult(
                    ok=False, exit_code=None,
                    captured_output=_read_log(log_path, target),
                    duration_seconds=time.monotonic() - start,
                    stall_recoveries=stall_recoveries,
                    error=f"Exhausted {max_stall_recoveries} stall recoveries",
                    recovery_log=recovery_log,
                )

            recovery_log.append(f"Stall recovery attempt {stall_recoveries}")
            # Step 1: Esc
            tmux_send_escape(target)
            time.sleep(30)
            if completion_marker and completion_marker.exists():
                continue
            # Step 2: Reprompt
            recovery_log.append("  Re-sending prompt")
            tmux_send_prompt(target, prompt)
            time.sleep(60)
            if completion_marker and completion_marker.exists():
                continue
            # Step 3: Kill and restart
            recovery_log.append("  Killing and restarting session")
            kill_agent_session(session_name, window_name, burst_user=burst_user)
            time.sleep(2)
            target = ensure_interactive_window(
                provider, command,
                session_name=session_name, window_name=window_name, work_dir=work_dir,
                burst_user=burst_user,
            )
            if log_path:
                tmux_pipe_pane(target, log_path)
            tmux_send_prompt(target, prompt)
            last_activity = time.monotonic()
            last_mtimes = _snapshot_mtimes()


# ---------------------------------------------------------------------------
# Non-interactive burst execution
# ---------------------------------------------------------------------------

def run_noninteractive_burst(
    command: List[str],
    prompt: str,
    *,
    work_dir: Path,
    timeout_seconds: float = 14400,
    log_dir: Optional[Path] = None,
) -> BurstResult:
    """Execute a non-interactive burst as a subprocess."""
    start = time.monotonic()

    try:
        proc = subprocess.run(
            [*command, prompt],
            capture_output=True, text=True,
            cwd=str(work_dir),
            timeout=timeout_seconds,
        )
        duration = time.monotonic() - start
        output = proc.stdout
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "burst.jsonl").write_text(output, encoding="utf-8")

        return BurstResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            captured_output=output,
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired:
        return BurstResult(
            ok=False, exit_code=None, captured_output="",
            duration_seconds=time.monotonic() - start,
            error=f"Timed out after {timeout_seconds}s",
        )
    except FileNotFoundError:
        return BurstResult(
            ok=False, exit_code=None, captured_output="",
            duration_seconds=time.monotonic() - start,
            error=f"Command not found: {command[0]}",
        )


def run_noninteractive_prompt(
    provider: str,
    prompt: str,
    *,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    work_dir: Path,
    timeout_seconds: float = 300,
    log_dir: Optional[Path] = None,
) -> BurstResult:
    """Run a non-interactive prompt (for reviewer/verification on Claude/Codex)."""
    if provider == "claude":
        cmd = ["claude", "-p", "--output-format", "json"]
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])
    elif provider == "codex":
        cmd = ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            cmd.extend(["-m", model])
    else:
        cmd = ["claude", "-p"]

    if extra_args:
        cmd.extend(extra_args)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            cwd=str(work_dir), timeout=timeout_seconds,
        )
        output = proc.stdout + "\n" + proc.stderr
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "reviewer_burst.log").write_text(output, encoding="utf-8")
        return BurstResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            captured_output=output,
            duration_seconds=time.monotonic() - start,
        )
    except subprocess.TimeoutExpired:
        return BurstResult(
            ok=False, exit_code=None, captured_output="",
            duration_seconds=time.monotonic() - start,
            error=f"Timed out after {timeout_seconds}s",
        )
    except FileNotFoundError:
        return BurstResult(
            ok=False, exit_code=None, captured_output="",
            duration_seconds=time.monotonic() - start,
            error=f"Command not found: {cmd[0]}",
        )


# ---------------------------------------------------------------------------
# High-level burst dispatch
# ---------------------------------------------------------------------------

def build_command(config: ProviderConfig, *, initial: bool = True) -> List[str]:
    """Build the CLI command for a provider.

    Reliability notes (from CLI_NOTES.md + GitHub issue research):

    Claude:
    - Use ANTHROPIC_API_KEY env var (OAuth tokens expire after ~8h, breaking automation)
    - Don't use --bare (breaks auth when using OAuth login)
    - Sessions can permanently wedge on rate limit -- our external retry handles this
    - Third-party harness detection may penalize automated use -- rate limit our calls

    Codex:
    - Avoid MCP servers in exec mode entirely (tool calls always cancelled)
    - Compaction hangs are unrecoverable -- treat sessions as disposable
    - --json flag for JSONL event output
    - --ephemeral to avoid session state accumulation

    Gemini:
    - ALWAYS interactive (non-interactive mode has missing event handlers and broken retry)
    - Use GEMINI_API_KEY env var (keytar/GNOME Keyring hangs on headless Linux)
    - Run from the repo directory (not home) to avoid MemoryDiscovery scanning home dir
    - Built-in 429 retry ignores Retry-After header -- our external retry handles this
    """
    if config.provider == "claude":
        cmd = ["claude", "--dangerously-skip-permissions"]
        if config.model:
            cmd.extend(["--model", config.model])
        if config.effort:
            cmd.extend(["--effort", config.effort])
        cmd.extend(config.extra_args or [])
        if not initial:
            cmd.insert(1, "--continue")
        return cmd

    if config.provider == "codex":
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox",
               "--ephemeral"]  # avoid session state accumulation
        if config.model:
            cmd.extend(["-m", config.model])
        cmd.extend(config.extra_args or [])
        return cmd

    if config.provider == "gemini":
        # Gemini's -i flag passes the initial prompt as a CLI argument,
        # which is far more reliable than pasting into the TUI.
        # The prompt placeholder __PROMPT__ will be replaced at launch time.
        cmd = ["gemini", "--approval-mode=yolo"]
        if config.model:
            cmd.extend(["--model", config.model])
        cmd.extend(config.extra_args or [])
        if not initial:
            cmd.extend(["--resume", "latest"])
        return cmd

    raise ValueError(f"Unknown provider: {config.provider}")


def run_worker_burst(
    config: ProviderConfig,
    prompt: str,
    *,
    session_name: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    timeout_seconds: float = 14400,
    stall_threshold_seconds: float = 900,
    max_stall_recoveries: int = 3,
    max_rate_limit_retries: int = 5,
    log_dir: Optional[Path] = None,
    completion_marker: Optional[Path] = None,
) -> BurstResult:
    """Run a worker burst with external retry for rate limits.

    Claude/Gemini: interactive (as burst_user for permission enforcement).
    Codex: non-interactive.
    All providers: external retry wrapper for rate limits.
    """
    cmd = build_command(config)

    def _run():
        if config.provider == "codex":
            return run_noninteractive_burst(
                cmd, prompt, work_dir=work_dir,
                timeout_seconds=timeout_seconds, log_dir=log_dir,
            )
        # Claude and Gemini: interactive, as burst_user
        return run_interactive_burst(
            config.provider, cmd, prompt,
            session_name=session_name,
            window_name=f"{config.provider}-worker",
            work_dir=work_dir,
            burst_user=burst_user,
            timeout_seconds=timeout_seconds,
            stall_threshold_seconds=stall_threshold_seconds,
            max_stall_recoveries=max_stall_recoveries,
            log_dir=log_dir,
            completion_marker=completion_marker,
        )

    return run_with_retry(_run, max_retries=max_rate_limit_retries, rate_limit_delay=120.0)


def run_reviewer_burst(
    config: ProviderConfig,
    prompt: str,
    *,
    session_name: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    timeout_seconds: float = 300,
    stall_threshold_seconds: float = 900,
    max_stall_recoveries: int = 3,
    max_rate_limit_retries: int = 3,
    log_dir: Optional[Path] = None,
    completion_marker: Optional[Path] = None,
) -> BurstResult:
    """Run a reviewer burst with external retry for rate limits.

    Claude/Codex: non-interactive. Gemini: interactive (always, as burst_user).
    All providers: external retry wrapper for rate limits.
    """
    def _run():
        if config.provider == "gemini":
            cmd = build_command(config)
            return run_interactive_burst(
                config.provider, cmd, prompt,
                session_name=session_name,
                window_name="gemini-reviewer",
                work_dir=work_dir,
                burst_user=burst_user,
                timeout_seconds=timeout_seconds,
                stall_threshold_seconds=stall_threshold_seconds,
                max_stall_recoveries=max_stall_recoveries,
                log_dir=log_dir,
                completion_marker=completion_marker,
            )
        # Claude and Codex: non-interactive
        return run_noninteractive_prompt(
            config.provider, prompt,
            model=config.model, effort=config.effort, extra_args=config.extra_args,
            work_dir=work_dir, timeout_seconds=timeout_seconds, log_dir=log_dir,
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
    usage = data.get("usage")
    if isinstance(usage, dict):
        return {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "total_cost_usd": data.get("total_cost_usd"),
        }
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_log(log_path: Optional[Path], target: str) -> str:
    if log_path and log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            return text
    return tmux_capture_pane(target, lines=10000)
