#!/usr/bin/env python3
"""Update hot-reloadable verification policy fields.

Example:
  ./scripts/set_verification_policy.py configs/extremal_vectors_run.policy.json \
    --soundness gemini codex \
    --correspondence claude gemini codex \
    --soundness-disagree-bias reject
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("policy_path", type=Path)
    parser.add_argument("--soundness", nargs="*", default=None, metavar="SELECTOR")
    parser.add_argument("--correspondence", nargs="*", default=None, metavar="SELECTOR")
    parser.add_argument(
        "--soundness-disagree-bias",
        choices=("reject", "approve"),
        default=None,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = args.policy_path.resolve()
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit(f"Policy file must contain a JSON object: {path}")

    verification = data.setdefault("verification", {})
    if not isinstance(verification, dict):
        raise SystemExit(f"Policy field 'verification' must be an object: {path}")

    if args.soundness is not None:
        verification["soundness_agent_selectors"] = list(args.soundness)
    if args.correspondence is not None:
        verification["correspondence_agent_selectors"] = list(args.correspondence)
    if args.soundness_disagree_bias is not None:
        verification["soundness_disagree_bias"] = args.soundness_disagree_bias

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(path)
    print(json.dumps(verification, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
