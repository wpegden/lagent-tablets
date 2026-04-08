"""Gemini account rotation for budget management.

Accounts are stored at ~/.gemini/accounts/{email}/.
Primary account is wes@math.cmu.edu. Falls back to others when any model's
budget drops below 10% remaining.

Usage:
    from lagent_tablets.gemini_accounts import ensure_budget
    ensure_budget()  # checks current account, rotates if needed
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PRIMARY_ACCOUNT = "wes@math.cmu.edu"
LOW_BUDGET_THRESHOLD = 10  # percent remaining


def _gemini_home(burst_user: str = "lagentworker") -> Path:
    return Path(f"/home/{burst_user}/.gemini")


def available_accounts(burst_user: str = "lagentworker") -> List[str]:
    """List all stored Gemini accounts."""
    accounts_dir = _gemini_home(burst_user) / "accounts"
    if not accounts_dir.exists():
        return []
    return sorted(d.name for d in accounts_dir.iterdir() if d.is_dir())


def active_account(burst_user: str = "lagentworker") -> Optional[str]:
    """Return the currently active Gemini account email."""
    ga_path = _gemini_home(burst_user) / "google_accounts.json"
    try:
        result = subprocess.run(
            ["sudo", "-n", "-u", burst_user, "cat", str(ga_path)],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("active", "")
    except Exception:
        pass
    return None


def switch_account(email: str, burst_user: str = "lagentworker") -> bool:
    """Switch to a different Gemini account by copying stored credentials."""
    accounts_dir = _gemini_home(burst_user) / "accounts" / email
    gemini_home = _gemini_home(burst_user)

    for filename in ("oauth_creds.json", "google_accounts.json"):
        src = accounts_dir / filename
        dst = gemini_home / filename
        try:
            result = subprocess.run(
                ["sudo", "-n", "-u", burst_user, "cp", str(src), str(dst)],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                return False
        except Exception:
            return False

    print(f"  Switched Gemini account to {email}")
    return True


def check_budget_low(port: int = 3290) -> Tuple[bool, Dict[str, dict]]:
    """Check if any Gemini model's budget is below threshold.

    Returns (is_low, stats_dict).
    """
    from lagent_tablets.gemini_usage import check_gemini_usage

    try:
        stats = check_gemini_usage(port=port)
        if not stats:
            return False, {}

        for model, data in stats.items():
            if data.get("remaining_pct", 100) < LOW_BUDGET_THRESHOLD:
                return True, stats

        return False, stats
    except Exception:
        return False, {}


def ensure_budget(burst_user: str = "lagentworker", port: int = 3290) -> str:
    """Ensure the active Gemini account has sufficient budget.

    Policy:
    - Use wes@math.cmu.edu as primary
    - If any model drops below 10% remaining, try switching to another account
    - If all accounts are low, stay on current and warn

    Returns the active account email.
    """
    current = active_account(burst_user)
    is_low, stats = check_budget_low(port=port)

    if not is_low:
        # Budget is fine. If we're not on primary, switch back.
        if current != PRIMARY_ACCOUNT:
            accounts = available_accounts(burst_user)
            if PRIMARY_ACCOUNT in accounts:
                # Check if primary has budget before switching back
                switch_account(PRIMARY_ACCOUNT, burst_user)
                is_low_primary, _ = check_budget_low(port=port)
                if is_low_primary:
                    # Primary is still low, switch back
                    switch_account(current, burst_user)
                    return current
                return PRIMARY_ACCOUNT
        return current or PRIMARY_ACCOUNT

    # Budget is low on current account
    low_models = [m for m, d in stats.items() if d.get("remaining_pct", 100) < LOW_BUDGET_THRESHOLD]
    print(f"  Gemini budget low on {current}: {low_models}")

    # Try other accounts
    accounts = available_accounts(burst_user)
    for email in accounts:
        if email == current:
            continue
        switch_account(email, burst_user)
        is_low_alt, alt_stats = check_budget_low(port=port)
        if not is_low_alt:
            print(f"  Rotated to {email} (budget OK)")
            return email
        else:
            low_alt = [m for m, d in alt_stats.items() if d.get("remaining_pct", 100) < LOW_BUDGET_THRESHOLD]
            print(f"  {email} also low: {low_alt}")

    # All accounts low — stay on current and warn
    print(f"  WARNING: All Gemini accounts are low on budget")
    switch_account(current or PRIMARY_ACCOUNT, burst_user)
    return current or PRIMARY_ACCOUNT
