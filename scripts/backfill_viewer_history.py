#!/usr/bin/env python3
"""Backfill legacy historical viewer snapshots for pre-cutover cycles.

This writes one JSON file per tagged cycle into an external cache directory.
The viewer server uses that cache only when a historical tag does not already
contain `.agent-supervisor/viewer_state.json`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lagent_tablets.viewer_state import backfill_cache_dir, write_legacy_backfill_viewer_state


def list_cycles(repo: Path) -> list[int]:
    raw = subprocess.check_output(
        ["git", "-C", str(repo), "tag", "-l", "cycle-*", "--sort=version:refname"],
        text=True,
        timeout=10,
    )
    cycles: list[int] = []
    for tag in raw.splitlines():
        tag = tag.strip()
        if not tag:
            continue
        try:
            cycles.append(int(tag.replace("cycle-", "")))
        except ValueError:
            continue
    return cycles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="Formalization repo path")
    parser.add_argument(
        "--static-out",
        type=Path,
        default=Path("/home/leanagent/lagent-tablets-web"),
        help="Static viewer output root",
    )
    parser.add_argument("--force", action="store_true", help="Rebuild existing cached files")
    args = parser.parse_args()

    repo = args.repo.resolve()
    cache = backfill_cache_dir(args.static_out.resolve(), repo)
    cache.mkdir(parents=True, exist_ok=True)

    for cycle in list_cycles(repo):
        out_path = cache / f"{cycle}.json"
        if out_path.exists() and not args.force:
            print(f"skip cycle {cycle}: {out_path}")
            continue
        write_legacy_backfill_viewer_state(repo, cycle, static_out=args.static_out.resolve())
        print(f"wrote cycle {cycle}: {out_path}")

    print(f"cache: {cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
