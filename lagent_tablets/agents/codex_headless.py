"""Codex headless backend: script-based `codex exec`.

This is the proven-reliable approach for Codex. The bash script wraps
the codex command, reads the prompt from a file, and writes start/exit
marker files via trap EXIT.

The supervisor waits only for the exit marker file -- deterministic
completion detection.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.adapters import BurstResult, ProviderConfig

WORKER_PATH = "/home/leanagent/.local/bin:/home/leanagent/.elan/bin:/home/leanagent/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"
WORKER_ELAN_HOME = "/home/leanagent/.elan"


def build_script(
    config: ProviderConfig,
    *,
    prompt_file: Path,
    start_file: Path,
    exit_file: Path,
    work_dir: Path,
    burst_user: Optional[str] = None,
    log_prefix: str = "worker",
    agent_timeout_seconds: int = 3600,
) -> Path:
    """Generate a bash script that wraps the codex exec command."""
    cmd_parts = ["codex", "exec", "--json",
                 "--skip-git-repo-check",
                 "--dangerously-bypass-approvals-and-sandbox",
                 "--ephemeral"]
    if config.model:
        cmd_parts.extend(["-m", config.model])
    if config.effort:
        cmd_parts.extend(["-c", f"reasoning_effort={shlex.quote(config.effort)}"])
    cmd_parts.extend(config.extra_args or [])
    cmd_parts.append("__PROMPT__")

    env_lines = [
        f"export PATH={shlex.quote(WORKER_PATH)}",
        f"export ELAN_HOME={shlex.quote(WORKER_ELAN_HOME)}",
    ]
    if burst_user:
        env_lines.append(f"export HOME=/home/{shlex.quote(burst_user)}")
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
        "cleanup() { ec=$?; printf '%s\\n' \"$ec\" > \"$EXIT_FILE\"; exit \"$ec\"; }",
        "trap cleanup EXIT",
        "",
        *env_lines,
        "",
        'cd "$WORK_DIR"',
        'printf "%s\\n" "$(date -Is)" > "$START_FILE"',
        'PROMPT_CONTENT=$(cat "$PROMPT_FILE")',
        "",
        "cmd=(",
        *[f"  {shlex.quote(p)}" for p in cmd_parts],
        ")",
        "real_cmd=()",
        'for arg in "${cmd[@]}"; do',
        '  if [[ "$arg" == "__PROMPT__" ]]; then real_cmd+=("$PROMPT_CONTENT")',
        '  else real_cmd+=("$arg"); fi',
        "done",
        "",
        f'LOG_FILE={shlex.quote(str(start_file.parent / f"{log_prefix}-output.log"))}',
        '"${real_cmd[@]}" > "$LOG_FILE" 2>&1',
        "ec=$?",
        'exit "$ec"',
    ]

    script_path = start_file.parent / f"{log_prefix}-burst.sh"
    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def run(
    config: ProviderConfig,
    prompt: str,
    *,
    role: str = "worker",
    session_name: str,
    work_dir: Path,
    burst_user: Optional[str] = None,
    startup_timeout: float = 60.0,
    burst_timeout: float = 7200.0,
    log_dir: Optional[Path] = None,
) -> BurstResult:
    """Run a Codex burst via the script-based pattern."""
    start = time.monotonic()

    if log_dir is None:
        log_dir = work_dir / ".agent-supervisor" / "logs" / "bursts"
    log_dir.mkdir(parents=True, exist_ok=True)

    prompt_file = log_dir / f"{role}-prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_file.chmod(0o644)

    start_file = log_dir / f"{role}.started"
    exit_file = log_dir / f"{role}.exit"
    start_file.unlink(missing_ok=True)
    exit_file.unlink(missing_ok=True)

    output_log = log_dir / f"{role}-output.log"
    output_log.write_text("", encoding="utf-8")

    script_path = build_script(
        config,
        prompt_file=prompt_file,
        start_file=start_file,
        exit_file=exit_file,
        work_dir=work_dir,
        burst_user=burst_user,
        log_prefix=role,
        agent_timeout_seconds=int(burst_timeout),
    )

    # Launch via tmux for process isolation
    from lagent_tablets.burst import tmux_ensure_session, tmux_kill_window, tmux_cmd, tmux_pane_is_dead
    tmux_ensure_session(session_name)
    window_name = f"{role}-codex"
    try:
        tmux_kill_window(session_name, window_name)
    except Exception:
        pass
    time.sleep(0.5)

    proc = tmux_cmd("new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}",
                     "-t", session_name, "-n", window_name)
    if proc.returncode != 0:
        return BurstResult(ok=False, exit_code=None, captured_output="",
                          duration_seconds=time.monotonic() - start,
                          error=f"Failed to create tmux window: {proc.stderr}")
    window_id, pane_id = proc.stdout.strip().split()
    tmux_cmd("set-window-option", "-t", window_id, "remain-on-exit", "on")

    if burst_user:
        launch_cmd = f"sudo -n -u {shlex.quote(burst_user)} {shlex.quote(str(script_path))}; exit"
    else:
        launch_cmd = f"{shlex.quote(str(script_path))}; exit"
    tmux_cmd("send-keys", "-t", pane_id, launch_cmd, "C-m")

    # Wait for start marker
    deadline_start = time.monotonic() + startup_timeout
    while time.monotonic() < deadline_start:
        if start_file.exists():
            break
        if tmux_pane_is_dead(pane_id):
            return BurstResult(ok=False, exit_code=None,
                              captured_output=output_log.read_text(errors="replace") if output_log.exists() else "",
                              duration_seconds=time.monotonic() - start,
                              error="Agent pane died before startup")
        time.sleep(0.5)

    # Wait for exit marker
    deadline_exit = time.monotonic() + burst_timeout
    while time.monotonic() < deadline_exit:
        if exit_file.exists():
            break
        if tmux_pane_is_dead(pane_id):
            time.sleep(2)
            if exit_file.exists():
                break
            return BurstResult(ok=False, exit_code=None,
                              captured_output=output_log.read_text(errors="replace") if output_log.exists() else "",
                              duration_seconds=time.monotonic() - start,
                              error="Agent pane died before exit")
        time.sleep(1)

    # Read result
    exit_code_text = exit_file.read_text().strip() if exit_file.exists() else "1"
    try:
        exit_code = int(exit_code_text)
    except ValueError:
        exit_code = 1

    time.sleep(0.5)
    output = output_log.read_text(errors="replace") if output_log.exists() else ""

    tmux_cmd("kill-window", "-t", window_id, check=False)

    # Parse usage from Codex JSON output (turn.completed event)
    usage = None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("type") == "turn.completed" and "usage" in rec:
                usage = rec["usage"]
                usage["provider"] = "codex"
                usage["model"] = config.model or "codex"
        except (json.JSONDecodeError, ValueError):
            pass

    return BurstResult(
        ok=exit_code == 0,
        exit_code=exit_code,
        captured_output=output,
        duration_seconds=time.monotonic() - start,
        usage=usage,
    )
