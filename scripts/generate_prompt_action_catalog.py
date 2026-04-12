#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lagent_tablets.prompt_action_catalog import write_prompt_action_catalog


def main() -> None:
    output_dir = ROOT / "prompt_action_catalog"
    write_prompt_action_catalog(output_dir)


if __name__ == "__main__":
    main()
