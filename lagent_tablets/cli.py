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
    FORBIDDEN_KEYWORDS_DEFAULT,
    PolicyManager,
    load_config,
    PHASES,
)
from lagent_tablets.cycle import (
    CycleOutcome,
    _normalize_theorem_stating_replay_state,
    preview_next_cycle,
    run_cleanup_cycle,
    run_cycle,
    run_theorem_stating_cycle,
)
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
    freeze_current_coarse_package,
    regenerate_support_files,
    tablet_dir,
)
from lagent_tablets.viewer_state import viewer_state_path, write_live_viewer_state
from lagent_tablets.git_ops import init_repo, rewind_to_cycle
from lagent_tablets.history_replay import find_first_history_divergence
from lagent_tablets.project_paths import (
    project_chats_dir,
    project_runtime_dir,
    project_runtime_skills_dir,
    project_runtime_src_dir,
    project_scratch_dir,
    project_viewer_dir,
)
from lagent_tablets.check import write_scripts


def ensure_directories(config: Config) -> None:
    """Create required directories if they don't exist."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "staging").mkdir(parents=True, exist_ok=True)
    (config.state_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    project_runtime_dir(config.state_dir).mkdir(parents=True, exist_ok=True)
    project_runtime_src_dir(config.state_dir).mkdir(parents=True, exist_ok=True)
    project_runtime_skills_dir(config.state_dir).mkdir(parents=True, exist_ok=True)
    project_viewer_dir(config.state_dir).mkdir(parents=True, exist_ok=True)
    project_chats_dir(config.state_dir).mkdir(parents=True, exist_ok=True)
    project_scratch_dir(config.state_dir).mkdir(parents=True, exist_ok=True)
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
        project_runtime_dir(config.state_dir): 0o2755,
        project_runtime_src_dir(config.state_dir): 0o2755,
        project_runtime_skills_dir(config.state_dir): 0o2755,
        project_viewer_dir(config.state_dir): 0o2775,
        project_chats_dir(config.state_dir): 0o2775,
        project_scratch_dir(config.state_dir): 0o2775,
    }
    for path, mode in dir_modes.items():
        try:
            os.chown(str(path), -1, gid)
            os.chmod(str(path), mode)
        except PermissionError:
            pass


def check_dependencies(config: Config) -> None:
    """Verify required tools are available."""
    import shutil
    from lagent_tablets.sandbox import probe_sandbox

    missing = []
    for tool in ["tmux", "lake", "bwrap"]:
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        print(f"ERROR: Required tools not found: {missing}")
        sys.exit(1)
    ok, detail = probe_sandbox(
        sandbox=config.sandbox,
        work_dir=config.repo_path,
        burst_user=config.tmux.burst_user,
        burst_home=config.tmux.burst_home,
    )
    if not ok:
        print("ERROR: sandbox preflight failed")
        print(f"  detail: {detail}")
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


def _check_restart_request(config: Config) -> bool:
    """Check if a restart has been requested via the restart file."""
    restart_path = config.state_dir / "restart"
    if restart_path.exists():
        try:
            restart_path.unlink()
        except OSError:
            pass
        return True
    return False


def _capture_trusted_main_result_hashes(config: Config, tablet: TabletState) -> dict[str, str]:
    """Snapshot correspondence fingerprints for currently trusted main results."""
    from lagent_tablets.nl_cache import NLCache

    cache = NLCache(config.state_dir / "nl_cache.json")
    trusted: dict[str, str] = {}
    for name, node in sorted(tablet.nodes.items()):
        if node.kind != "paper_main_result":
            continue
        fp = cache.correspondence_fingerprint(config.repo_path, name)
        if fp:
            trusted[name] = fp
    return trusted


def _trusted_main_result_review_issues(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
) -> list[str]:
    """Return paper main results whose correspondence drifted after human review."""
    trusted = dict(state.trusted_main_result_hashes)
    if not trusted:
        return []

    from lagent_tablets.nl_cache import NLCache

    cache = NLCache(config.state_dir / "nl_cache.json")
    current_main_results = {
        name for name, node in tablet.nodes.items()
        if node.kind == "paper_main_result"
    }
    issues: list[str] = []

    removed = sorted(set(trusted) - current_main_results)
    issues.extend(f"{name}: removed from the main-result set after human review" for name in removed)

    added = sorted(current_main_results - set(trusted))
    issues.extend(f"{name}: newly classified as a paper main result after human review" for name in added)

    for name in sorted(current_main_results & set(trusted)):
        current_fp = cache.correspondence_fingerprint(config.repo_path, name)
        if not current_fp:
            issues.append(f"{name}: current correspondence fingerprint could not be computed")
            continue
        if current_fp != trusted[name]:
            issues.append(f"{name}: correspondence changed since the last human-reviewed package")

    return issues


def _process_human_feedback_signal(config: Config, state: SupervisorState) -> bool:
    feedback_path = config.state_dir / "human_feedback.json"
    if not feedback_path.exists():
        return False
    try:
        fb = json.loads(feedback_path.read_text(encoding="utf-8"))
        feedback_text = fb.get("feedback", "")
        print(f"Human feedback received: {str(feedback_text)[:100]}")
        state.human_input = str(feedback_text)
        state.human_input_at_cycle = state.cycle
        state.awaiting_human_input = False
        state.last_review = {
            "decision": "CONTINUE",
            "reason": "Human feedback",
            "next_prompt": str(feedback_text),
        }
        feedback_path.unlink()
    except Exception:
        pass
    return True


def _process_human_approval_signal(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    *,
    next_phase: Optional[str] = None,
) -> bool:
    approve_path = config.state_dir / "human_approve.json"
    if not approve_path.exists():
        return False
    try:
        approve_path.unlink()
    except OSError:
        pass

    if next_phase is not None:
        print(f"Human approved. Advancing phase: {state.phase} -> {next_phase}")
        if state.phase == "theorem_stating" and next_phase == "proof_formalization":
            state.trusted_main_result_hashes = _capture_trusted_main_result_hashes(config, tablet)
            print(f"  Trusted paper main results: {len(state.trusted_main_result_hashes)}")
            freeze_current_coarse_package(tablet, config.repo_path, cycle=state.cycle)
            print(f"  Frozen coarse package: {len([n for n in tablet.nodes.values() if n.coarse])} nodes")
        state.phase = next_phase
        state.last_review = None
        state.open_rejections = []
        state.awaiting_human_input = False
        return True

    if (
        isinstance(state.last_review, dict)
        and state.last_review.get("decision") == "NEED_INPUT"
        and state.last_review.get("human_gate") == "paper_main_result_correspondence"
    ):
        state.trusted_main_result_hashes = _capture_trusted_main_result_hashes(config, tablet)
        state.awaiting_human_input = False
        state.last_review = {
            "decision": "CONTINUE",
            "reason": "Human re-approved the paper main results after correspondence drift.",
            "next_prompt": "Continue from the current phase with the newly re-trusted paper main results.",
        }
        print(f"Human approved updated paper main results. Trusted set: {len(state.trusted_main_result_hashes)}")
        return True

    state.awaiting_human_input = False
    state.last_review = {
        "decision": "CONTINUE",
        "reason": "Human approved continuation.",
        "next_prompt": "Continue from the current phase.",
    }
    print("Human approved continuation.")
    return True


def _apply_trusted_main_result_review_gate(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
) -> bool:
    """Require renewed human review when trusted paper main results drift."""
    issues = _trusted_main_result_review_issues(config, state, tablet)
    if not issues:
        return False
    state.awaiting_human_input = True
    state.last_review = {
        "decision": "NEED_INPUT",
        "human_gate": "paper_main_result_correspondence",
        "reason": "Human-reviewed paper main results lost correspondence and must be reviewed again.",
        "next_prompt": (
            "Review the changed paper main results. Approve to re-trust the current package, "
            "or provide human feedback describing what must be restored."
        ),
        "issues": issues,
    }
    print("Trusted paper-main-result review gate opened:")
    for issue in issues:
        print(f"  - {issue}")
    write_live_viewer_state(
        viewer_state_path(config.state_dir),
        config.repo_path,
        tablet,
        state,
        source="reviewer",
        fast=False,
    )
    save_state(state_path(config), state)
    return True


def _restart_supervisor_process(argv: list[str]) -> None:
    """Replace the current process with a fresh supervisor process."""
    print("Restart requested. Restarting supervisor now.")
    os.execv(sys.executable, [sys.executable, "-u", "-m", "lagent_tablets.cli", *argv])


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

        if _process_human_approval_signal(
            config,
            state,
            tablet,
            next_phase=PHASES[current_idx + 1],
        ):
            if stop_at_phase_boundary:
                print("Stopping at phase boundary as requested.")
                return True
            return False

        if _process_human_feedback_signal(config, state):
            return False

        # No human signal — pause and wait for review via web viewer
        print(f"Reviewer wants to advance phase. Waiting for human approval via web viewer.")
        print(f"  Visit the viewer and click 'Approve' or provide feedback.")
        state.awaiting_human_input = True
        return True

    # Reviewer said NEED_INPUT
    if state.last_review and state.last_review.get("decision") == "NEED_INPUT":
        if _process_human_approval_signal(config, state, tablet):
            return False
        if _process_human_feedback_signal(config, state):
            return False
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
            state.cleanup_last_good_commit = f"cycle-{state.cycle}"
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
    parser.add_argument("--preview-next-cycle", action="store_true",
                        help="Render the next worker cycle prompt/state without launching any agents")
    parser.add_argument("--history-divergence", action="store_true",
                        help="Replay committed history and report the first place current code diverges from the recorded run")
    parser.add_argument("--history-max-cycle", type=int, default=None,
                        help="Only check committed history up to cycle N (used with --history-divergence)")
    parser.add_argument("--cycles", type=int, default=None,
                        help="Run at most N cycles then stop (overrides config max_cycles)")
    parser.add_argument("--stop-at-phase-boundary", action="store_true",
                        help="Stop when the phase changes (e.g., theorem_stating -> proof_formalization)")
    parser.add_argument("--rewind-to-cycle", type=int, default=None, metavar="N",
                        help="Rewind repo to cycle N (clears agent sessions) and exit")
    parser.add_argument("--rewind-stage", choices=["worker", "verification", "reviewer"], default="reviewer",
                        help="Exact committed checkpoint to restore with --rewind-to-cycle (default: reviewer/final)")
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
            stage=args.rewind_stage,
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
    check_dependencies(config)

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

    startup_notes = _normalize_theorem_stating_replay_state(state)
    if startup_notes:
        for note in startup_notes:
            print(f"  normalized: {note}")
        save_state(state_path(config), state)

    if args.preview_next_cycle:
        preview = preview_next_cycle(config, state, tablet, policy)
        trusted_issues = _trusted_main_result_review_issues(config, state, tablet)
        if trusted_issues:
            preview["preflight_error"] = (
                "Human-reviewed paper main results lost correspondence and require renewed human review."
            )
            preview["trusted_main_result_issues"] = trusted_issues
        summary = {
            k: v for k, v in preview.items()
            if k != "worker_prompt"
        }
        print(json.dumps(summary, indent=2))
        if preview.get("worker_prompt"):
            print("\n--- WORKER PROMPT ---\n")
            print(preview["worker_prompt"])
        return 1 if preview.get("preflight_error") else 0

    if args.history_divergence:
        result = find_first_history_divergence(
            config,
            policy,
            max_cycle=args.history_max_cycle,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.status == "match" else 1

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
    fix_lake_permissions(
        config.repo_path,
        burst_user=config.tmux.burst_user,
        include_package_builds=True,
    )

    # Reconcile tablet status with actual file state
    from lagent_tablets.cycle import reconcile_tablet_status
    reconciled = reconcile_tablet_status(config, tablet)
    if reconciled:
        save_tablet(tablet_path(config), tablet)
        print(f"  reconciled: {reconciled}")

    # Regenerate support files
    regenerate_support_files(tablet, config.repo_path)
    write_live_viewer_state(
        viewer_state_path(config.state_dir),
        config.repo_path,
        tablet,
        state,
        source="startup",
        fast=True,
    )

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
            fix_lake_permissions(
                config.repo_path,
                burst_user=config.tmux.burst_user,
                include_package_builds=True,
            )
            _apply_trusted_main_result_review_gate(config, state, tablet)

            if remaining_cycles is not None:
                remaining_cycles -= 1

            if should_stop(config, state, tablet, outcome,
                          stop_at_phase_boundary=args.stop_at_phase_boundary,
                          remaining_cycles=remaining_cycles):
                break
            if _check_restart_request(config):
                _restart_supervisor_process(sys.argv[1:])
            sleep_secs = policy.timing.sleep_seconds
            if sleep_secs > 0:
                time.sleep(sleep_secs)
            continue

        if state.phase not in ("proof_formalization", "proof_complete_style_cleanup"):
            print(f"Phase {state.phase} is not yet implemented. Use --dry-run to validate config.")
            return 0

        # Ensure there's an active node
        current = state.active_node or tablet.active_node
        if state.phase == "proof_formalization" and current and current in tablet.nodes and tablet.nodes[current].status == "closed":
            current = ""  # Active node is already closed, need a new one
        if not current:
            if state.phase == "proof_complete_style_cleanup":
                cleanup_nodes = [n for n, node in sorted(tablet.nodes.items()) if node.kind != "preamble"]
                if cleanup_nodes:
                    current = cleanup_nodes[0]
                    print(f"  Selecting cleanup focus node: {current}")
                else:
                    print("No cleanup-capable nodes found. Stopping.")
                    break
            else:
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
        if state.phase == "proof_complete_style_cleanup":
            outcome = run_cleanup_cycle(
                config, state, tablet, policy,
                previous_outcome=previous_outcome.to_dict() if isinstance(previous_outcome, CycleOutcome) else previous_outcome,
            )
        else:
            outcome = run_cycle(
                config, state, tablet, policy,
                previous_outcome=previous_outcome.to_dict() if isinstance(previous_outcome, CycleOutcome) else previous_outcome,
            )
        health.on_cycle_outcome(state.cycle, outcome.outcome, outcome.detail)

        previous_outcome = outcome
        _apply_trusted_main_result_review_gate(config, state, tablet)

        # Fix .lake permissions after each cycle
        fix_lake_permissions(
            config.repo_path,
            burst_user=config.tmux.burst_user,
            include_package_builds=True,
        )

        if remaining_cycles is not None:
            remaining_cycles -= 1

        if should_stop(config, state, tablet, outcome,
                      stop_at_phase_boundary=args.stop_at_phase_boundary,
                      remaining_cycles=remaining_cycles):
            break
        if _check_restart_request(config):
            _restart_supervisor_process(sys.argv[1:])

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
