#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lagent_tablets.prompt_action_catalog import write_prompt_action_catalog
from lagent_tablets.prompt_catalog import generate_prompt_catalog
from lagent_tablets.prompt_reference_web import publish_prompt_reference_web


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate and publish the prompt catalogs to the static web root.")
    parser.add_argument(
        "--static-root",
        type=Path,
        default=Path("/home/leanagent/lagent-tablets-web"),
        help="Static web root that backs /lagent-tablets/.",
    )
    parser.add_argument(
        "--route-name",
        default="prompt-reference",
        help="Route name under /lagent-tablets/.",
    )
    parser.add_argument(
        "--project-alias",
        action="append",
        default=[],
        help="Optional project slug to receive a symlink alias to the published reference.",
    )
    args = parser.parse_args()

    prompt_catalog_dir = ROOT / "prompt_catalog"
    prompt_action_catalog_dir = ROOT / "prompt_action_catalog"
    generate_prompt_catalog(prompt_catalog_dir)
    write_prompt_action_catalog(prompt_action_catalog_dir)
    site_root = publish_prompt_reference_web(
        static_root=args.static_root,
        prompt_catalog_dir=prompt_catalog_dir,
        prompt_action_catalog_dir=prompt_action_catalog_dir,
        route_name=args.route_name,
        alias_projects=args.project_alias,
    )

    print(f"Published prompt reference to: {site_root}")
    print(f"Browse at: /lagent-tablets/{args.route_name}/")
    for slug in args.project_alias:
        print(f"Alias: /lagent-tablets/{slug}/{args.route_name}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
