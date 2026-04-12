#!/usr/bin/env python3
"""Generate the checked-in prompt catalog."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lagent_tablets.prompt_catalog import main


if __name__ == "__main__":
    raise SystemExit(main())
