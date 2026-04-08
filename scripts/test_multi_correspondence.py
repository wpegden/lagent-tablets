#!/usr/bin/env python3
"""Test multi-agent correspondence verification on a real node.

Uses load_config with a real config file and the actual tablet/repo
from the connectivity_gnp run. Runs _run_nl_verification which
dispatches to _run_multi_correspondence when correspondence_agents
is configured.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lagent_tablets.config import load_config
from lagent_tablets.state import load_tablet, load_state, state_path, tablet_path
from lagent_tablets.cycle import _run_nl_verification

CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "test_multi_corr.json"
NODE = "expected_isolated_limit"  # closed node with complete .lean + .tex


def main():
    # Load real config
    config = load_config(CONFIG_PATH)
    print(f"Config loaded: {CONFIG_PATH.name}")
    print(f"  repo: {config.repo_path}")
    print(f"  verification: {config.verification.provider}/{config.verification.model}")
    print(f"  correspondence_agents: {len(config.verification.correspondence_agents)}")
    for a in config.verification.correspondence_agents:
        print(f"    - {a.label} ({a.provider}/{a.model})")

    # Load real tablet
    tablet = load_tablet(tablet_path(config))
    print(f"\nTablet: {tablet.closed_nodes}/{tablet.total_nodes} closed")

    node = tablet.nodes.get(NODE)
    if not node:
        print(f"ERROR: Node {NODE} not found")
        return 1
    print(f"Target: {NODE} (status={node.status}, kind={node.kind})")

    # Run _run_nl_verification on this one node — the real code path
    log_dir = config.state_dir / "logs" / "test-multi-corr"
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n--- Running _run_nl_verification([{NODE}]) ---")
    start = time.time()

    results = _run_nl_verification(
        config, tablet, [NODE],
        log_dir=log_dir,
        human_input="",
    )

    elapsed = time.time() - start
    print(f"\n--- Results ({elapsed:.1f}s) ---")
    print(json.dumps(results, indent=2, default=str))

    # Summarize
    for r in results:
        check = r.get("check", "?")
        overall = r.get("overall", "?")
        agent_results = r.get("agent_results")
        if agent_results:
            print(f"\n{check}: {overall}")
            for ar in agent_results:
                print(f"  [{ar.get('agent','?')}] -> {ar.get('overall','?')}: {ar.get('summary','')[:120]}")
        else:
            print(f"\n{check}: {overall} — {r.get('summary','')[:120]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
