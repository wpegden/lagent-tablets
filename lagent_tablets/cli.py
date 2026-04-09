#!/usr/bin/env python3
"""CLI entry point for the lagent-tablets supervisor.

Usage:
    python -m lagent_tablets.cli --config path/to/config.json
    python -m lagent_tablets.cli --config path/to/config.json --cycles 3
    python -m lagent_tablets.cli --config path/to/config.json --stop-at-phase-boundary

Pause/resume:
    To pause gracefully after the current cycle completes, write a file:
        echo '{}' > /path/to/repo/.agent-supervisor/pause

    The supervisor will finish the current cycle, save state, and exit.
    To resume, just run the same command again -- it picks up from saved state.
"""

from __future__ import annotations

import argparse
import json
import os
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
from lagent_tablets.cycle import CycleOutcome, run_cycle, run_theorem_stating_cycle
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
from lagent_tablets.git_ops import init_repo, rewind_to_cycle
from lagent_tablets.verification import write_scripts, FORBIDDEN_KEYWORDS_DEFAULT


def ensure_directories(config: Config) -> None:
    """Create required directories if they don't exist."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "staging").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    tablet_dir(config.repo_path).mkdir(parents=True, exist_ok=True)

    try:
        import grp
        gid = grp.getgrnam("leanagent").gr_gid
    except (ImportError, KeyError):
        return

    dir_modes = {
        config.state_dir: 0o2755,
        config.state_dir / "logs": 0o2775,
        config.state_dir / "scripts": 0o2755,
        config.state_dir / "staging": 0o2775,
        config.state_dir / "checkpoints": 0o2755,
    }
    for path, mode in dir_modes.items():
        try:
            os.chown(str(path), -1, gid)
            os.chmod(str(path), mode)
        except PermissionError:
            pass


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


def _check_pause(config: Config) -> bool:
    """Check if a pause has been requested via the pause file."""
    pause_path = config.state_dir / "pause"
    if pause_path.exists():
        try:
            pause_path.unlink()
        except OSError:
            pass
        return True
    return False


def should_stop(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    outcome: CycleOutcome,
    *,
    stop_at_phase_boundary: bool = False,
    remaining_cycles: Optional[int] = None,
) -> bool:
    """Determine if the supervisor should stop after this cycle."""

    # Pause requested via file
    if _check_pause(config):
        print(f"Pause requested. Stopping after cycle {state.cycle}.")
        return True

    # Cycle budget exhausted
    if remaining_cycles is not None and remaining_cycles <= 0:
        print(f"Cycle limit reached. Stopping after cycle {state.cycle}.")
        return True

    # Max cycles from config
    if config.max_cycles > 0 and state.cycle >= config.max_cycles:
        print(f"Max cycles reached ({config.max_cycles}). Stopping.")
        return True

    # Reviewer said DONE
    if state.last_review and state.last_review.get("decision") == "DONE":
        print("Reviewer decision: DONE. Stopping.")
        return True

    # Reviewer said ADVANCE_PHASE
    if state.last_review and state.last_review.get("decision") == "ADVANCE_PHASE":
        current_idx = PHASES.index(state.phase) if state.phase in PHASES else -1
        if current_idx >= len(PHASES) - 1:
            print("Already at last phase. Stopping.")
            return True

        # Check for human approval signal (written by web viewer)
        approve_path = config.state_dir / "human_approve.json"
        feedback_path = config.state_dir / "human_feedback.json"

        if approve_path.exists():
            # Human approved — advance
            try:
                approve_path.unlink()
            except OSError:
                pass
            next_phase = PHASES[current_idx + 1]
            print(f"Human approved. Advancing phase: {state.phase} -> {next_phase}")
            state.phase = next_phase
            state.last_review = None
            state.open_rejections = []
            if stop_at_phase_boundary:
                print("Stopping at phase boundary as requested.")
                return True
            return False

        if feedback_path.exists():
            # Human gave feedback — store persistently and continue
            try:
                fb = json.loads(feedback_path.read_text(encoding="utf-8"))
                feedback_text = fb.get("feedback", "")
                print(f"Human feedback received: {feedback_text[:100]}")
                state.human_input = feedback_text
                state.human_input_at_cycle = state.cycle
                state.awaiting_human_input = False
                state.last_review = {
                    "decision": "CONTINUE",
                    "reason": "Human feedback",
                    "next_prompt": feedback_text,
                }
                feedback_path.unlink()
            except Exception:
                pass
            return False

        # No human signal — pause and wait for review via web viewer
        print(f"Reviewer wants to advance phase. Waiting for human approval via web viewer.")
        print(f"  Visit the viewer and click 'Approve' or provide feedback.")
        state.awaiting_human_input = True
        return True

    # Reviewer said NEED_INPUT
    if state.last_review and state.last_review.get("decision") == "NEED_INPUT":
        print("Reviewer requested human input. Stopping.")
        state.awaiting_human_input = True
        return True

    # Reviewer said STUCK
    if state.last_review and state.last_review.get("decision") == "STUCK":
        print("Reviewer said STUCK. Stopping (stuck recovery not yet implemented).")
        return True

    # All nodes closed
    if tablet.total_nodes > 0 and tablet.open_nodes == 0:
        print(f"All {tablet.total_nodes} nodes are closed!")
        if state.phase == "proof_formalization":
            print("Proof formalization complete. Advancing to cleanup.")
            state.phase = "proof_complete_style_cleanup"
            if stop_at_phase_boundary:
                print("Stopping at phase boundary as requested.")
                return True
        return False

    return False


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="lagent-tablets supervisor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="To pause: echo '{}' > REPO/.agent-supervisor/pause",
    )
    parser.add_argument("--config", required=True, type=Path, help="Path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and exit")
    parser.add_argument("--cycles", type=int, default=None,
                        help="Run at most N cycles then stop (overrides config max_cycles)")
    parser.add_argument("--stop-at-phase-boundary", action="store_true",
                        help="Stop when the phase changes (e.g., theorem_stating -> proof_formalization)")
    parser.add_argument("--rewind-to-cycle", type=int, default=None, metavar="N",
                        help="Rewind repo to cycle N (clears agent sessions) and exit")
    parser.add_argument("--resume-from", choices=["verification", "reviewer"], default=None,
                        help="Resume current cycle from a mid-cycle checkpoint (skip earlier stages)")
    args = parser.parse_args(argv)

    # Load config
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Config error: {e}")
        return 1

    # Handle --rewind-to-cycle early exit
    if args.rewind_to_cycle is not None:
        success = rewind_to_cycle(
            config.repo_path, args.rewind_to_cycle,
            burst_user=config.tmux.burst_user,
        )
        return 0 if success else 1

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
    if args.cycles:
        print(f"  cycle limit: {args.cycles}")
    if args.stop_at_phase_boundary:
        print(f"  stop at phase boundary: yes")

    # Check dependencies
    check_dependencies()

    # Ensure directories
    ensure_directories(config)

    # Initialize git repo for cycle versioning
    init_repo(config.repo_path)

    # Load state and tablet
    state = load_state(state_path(config))
    tablet = load_tablet(tablet_path(config))

    # If state has no meaningful phase or cycle is 0, use the config's start_phase
    if state.cycle == 0:
        state.phase = config.workflow.start_phase

    # Apply --resume-from if specified
    if args.resume_from:
        state.resume_from = args.resume_from
        print(f"  resume_from: {args.resume_from} (set via --resume-from)")

    print(f"  cycle:     {state.cycle}")
    print(f"  phase:     {state.phase}")
    if state.resume_from:
        print(f"  resume:    from {state.resume_from}")
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

    print(f"  pause file: {config.state_dir / 'pause'}")
    print()

    # Main loop
    previous_outcome = None
    remaining_cycles = args.cycles  # None means no limit

    while True:
        # Hot-reload config and policy
        if config_manager.check_reload():
            config = config_manager.config
            print(f"  Config reloaded. Worker: {config.worker.provider}/{config.worker.model}")

        policy = policy_manager.reload()

        # Check Gemini budget and rotate accounts if needed (every 5 cycles)
        if state.cycle % 5 == 0 and (
            config.worker.provider == "gemini" or config.reviewer.provider == "gemini" or config.verification.provider == "gemini"
        ):
            try:
                from lagent_tablets.gemini_accounts import ensure_budget
                ensure_budget(burst_user=config.tmux.burst_user)
            except Exception as e:
                print(f"  Gemini budget check failed: {e}")

        # Apply per-phase model overrides
        phase_override = config.workflow.phase_overrides.get(state.phase)
        if phase_override:
            if phase_override.worker_model and config.worker.model != phase_override.worker_model:
                print(f"  Phase override: worker model -> {phase_override.worker_model}")
                config = config  # config is immutable-ish, but worker is a dataclass
                config.worker.model = phase_override.worker_model
            if phase_override.reviewer_model and config.reviewer.model != phase_override.reviewer_model:
                print(f"  Phase override: reviewer model -> {phase_override.reviewer_model}")
                config.reviewer.model = phase_override.reviewer_model

        # Dispatch by phase
        if state.phase == "theorem_stating":
            outcome = run_theorem_stating_cycle(
                config, state, tablet, policy,
                previous_outcome=previous_outcome.to_dict() if isinstance(previous_outcome, CycleOutcome) else previous_outcome,
            )
            health.on_cycle_outcome(state.cycle, outcome.outcome, outcome.detail)
            previous_outcome = outcome
            fix_lake_permissions(config.repo_path)

            if remaining_cycles is not None:
                remaining_cycles -= 1

            if should_stop(config, state, tablet, outcome,
                          stop_at_phase_boundary=args.stop_at_phase_boundary,
                          remaining_cycles=remaining_cycles):
                break
            sleep_secs = policy.timing.sleep_seconds
            if sleep_secs > 0:
                time.sleep(sleep_secs)
            continue

        if state.phase not in ("proof_formalization", "proof_complete_style_cleanup"):
            print(f"Phase {state.phase} is not yet implemented. Use --dry-run to validate config.")
            return 0

        # Ensure there's an active OPEN node
        current = state.active_node or tablet.active_node
        if current and current in tablet.nodes and tablet.nodes[current].status == "closed":
            current = ""  # Active node is already closed, need a new one
        if not current:
            open_nodes = [n for n, node in sorted(tablet.nodes.items())
                         if node.status == "open" and node.kind != "preamble"]
            if open_nodes:
                current = open_nodes[0]
                print(f"  Selecting next open node: {current}")
            else:
                print("No open nodes. All done?")
                break
        state.active_node = current
        tablet.active_node = current

        # Check if agent needs restart (wedged session)
        if health.should_restart_agent():
            print(f"  Health monitor: agent appears wedged ({health.stats.consecutive_failures} consecutive failures)")
            print(f"  Last error: {health.stats.last_failure_error}")
            from lagent_tablets.burst import tmux_kill_window
            try:
                tmux_kill_window(config.tmux.session_name, f"{config.worker.provider}-worker")
            except Exception:
                pass
            health.stats.consecutive_failures = 0

        # Run one cycle
        health.on_cycle_start(state.cycle + 1)
        outcome = run_cycle(
            config, state, tablet, policy,
            previous_outcome=previous_outcome.to_dict() if isinstance(previous_outcome, CycleOutcome) else previous_outcome,
        )
        health.on_cycle_outcome(state.cycle, outcome.outcome, outcome.detail)

        previous_outcome = outcome

        # Fix .lake permissions after each cycle
        fix_lake_permissions(config.repo_path)

        if remaining_cycles is not None:
            remaining_cycles -= 1

        if should_stop(config, state, tablet, outcome,
                      stop_at_phase_boundary=args.stop_at_phase_boundary,
                      remaining_cycles=remaining_cycles):
            break

        # Sleep between cycles
        sleep_secs = policy.timing.sleep_seconds
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    # Final save
    save_state(state_path(config), state)
    save_tablet(tablet_path(config), tablet)
    print(f"\nSupervisor stopped at cycle {state.cycle}, phase {state.phase}.")
    print(f"  Tablet: {tablet.closed_nodes}/{tablet.total_nodes} closed.")
    print(f"  Health: {json.dumps(health.summary())}")
    print(f"  To resume: python -m lagent_tablets.cli --config {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
