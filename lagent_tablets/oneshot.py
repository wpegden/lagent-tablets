"""One-shot test mode for the supervisor.

Runs exactly one action through the real supervisor code path:
  worker   — build the worker prompt, run the worker burst, print output
  reviewer — build a reviewer prompt (with mock validation), run the reviewer burst, parse decision
  nl       — build the NL verification prompt, run the verification burst, parse result

Usage:
    python -m lagent_tablets.oneshot --config configs/connectivity_gnp.json --action worker
    python -m lagent_tablets.oneshot --config configs/connectivity_gnp.json --action nl
    python -m lagent_tablets.oneshot --config configs/connectivity_gnp.json --action reviewer

Options:
    --save       Save state changes (default: dry-run, no state mutation)
    --node NAME  Override the active node
    --timeout N  Override burst timeout (seconds)
    --port N     Override agentapi port
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from lagent_tablets.config import load_config, ConfigError, Policy, PolicyManager, FORBIDDEN_KEYWORDS_DEFAULT
from lagent_tablets.state import (
    SupervisorState,
    TabletState,
    load_state,
    load_tablet,
    save_state,
    save_tablet,
    state_path,
    tablet_path,
)
from lagent_tablets.prompts import (
    build_worker_prompt,
    build_reviewer_prompt,
    build_verification_prompt,
)
from lagent_tablets.burst import (
    run_worker_burst,
    run_reviewer_burst,
    extract_json_decision,
    _clean_terminal_json,
)
from lagent_tablets.check import write_scripts
from lagent_tablets.tablet import regenerate_support_files


def _setup(args) -> tuple:
    """Load config, state, tablet, policy — same as the real supervisor."""
    config = load_config(args.config)
    state = load_state(state_path(config))
    tablet = load_tablet(tablet_path(config))
    policy_manager = PolicyManager(config)
    policy = policy_manager.current()

    # Override active node if requested
    if args.node:
        state.active_node = args.node
        tablet.active_node = args.node
    elif not state.active_node:
        if tablet.active_node:
            state.active_node = tablet.active_node
        else:
            open_nodes = [n for n, nd in sorted(tablet.nodes.items()) if nd.status == "open"]
            if open_nodes:
                state.active_node = open_nodes[0]
                tablet.active_node = open_nodes[0]

    # Ensure scripts exist (worker needs check_node.sh)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "scripts").mkdir(parents=True, exist_ok=True)
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT
                 if kw not in config.workflow.forbidden_keyword_allowlist]
    write_scripts(
        config.repo_path, config.state_dir,
        allowed_prefixes=config.workflow.allowed_import_prefixes,
        forbidden_keywords=forbidden,
    )

    return config, state, tablet, policy


def action_worker(args):
    """Run one worker burst through the same code path as run_cycle."""
    config, state, tablet, policy = _setup(args)
    active = state.active_node
    print(f"One-shot WORKER | node={active} | provider={config.worker.provider}/{config.worker.model}")
    print(f"  repo: {config.repo_path}")
    print(f"  burst_user: {config.tmux.burst_user}")

    # Same setup as run_cycle — this is critical for correct behavior
    from lagent_tablets.cycle import setup_permissions
    from lagent_tablets.health import fix_lake_permissions
    from lagent_tablets.tablet import regenerate_support_files

    fix_lake_permissions(config.repo_path, burst_user=config.tmux.burst_user, include_package_builds=True)
    setup_permissions(config, active)
    regenerate_support_files(tablet, config.repo_path)

    # Ensure lake is built so check_node.sh works
    import subprocess
    print(f"  Running lake build...")
    subprocess.run(["lake", "build", "Tablet"], capture_output=True, timeout=300,
                   cwd=str(config.repo_path))

    # Build prompt (identical to run_cycle)
    prompt = build_worker_prompt(config, state, tablet, policy)
    print(f"  prompt: {len(prompt)} chars")

    if args.show_prompt:
        print("\n--- PROMPT ---")
        print(prompt)
        print("--- END PROMPT ---\n")

    # Save prompt for inspection
    prompt_path = config.state_dir / "logs" / "oneshot-worker-prompt.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    print(f"  prompt saved: {prompt_path}")

    timeout = args.timeout or policy.timing.burst_timeout_seconds
    print(f"  timeout: {timeout}s")
    print(f"  starting burst...")

    kwargs = {}
    if args.port:
        kwargs["port"] = args.port

    result = run_worker_burst(
        config.worker,
        prompt,
        session_name=config.tmux.session_name,
        work_dir=config.repo_path,
        burst_user=config.tmux.burst_user,
        timeout_seconds=timeout,
        startup_timeout_seconds=config.startup_timeout_seconds,
        log_dir=config.state_dir / "logs" / "oneshot",
        **kwargs,
    )

    print(f"\n--- RESULT ---")
    print(f"  ok: {result.ok}")
    print(f"  duration: {result.duration_seconds:.1f}s")
    print(f"  exit_code: {result.exit_code}")
    if result.error:
        print(f"  error: {result.error}")
    print(f"  output: {len(result.captured_output)} chars")
    print(f"\n--- OUTPUT ---")
    print(result.captured_output[:5000])
    if len(result.captured_output) > 5000:
        print(f"\n... ({len(result.captured_output) - 5000} more chars)")

    # Save output
    out_path = config.state_dir / "logs" / "oneshot-worker-output.txt"
    out_path.write_text(result.captured_output, encoding="utf-8")
    print(f"\n  output saved: {out_path}")

    return 0 if result.ok else 1


def action_reviewer(args):
    """Run one reviewer burst with a mock validation outcome."""
    config, state, tablet, policy = _setup(args)
    active = state.active_node
    print(f"One-shot REVIEWER | node={active} | provider={config.reviewer.provider}/{config.reviewer.model}")

    # Build reviewer prompt with a mock PROGRESS validation
    prompt = build_reviewer_prompt(
        config, state, tablet, policy,
        worker_handoff={"summary": "One-shot test", "status": "NOT_STUCK"},
        worker_output="(one-shot test — no real worker output)",
        validation_summary={"outcome": "PROGRESS", "detail": "One-shot test cycle",
                            "consecutive_invalids": 0},
    )
    print(f"  prompt: {len(prompt)} chars")

    if args.show_prompt:
        print("\n--- PROMPT ---")
        print(prompt)
        print("--- END PROMPT ---\n")

    prompt_path = config.state_dir / "logs" / "oneshot-reviewer-prompt.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")

    timeout = args.timeout or 300
    print(f"  timeout: {timeout}s")
    print(f"  starting burst...")

    kwargs = {}
    if args.port:
        kwargs["port"] = args.port

    result = run_reviewer_burst(
        config.reviewer,
        prompt,
        session_name=config.tmux.session_name,
        work_dir=config.repo_path,
        burst_user=config.tmux.burst_user,
        timeout_seconds=timeout,
        log_dir=config.state_dir / "logs" / "oneshot",
        **kwargs,
    )

    print(f"\n--- RESULT ---")
    print(f"  ok: {result.ok}")
    print(f"  duration: {result.duration_seconds:.1f}s")
    if result.error:
        print(f"  error: {result.error}")

    # Parse decision
    cleaned = _clean_terminal_json(result.captured_output)
    decision = extract_json_decision(cleaned)
    if decision:
        print(f"\n--- DECISION ---")
        print(json.dumps(decision, indent=2))
    else:
        print(f"\n--- RAW OUTPUT ---")
        print(result.captured_output[:3000])

    out_path = config.state_dir / "logs" / "oneshot-reviewer-output.txt"
    out_path.write_text(result.captured_output, encoding="utf-8")
    return 0 if result.ok else 1


def action_nl(args):
    """Run one NL verification burst."""
    config, state, tablet, policy = _setup(args)
    active = state.active_node
    print(f"One-shot NL VERIFICATION | node={active} | provider={config.verification.provider}/{config.verification.model}")

    # Build verification prompt for the active node
    paper_tex = ""
    if config.workflow.paper_tex_path and config.workflow.paper_tex_path.exists():
        paper_tex = config.workflow.paper_tex_path.read_text(encoding="utf-8", errors="replace")

    prompt = build_verification_prompt(
        config, tablet,
        new_nodes=[], modified_nodes=[active],
        paper_tex=paper_tex,
    )
    print(f"  prompt: {len(prompt)} chars")

    if args.show_prompt:
        print("\n--- PROMPT ---")
        print(prompt)
        print("--- END PROMPT ---\n")

    prompt_path = config.state_dir / "logs" / "oneshot-nl-prompt.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")

    # NL verification uses the verification provider config
    from lagent_tablets.adapters import ProviderConfig
    verify_config = ProviderConfig(
        provider=config.verification.provider,
        model=config.verification.model,
        effort=None,
        extra_args=config.verification.extra_args,
    )

    timeout = args.timeout or 300
    print(f"  timeout: {timeout}s")
    print(f"  starting burst...")

    kwargs = {}
    if args.port:
        kwargs["port"] = args.port

    result = run_reviewer_burst(
        verify_config,
        prompt,
        session_name=config.tmux.session_name,
        work_dir=config.repo_path,
        burst_user=config.tmux.burst_user,
        timeout_seconds=timeout,
        log_dir=config.state_dir / "logs" / "oneshot",
        **kwargs,
    )

    print(f"\n--- RESULT ---")
    print(f"  ok: {result.ok}")
    print(f"  duration: {result.duration_seconds:.1f}s")
    if result.error:
        print(f"  error: {result.error}")

    # Parse verification result
    cleaned = _clean_terminal_json(result.captured_output)
    decision = extract_json_decision(cleaned)
    if decision:
        print(f"\n--- VERIFICATION RESULT ---")
        print(json.dumps(decision, indent=2))
        overall = decision.get("overall", decision.get("decision", "?"))
        print(f"\n  Overall: {overall}")
    else:
        print(f"\n--- RAW OUTPUT ---")
        print(result.captured_output[:3000])

    out_path = config.state_dir / "logs" / "oneshot-nl-output.txt"
    out_path.write_text(result.captured_output, encoding="utf-8")
    return 0 if result.ok else 1


def action_theorem_stating(args):
    """Run one theorem_stating cycle through the real cycle code."""
    from lagent_tablets.cycle import run_theorem_stating_cycle, CycleOutcome

    config, state, tablet, policy = _setup(args)
    state.phase = "theorem_stating"
    print(f"One-shot THEOREM_STATING | provider={config.worker.provider}/{config.worker.model}")
    print(f"  repo: {config.repo_path}")
    print(f"  reviewer: {config.reviewer.provider}/{config.reviewer.model}")

    if args.timeout:
        policy = policy  # timeout override would need policy mutation; skip for now

    outcome = run_theorem_stating_cycle(config, state, tablet, policy)

    print(f"\n--- CYCLE OUTCOME ---")
    print(f"  outcome: {outcome.outcome}")
    print(f"  detail: {outcome.detail}")
    if outcome.nodes_created:
        print(f"  nodes_created: {outcome.nodes_created}")
    if outcome.build_output:
        print(f"  build_output (last 500):\n{outcome.build_output[-500:]}")

    if state.last_review:
        print(f"\n--- REVIEWER DECISION ---")
        print(json.dumps(state.last_review, indent=2))

    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot supervisor test: run exactly one action through the real code path",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True, type=Path, help="Path to config.json")
    parser.add_argument("--action", required=True,
                        choices=["worker", "reviewer", "nl", "theorem_stating"],
                        help="Which action to run")
    parser.add_argument("--node", type=str, help="Override active node name")
    parser.add_argument("--timeout", type=float, help="Override burst timeout (seconds)")
    parser.add_argument("--port", type=int, help="Override agentapi port")
    parser.add_argument("--show-prompt", action="store_true", help="Print the full prompt to stdout")
    parser.add_argument("--save", action="store_true", help="Save state changes (default: dry-run)")

    args = parser.parse_args(argv)

    try:
        if args.action == "worker":
            return action_worker(args)
        elif args.action == "reviewer":
            return action_reviewer(args)
        elif args.action == "nl":
            return action_nl(args)
        elif args.action == "theorem_stating":
            return action_theorem_stating(args)
    except ConfigError as e:
        print(f"Config error: {e}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
