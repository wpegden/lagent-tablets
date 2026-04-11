"""Burst dispatch: routes to the right agent backend.

The supervisor calls run_worker_burst() and run_reviewer_burst().
These dispatch to:
- codex_headless: for Codex (proven reliable headless mode)
- agentapi_backend: for Claude and Gemini (interactive via agentapi HTTP API)
- script_headless: fallback for any provider (script-based -p mode)

All backends return the same BurstResult type.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.adapters import BurstResult, ProviderConfig
from lagent_tablets.config import SandboxConfig


# ---------------------------------------------------------------------------
# Rate limit detection (shared across backends)
# ---------------------------------------------------------------------------

RATE_LIMIT_PATTERNS = [
    "rate limit", "rate_limit", "ratelimit", "too many requests", "429",
    "resource_exhausted", "model_capacity_exhausted", "quota exceeded",
    "usage limit", "credit balance is too low", "overloaded_error",
    "hit your limit", "exceeded retry limit",
]


def is_rate_limited(output: str) -> bool:
    lowered = output.lower()
    return any(p in lowered for p in RATE_LIMIT_PATTERNS)


_EXHAUSTED_MODEL_RE = re.compile(
    r'No capacity available for model (\S+)',
    re.IGNORECASE,
)
_EXHAUSTED_MODEL_JSON_RE = re.compile(
    r'"model":\s*"([^"]+)"',
)


def extract_exhausted_model(text: str) -> Optional[str]:
    """Parse the model name from a MODEL_CAPACITY_EXHAUSTED error.

    Returns the model name (e.g., 'gemini-3-flash-preview') or None.
    """
    m = _EXHAUSTED_MODEL_RE.search(text)
    if m:
        return m.group(1)
    if "model_capacity_exhausted" in text.lower():
        m = _EXHAUSTED_MODEL_JSON_RE.search(text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Retry wrapper (shared across backends)
# ---------------------------------------------------------------------------

def run_with_retry(
    fn,
    *,
    max_retries: int = 5,
    base_delay: float = 60.0,
    max_delay: float = 900.0,
    rate_limit_delay: float = 120.0,
    config: Optional[ProviderConfig] = None,
    port: Optional[int] = None,
) -> BurstResult:
    """Retry a burst function with exponential backoff on rate limits.

    When config has fallback_models and the error identifies a specific
    exhausted model, attempts to switch to the next available fallback
    via /model command (no server restart) before retrying.
    """
    from lagent_tablets.model_availability import get_availability
    availability = get_availability()

    last_result = None
    for attempt in range(max_retries + 1):
        result = fn()
        last_result = result
        if result.ok:
            return result

        combined_output = result.captured_output + " " + result.error
        rate_limited = is_rate_limited(combined_output)

        # Gemini startup failures (Failed to send message) are often 429s
        # that happen before agentapi can capture the error text.
        # Treat as rate-limited if we have fallbacks to try.
        gemini_startup_fail = (
            not rate_limited
            and config
            and config.provider == "gemini"
            and "Failed to send message" in result.error
            and config.fallback_models
        )
        if gemini_startup_fail:
            rate_limited = True
            # Use current model as the exhausted one
            if config.model:
                combined_output += f" No capacity available for model {config.model}"

        if rate_limited:
            # Try model fallback before sleeping
            exhausted = extract_exhausted_model(combined_output)
            if exhausted and config and config.fallback_models:
                availability.mark_unavailable(exhausted, f"429 capacity exhausted")
                fallback = availability.pick_available(config.fallback_models)
                if fallback:
                    print(f"  Model {exhausted} exhausted, falling back to {fallback}")
                    config.model = fallback
                    # Switch model in running session if we have a port
                    if port and config.provider == "gemini":
                        from lagent_tablets.agents.agentapi_backend import switch_model
                        switch_model(port, fallback)
                    continue  # retry immediately with new model, no backoff

            if attempt >= max_retries:
                result.error = f"Rate limited after {max_retries} retries: {result.error}"
                blocked = availability.status()
                if blocked:
                    result.error += f" Blocked models: {blocked}"
                return result
            delay = min(rate_limit_delay * (2 ** attempt), max_delay)
            print(f"  Rate limited (attempt {attempt + 1}/{max_retries}), waiting {delay:.0f}s...")
            time.sleep(delay)
            continue

        if attempt >= max_retries:
            return result
        delay = min(base_delay * (2 ** attempt), max_delay)
        print(f"  Burst failed (attempt {attempt + 1}/{max_retries}), waiting {delay:.0f}s...")
        time.sleep(delay)
    return last_result or BurstResult(ok=False, exit_code=None, captured_output="",
                                       duration_seconds=0, error="No attempts made")


# ---------------------------------------------------------------------------
# Dispatch functions
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
    port: Optional[int] = None,
    done_file: Optional[Path] = None,
    artifact_prefix: Optional[str] = None,
    sandbox: Optional[SandboxConfig] = None,
    burst_home: Optional[Path] = None,
    **_kwargs,
) -> BurstResult:
    """Run a worker burst -- dispatches to the right backend."""
    handoff_file = done_file or work_dir / "worker_handoff.json"
    handoff_file.unlink(missing_ok=True)
    prefix = artifact_prefix or handoff_file.stem or "worker"

    def _run():
        if config.provider == "codex":
            from lagent_tablets.agents.codex_headless import run
            return run(config, prompt, role="worker", session_name=session_name,
                      work_dir=work_dir, burst_user=burst_user,
                      startup_timeout=startup_timeout_seconds,
                      burst_timeout=timeout_seconds, log_dir=log_dir,
                      artifact_prefix=prefix, fresh=False,
                      sandbox=sandbox, burst_home=burst_home)

        if config.provider in ("claude", "gemini"):
            from lagent_tablets.agents.agentapi_backend import run
            return run(config, prompt, role="worker", work_dir=work_dir,
                      burst_user=burst_user, timeout=timeout_seconds,
                      port=port,
                      log_dir=log_dir,
                      artifact_prefix=prefix,
                      done_file=handoff_file,
                      sandbox=sandbox,
                      burst_home=burst_home)

        # Unknown providers: script-based headless (-p mode)
        from lagent_tablets.agents.script_headless import run
        return run(config, prompt, role="worker", session_name=session_name,
                  work_dir=work_dir, burst_user=burst_user,
                  startup_timeout=startup_timeout_seconds,
                  burst_timeout=timeout_seconds, log_dir=log_dir,
                  artifact_prefix=prefix,
                  sandbox=sandbox,
                  burst_home=burst_home)

    return run_with_retry(_run, max_retries=max_rate_limit_retries, rate_limit_delay=120.0,
                          config=config, port=port)


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
    port: Optional[int] = None,
    fresh: bool = False,
    done_file: Optional[Path] = None,
    artifact_prefix: Optional[str] = None,
    sandbox: Optional[SandboxConfig] = None,
    burst_home: Optional[Path] = None,
    **_kwargs,
) -> BurstResult:
    """Run a reviewer burst -- dispatches to the right backend.

    If fresh=True, starts a new agent session (no context from prior cycles).
    Used for stateless verification agents.
    """

    def _run():
        prefix = artifact_prefix or (done_file.stem if done_file is not None else "reviewer")
        if config.provider == "codex":
            from lagent_tablets.agents.codex_headless import run
            return run(config, prompt, role="reviewer", session_name=session_name,
                      work_dir=work_dir, burst_user=burst_user,
                      startup_timeout=startup_timeout_seconds,
                      burst_timeout=timeout_seconds, log_dir=log_dir,
                      artifact_prefix=prefix, fresh=fresh,
                      sandbox=sandbox, burst_home=burst_home)

        if config.provider in ("claude", "gemini"):
            from lagent_tablets.agents.agentapi_backend import run
            return run(config, prompt, role="reviewer", work_dir=work_dir,
                      burst_user=burst_user, timeout=timeout_seconds,
                      port=port, fresh=fresh,
                      log_dir=log_dir,
                      artifact_prefix=prefix,
                      done_file=done_file or work_dir / "reviewer_decision.json",
                      sandbox=sandbox,
                      burst_home=burst_home)

        # Unknown providers: script-based headless
        from lagent_tablets.agents.script_headless import run
        return run(config, prompt, role="reviewer", session_name=session_name,
                  work_dir=work_dir, burst_user=burst_user,
                  startup_timeout=startup_timeout_seconds,
                  burst_timeout=timeout_seconds, log_dir=log_dir,
                  artifact_prefix=prefix,
                  sandbox=sandbox,
                  burst_home=burst_home)

    return run_with_retry(_run, max_retries=max_rate_limit_retries, rate_limit_delay=60.0,
                          config=config, port=port)


# ---------------------------------------------------------------------------
# JSON extraction (shared utility)
# ---------------------------------------------------------------------------

def _clean_terminal_json(text: str) -> str:
    """Clean terminal-formatted text for JSON parsing.

    Agent output from agentapi may have trailing whitespace padding
    on each line (terminal width) and line-wrapping inside string
    values. Strip trailing spaces and rejoin continuation lines.
    """
    # Strip ✦ prefix (Gemini response marker)
    text = text.strip()
    if text.startswith("✦"):
        text = text[1:].strip()
    # Strip trailing whitespace from each line, rejoin
    lines = [line.rstrip() for line in text.split("\n")]
    # Collapse lines that are continuations inside JSON strings:
    # A line that starts with spaces and doesn't start a new JSON key
    # is likely a wrapped continuation of the previous line.
    collapsed = []
    for line in lines:
        stripped = line.lstrip()
        if collapsed and stripped and not stripped.startswith(("{", "}", "[", "]", '"')):
            # Continuation line -- append to previous
            collapsed[-1] = collapsed[-1] + " " + stripped
        else:
            collapsed.append(line)
    return "\n".join(collapsed)


def extract_json_decision(text: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON decision from agent output."""
    text = _clean_terminal_json(text)
    try:
        wrapper = json.loads(text)
        if isinstance(wrapper, dict):
            if "decision" in wrapper:
                return wrapper
            result_text = str(wrapper.get("result", ""))
            return extract_json_decision(result_text)
    except json.JSONDecodeError:
        pass

    code_block = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if code_block:
        try:
            parsed = json.loads(code_block.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

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
# tmux helpers (used by codex_headless backend)
# ---------------------------------------------------------------------------

def tmux_cmd(*args: str, check: bool = False, timeout: int = 10):
    import subprocess
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
