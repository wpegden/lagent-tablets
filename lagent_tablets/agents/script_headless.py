"""Generic script-based headless backend (-p mode).

This is the generic fallback for any provider in non-interactive -p mode. The bash script wraps
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
from lagent_tablets.chat_history import ensure_chat_file_link
from lagent_tablets.config import SandboxConfig
from lagent_tablets.sandbox import wrap_command

WORKER_PATH = "/home/leanagent/.local/bin:/home/leanagent/.elan/bin:/home/leanagent/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"
WORKER_ELAN_HOME = "/home/leanagent/.elan"


def _artifact_prefix(prefix: Optional[str], role: str) -> str:
    base = prefix or role
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("._-") or role
    return base[:80]


def build_script(
    config: ProviderConfig,
    *,
    prompt_file: Path,
    start_file: Path,
    exit_file: Path,
    work_dir: Path,
    burst_user: Optional[str] = None,
    burst_home: Optional[Path] = None,
    log_prefix: str = "worker",
) -> Path:
    """Generate a bash script that wraps the codex exec command."""
    codex_stdin = False
    if config.provider == "claude":
        cmd_parts = ["claude", "-p", "--dangerously-skip-permissions"]
        if config.model:
            cmd_parts.extend(["--model", config.model])
        if config.effort:
            cmd_parts.extend(["--effort", config.effort])
        cmd_parts.extend(config.extra_args or [])
        cmd_parts.append("__PROMPT__")
    elif config.provider == "gemini":
        cmd_parts = ["gemini", "--approval-mode=yolo"]
        if config.model:
            cmd_parts.extend(["--model", config.model])
        cmd_parts.extend(config.extra_args or [])
        cmd_parts.extend(["-p", "__PROMPT__"])
    elif config.provider == "codex":
        cmd_parts = ["codex", "exec", "--json", "--skip-git-repo-check",
                     "--dangerously-bypass-approvals-and-sandbox", "--ephemeral"]
        if config.model:
            cmd_parts.extend(["-m", config.model])
        cmd_parts.extend(config.extra_args or [])
        cmd_parts.append("-")
        codex_stdin = True
    else:
        raise ValueError(f"Unknown provider: {config.provider}")

    env_lines = [
        f"export PATH={shlex.quote(WORKER_PATH)}",
        f"export ELAN_HOME={shlex.quote(WORKER_ELAN_HOME)}",
    ]
    if burst_home is not None:
        env_lines.append(f"export HOME={shlex.quote(str(burst_home))}")
    elif burst_user:
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
        "cleanup() {",
        "  ec=$?",
        "  trap - EXIT HUP INT TERM",
        "  if [[ -n \"${AGENT_PID:-}\" ]]; then",
        "    kill -- -\"$AGENT_PID\" 2>/dev/null || true",
        "    for _ in 1 2 3 4 5; do",
        "      if ! kill -0 -- -\"$AGENT_PID\" 2>/dev/null; then",
        "        break",
        "      fi",
        "      sleep 1",
        "    done",
        "    kill -KILL -- -\"$AGENT_PID\" 2>/dev/null || true",
        "    wait \"$AGENT_PID\" 2>/dev/null || true",
        "  fi",
        "  printf '%s\\n' \"$ec\" > \"$EXIT_FILE\"",
        "  exit \"$ec\"",
        "}",
        "trap cleanup EXIT HUP INT TERM",
        "",
        *env_lines,
        "",
        'cd "$WORK_DIR"',
        'printf "%s\\n" "$(date -Is)" > "$START_FILE"',
        "cmd=(",
        *[f"  {shlex.quote(p)}" for p in cmd_parts],
        ")",
        "",
        f'LOG_FILE={shlex.quote(str(start_file.parent / f"{log_prefix}-output.log"))}',
    ]

    if codex_stdin:
        lines.extend([
            'setsid "${cmd[@]}" < "$PROMPT_FILE" > "$LOG_FILE" 2>&1 &',
            'AGENT_PID=$!',
        ])
    else:
        lines.extend([
            'PROMPT_CONTENT=$(cat "$PROMPT_FILE")',
            "",
            "real_cmd=()",
            'for arg in "${cmd[@]}"; do',
            '  if [[ "$arg" == "__PROMPT__" ]]; then real_cmd+=("$PROMPT_CONTENT")',
            '  else real_cmd+=("$arg"); fi',
            "done",
            "",
            'setsid "${real_cmd[@]}" > "$LOG_FILE" 2>&1 &',
            'AGENT_PID=$!',
        ])

    lines.extend([
        'wait "$AGENT_PID"',
        "ec=$?",
        'exit "$ec"',
    ])

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
    artifact_prefix: Optional[str] = None,
    sandbox: Optional[SandboxConfig] = None,
    burst_home: Optional[Path] = None,
) -> BurstResult:
    """Run a Codex burst via the script-based pattern."""
    start = time.monotonic()

    if log_dir is None:
        log_dir = work_dir / ".agent-supervisor" / "logs" / "bursts"
    log_dir.mkdir(parents=True, exist_ok=True)
    prefix = _artifact_prefix(artifact_prefix, role)

    prompt_file = ensure_chat_file_link(
        work_dir,
        log_dir=log_dir,
        artifact_prefix=prefix,
        role=role,
        log_filename=f"{prefix}-prompt.txt",
        canonical_name="prompt.txt",
    )
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_file.chmod(0o644)

    start_file = log_dir / f"{prefix}.started"
    exit_file = log_dir / f"{prefix}.exit"
    start_file.unlink(missing_ok=True)
    exit_file.unlink(missing_ok=True)

    output_log = ensure_chat_file_link(
        work_dir,
        log_dir=log_dir,
        artifact_prefix=prefix,
        role=role,
        log_filename=f"{prefix}-output.log",
        canonical_name="output.log",
    )
    output_log.write_text("", encoding="utf-8")

    script_path = build_script(
        config,
        prompt_file=prompt_file,
        start_file=start_file,
        exit_file=exit_file,
        work_dir=work_dir,
        burst_user=burst_user,
        burst_home=burst_home,
        log_prefix=prefix,
    )

    # Launch via tmux for process isolation
    from lagent_tablets.burst import tmux_ensure_session, tmux_kill_window, tmux_cmd, tmux_pane_is_dead
    tmux_ensure_session(session_name)
    window_name = f"{prefix}-{config.provider}"
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

    inner_cmd = [str(script_path)]
    if burst_user:
        sandbox_cmd = wrap_command(
            inner_cmd,
            sandbox=sandbox,
            work_dir=work_dir,
            burst_user=burst_user,
            burst_home=burst_home,
        )
        launch_cmd = (
            f"sudo -n -u {shlex.quote(burst_user)} "
            + " ".join(shlex.quote(part) for part in sandbox_cmd)
            + "; exit"
        )
    else:
        sandbox_cmd = wrap_command(
            inner_cmd,
            sandbox=sandbox,
            work_dir=work_dir,
            burst_user=burst_user,
            burst_home=burst_home,
        )
        launch_cmd = " ".join(shlex.quote(part) for part in sandbox_cmd) + "; exit"
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

    # Wait for exit marker. Completion is unbounded; only startup is timed.
    while True:
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

    return BurstResult(
        ok=exit_code == 0,
        exit_code=exit_code,
        captured_output=output,
        duration_seconds=time.monotonic() - start,
    )
