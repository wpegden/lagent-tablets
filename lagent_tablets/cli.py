#!/usr/bin/env python3
"""CLI entry point for the lagent-tablets supervisor.

Usage:
    python -m lagent_tablets.cli --config path/to/config.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from lagent_tablets.config import (
    Config,
    ConfigError,
    ConfigManager,
    PolicyManager,
    load_config,
    PHASES,
)
from lagent_tablets.cycle import CycleOutcome, run_cycle
from lagent_tablets.state import (
    SupervisorState,
    TabletState,
    load_state,
    load_tablet,
    save_state,
    save_tablet,
    state_path,
    tablet_path,
    timestamp_now,
)
from lagent_tablets.tablet import (
    regenerate_support_files,
    tablet_dir,
)
from lagent_tablets.verification import write_scripts, FORBIDDEN_KEYWORDS_DEFAULT


def ensure_directories(config: Config) -> None:
    """Create required directories if they don't exist."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    tablet_dir(config.repo_path).mkdir(parents=True, exist_ok=True)


def check_dependencies() -> None:
    """Verify required tools are available."""
    import shutil
    missing = []
    for tool in ["tmux", "lake"]:
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        print(f"ERROR: Required tools not found: {missing}")
        sys.exit(1)


def should_stop(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    outcome: CycleOutcome,
) -> bool:
    """Determine if the supervisor should stop after this cycle."""
    # Max cycles reached
    if config.max_cycles > 0 and state.cycle >= config.max_cycles:
        print(f"Max cycles reached ({config.max_cycles}). Stopping.")
        return True

    # Reviewer said DONE
    if state.last_review and state.last_review.get("decision") == "DONE":
        print("Reviewer decision: DONE. Stopping.")
        return True

    # Reviewer said ADVANCE_PHASE from proof_formalization
    if state.last_review and state.last_review.get("decision") == "ADVANCE_PHASE":
        current_idx = PHASES.index(state.phase) if state.phase in PHASES else -1
        if current_idx >= len(PHASES) - 1:
            print("Already at last phase. Stopping.")
            return True
        next_phase = PHASES[current_idx + 1]
        print(f"Advancing phase: {state.phase} -> {next_phase}")
        state.phase = next_phase
        return False

    # Reviewer said NEED_INPUT
    if state.last_review and state.last_review.get("decision") == "NEED_INPUT":
        print("Reviewer requested human input. Stopping.")
        state.awaiting_human_input = True
        return True

    # Reviewer said STUCK and stuck recovery is exhausted
    if state.last_review and state.last_review.get("decision") == "STUCK":
        # TODO: stuck recovery logic
        print("Reviewer said STUCK. Stopping (stuck recovery not yet implemented).")
        return True

    # Cycle boundary restart request
    restart_path = config.state_dir / "restart_after_cycle.json"
    if restart_path.exists():
        print("Cycle boundary restart requested. Stopping.")
        try:
            restart_path.unlink()
        except OSError:
            pass
        return True

    # All nodes closed
    if tablet.total_nodes > 0 and tablet.open_nodes == 0:
        print(f"All {tablet.total_nodes} nodes are closed!")
        if state.phase == "proof_formalization":
            print("Proof formalization complete. Advancing to cleanup.")
            state.phase = "proof_complete_style_cleanup"
        return False

    return False


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="lagent-tablets supervisor")
    parser.add_argument("--config", required=True, type=Path, help="Path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and exit")
    args = parser.parse_args(argv)

    # Load config
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Config error: {e}")
        return 1

    if args.dry_run:
        print(f"Config loaded successfully from {args.config}")
        print(f"  repo_path: {config.repo_path}")
        print(f"  state_dir: {config.state_dir}")
        print(f"  worker:    {config.worker.provider}/{config.worker.model}")
        print(f"  reviewer:  {config.reviewer.provider}/{config.reviewer.model}")
        print(f"  burst_user: {config.tmux.burst_user}")
        print(f"  phase:     {config.workflow.start_phase}")
        return 0

    print(f"lagent-tablets supervisor starting")
    print(f"  config:    {args.config}")
    print(f"  repo:      {config.repo_path}")
    print(f"  worker:    {config.worker.provider}/{config.worker.model}")
    print(f"  reviewer:  {config.reviewer.provider}/{config.reviewer.model}")
    print(f"  burst_user: {config.tmux.burst_user}")

    # Check dependencies
    check_dependencies()

    # Ensure directories
    ensure_directories(config)

    # Load state and tablet
    state = load_state(state_path(config))
    tablet = load_tablet(tablet_path(config))

    print(f"  cycle:     {state.cycle}")
    print(f"  phase:     {state.phase}")
    print(f"  tablet:    {tablet.closed_nodes}/{tablet.total_nodes} closed")

    # Policy manager
    policy_manager = PolicyManager(config)
    policy = policy_manager.current()

    # Config manager (for hot-reload)
    config_manager = ConfigManager(config)

    # Generate verification scripts
    forbidden = [kw for kw in FORBIDDEN_KEYWORDS_DEFAULT if kw not in config.workflow.forbidden_keyword_allowlist]
    write_scripts(
        config.repo_path, config.state_dir,
        allowed_prefixes=config.workflow.allowed_import_prefixes,
        forbidden_keywords=forbidden,
    )

    # Fix .lake permissions for multi-user access
    from lagent_tablets.health import fix_lake_permissions, HealthMonitor
    fix_lake_permissions(config.repo_path)

    # Reconcile tablet status with actual file state
    from lagent_tablets.cycle import reconcile_tablet_status
    reconciled = reconcile_tablet_status(config, tablet)
    if reconciled:
        save_tablet(tablet_path(config), tablet)
        print(f"  reconciled: {reconciled}")

    # Regenerate support files
    regenerate_support_files(tablet, config.repo_path)

    # Health monitor
    health_log = config.state_dir / "logs" / "health.jsonl"
    health = HealthMonitor(health_log)

    # Burst execution is handled by burst.py; no adapter objects needed here

    # Main loop
    previous_outcome = None
    consecutive_close_bypass = 0

    while True:
        # Hot-reload config and policy
        if config_manager.check_reload():
            config = config_manager.config
            print(f"  Config reloaded. Worker: {config.worker.provider}/{config.worker.model}")
            # Provider changes are handled per-burst by burst.py

        policy = policy_manager.reload()

        # Check if we're in proof_formalization (the tablet phase)
        if state.phase != "proof_formalization":
            # Pre-tablet phases: just run worker/reviewer cycles without tablet logic
            # TODO: implement pre-tablet phase handling
            print(f"Phase {state.phase} is not yet implemented. Use --dry-run to validate config.")
            return 0

        # Ensure there's an active node
        if not state.active_node and tablet.active_node:
            state.active_node = tablet.active_node
        if not state.active_node:
            # Pick first open node alphabetically
            open_nodes = [n for n, node in sorted(tablet.nodes.items()) if node.status == "open"]
            if open_nodes:
                state.active_node = open_nodes[0]
                tablet.active_node = open_nodes[0]
            else:
                print("No open nodes. All done?")
                break

        # Check if agent needs restart (wedged session)
        if health.should_restart_agent():
            print(f"  Health monitor: agent appears wedged ({health.stats.consecutive_failures} consecutive failures)")
            print(f"  Last error: {health.stats.last_failure_error}")
            from lagent_tablets.burst import tmux_kill_window
            try:
                tmux_kill_window(config.tmux.session_name, f"{config.worker.provider}-worker")
            except Exception:
                pass
            health.stats.consecutive_failures = 0  # reset after restart

        # Run one cycle
        health.on_cycle_start(state.cycle + 1)
        outcome = run_cycle(
            config, state, tablet, policy,
            previous_outcome=previous_outcome.to_dict() if isinstance(previous_outcome, CycleOutcome) else previous_outcome,
        )
        health.on_cycle_outcome(state.cycle, outcome.outcome, outcome.detail)

        previous_outcome = outcome

        # Fix .lake permissions after each cycle (agent may have created new .olean files)
        fix_lake_permissions(config.repo_path)

        # Check stopping conditions
        if should_stop(config, state, tablet, outcome):
            break

        # Sleep between cycles
        sleep_secs = policy.timing.sleep_seconds
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    # Final save
    save_state(state_path(config), state)
    save_tablet(tablet_path(config), tablet)
    print(f"Supervisor stopped at cycle {state.cycle}. Tablet: {tablet.closed_nodes}/{tablet.total_nodes} closed.")
    print(f"Health: {json.dumps(health.summary())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
