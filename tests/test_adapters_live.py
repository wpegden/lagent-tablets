#!/usr/bin/env python3
"""Live integration tests for provider adapters.

These tests make real API calls. Run manually:
    cd ~/src/lagent-tablets
    python -m tests.test_adapters_live

Each test sends a trivial prompt and verifies:
1. The burst completes (ok=True)
2. Output is captured
3. Usage/token info is extracted (where available)
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lagent_tablets.adapters import (
    BurstResult,
    ClaudeAdapter,
    CodexAdapter,
    GeminiAdapter,
    ProviderConfig,
    UsageSnapshot,
    make_adapter,
    tmux_has_session,
    tmux_kill_window,
)


TEST_SESSION = "lagent-test-adapters"
SIMPLE_PROMPT = "What is 2 + 2? Reply with just the number."


def print_result(label: str, result: BurstResult) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  ok:              {result.ok}")
    print(f"  exit_code:       {result.exit_code}")
    print(f"  duration:        {result.duration_seconds:.1f}s")
    print(f"  stall_recoveries: {result.stall_recoveries}")
    print(f"  error:           {result.error or '(none)'}")
    print(f"  output length:   {len(result.captured_output)} chars")

    # Show first/last few lines of output
    lines = result.captured_output.strip().splitlines()
    if lines:
        preview = lines[:3] + (["  ..."] if len(lines) > 6 else []) + lines[-3:]
        for line in preview:
            print(f"    | {line[:120]}")

    if result.usage:
        print(f"  usage:")
        for k, v in result.usage.items():
            if k != "raw" and v:
                print(f"    {k}: {v}")

    if result.recovery_log:
        print(f"  recovery_log:")
        for entry in result.recovery_log:
            print(f"    - {entry}")

    print()


def test_codex(work_dir: Path, log_dir: Path) -> BurstResult:
    """Test Codex in non-interactive mode."""
    config = ProviderConfig(
        provider="codex",
        model=None,  # use default model
        extra_args=["--color", "never"],
    )
    adapter = make_adapter(config, work_dir=work_dir, session_name=TEST_SESSION)
    print("Testing Codex (non-interactive, model=o3-mini)...")
    result = adapter.run_burst(
        SIMPLE_PROMPT,
        timeout_seconds=120,
        log_dir=log_dir / "codex",
    )
    print_result("Codex (o3-mini)", result)
    return result


def test_claude(work_dir: Path, log_dir: Path) -> BurstResult:
    """Test Claude in interactive mode."""
    config = ProviderConfig(
        provider="claude",
        model="sonnet",
        effort="low",
    )
    adapter = make_adapter(config, work_dir=work_dir, session_name=TEST_SESSION)
    print("Testing Claude (interactive, model=sonnet, effort=low)...")
    result = adapter.run_burst(
        SIMPLE_PROMPT,
        timeout_seconds=120,
        stall_threshold_seconds=60,  # shorter for testing
        log_dir=log_dir / "claude",
    )
    print_result("Claude (sonnet, low)", result)
    # Clean up the tmux window
    try:
        tmux_kill_window(TEST_SESSION, "claude-agent")
    except Exception:
        pass
    return result


def test_gemini(work_dir: Path, log_dir: Path) -> BurstResult:
    """Test Gemini in interactive mode."""
    config = ProviderConfig(
        provider="gemini",
        model=None,  # use default
    )
    adapter = make_adapter(config, work_dir=work_dir, session_name=TEST_SESSION)
    print("Testing Gemini (interactive, default model)...")
    result = adapter.run_burst(
        SIMPLE_PROMPT,
        timeout_seconds=120,
        stall_threshold_seconds=60,  # shorter for testing
        log_dir=log_dir / "gemini",
    )
    print_result("Gemini (default)", result)
    # Clean up the tmux window
    try:
        tmux_kill_window(TEST_SESSION, "gemini-agent")
    except Exception:
        pass
    return result


def test_factory() -> None:
    """Test the make_adapter factory."""
    work_dir = Path(tempfile.mkdtemp())
    for provider in ["codex", "claude", "gemini"]:
        config = ProviderConfig(provider=provider, model="test-model")
        adapter = make_adapter(config, work_dir=work_dir)
        assert adapter.provider == provider
        assert adapter.model == "test-model"
        assert isinstance(adapter.build_initial_command(), list)
        assert isinstance(adapter.build_resume_command(), list)
        print(f"  {provider}: initial={adapter.build_initial_command()}")
        print(f"  {provider}: resume ={adapter.build_resume_command()}")
    print("Factory test passed.\n")


def main():
    print("=" * 60)
    print("  Provider Adapter Live Tests")
    print("=" * 60)
    print()

    # Basic factory test (no API calls)
    print("--- Factory test ---")
    test_factory()

    # Set up temp directories
    work_dir = Path(tempfile.mkdtemp(prefix="lagent-test-work-"))
    log_dir = Path(tempfile.mkdtemp(prefix="lagent-test-logs-"))
    print(f"Work dir: {work_dir}")
    print(f"Log dir:  {log_dir}")

    # Initialize work_dir as a git repo (Codex requires this sometimes)
    import subprocess
    subprocess.run(["git", "init", str(work_dir)], capture_output=True)
    (work_dir / "README.md").write_text("Test repo\n")
    subprocess.run(["git", "-C", str(work_dir), "add", "."], capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_dir), "commit", "-m", "init"],
        capture_output=True,
        env={**dict(os.environ), "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"},
    )

    results = {}

    # Run tests
    providers_to_test = sys.argv[1:] if len(sys.argv) > 1 else ["codex", "claude", "gemini"]

    for provider in providers_to_test:
        try:
            if provider == "codex":
                results["codex"] = test_codex(work_dir, log_dir)
            elif provider == "claude":
                results["claude"] = test_claude(work_dir, log_dir)
            elif provider == "gemini":
                results["gemini"] = test_gemini(work_dir, log_dir)
            else:
                print(f"Unknown provider: {provider}")
        except Exception as e:
            print(f"ERROR testing {provider}: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    print("\n" + "=" * 60)
    print("  Summary")
    print("=" * 60)
    for provider, result in results.items():
        status = "PASS" if result.ok else "FAIL"
        print(f"  {provider:8s}: {status} ({result.duration_seconds:.1f}s)")
        if result.usage:
            tokens = result.usage.get("total_tokens", 0)
            if tokens:
                print(f"           tokens: {tokens}")
    print()

    # Clean up tmux session
    if tmux_has_session(TEST_SESSION):
        from lagent_tablets.adapters import tmux_cmd
        tmux_cmd("kill-session", "-t", TEST_SESSION)
        print(f"Cleaned up tmux session '{TEST_SESSION}'")


if __name__ == "__main__":
    import os
    main()
