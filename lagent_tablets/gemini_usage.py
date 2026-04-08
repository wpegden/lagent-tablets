"""Query Gemini and Claude CLI usage stats.

Starts temporary agentapi sessions, sends /stats (Gemini) or /status→Usage (Claude),
parses the screen output to extract usage percentages and reset times.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Dict, Optional


def check_gemini_usage(port: int = 3290, timeout: float = 30) -> Dict[str, dict]:
    """Query Gemini usage stats. Returns dict of model -> {used_pct, remaining_pct, resets_in}.

    Starts a temporary agentapi server, sends /stats, reads the screen.
    """
    # Start temporary server (as lagentworker for auth)
    burst_user = "lagentworker"
    worker_path = "/home/leanagent/.local/bin:/home/leanagent/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"
    proc = subprocess.Popen(
        ["agentapi", "server", "-p", str(port), "-t", "gemini", "--",
         "sudo", "-n", "-u", burst_user, "env", f"PATH={worker_path}",
         f"HOME=/home/{burst_user}", "gemini", "--approval-mode=yolo"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )

    try:
        # Wait for server
        import urllib.request
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(f"http://localhost:{port}/status")
                resp = urllib.request.urlopen(req, timeout=3)
                if resp.status == 200:
                    break
            except Exception:
                pass
            time.sleep(1)

        # Send /stats
        data = json.dumps({"content": "/stats", "type": "raw"}).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/message",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        time.sleep(0.5)

        data = json.dumps({"content": "\r", "type": "raw"}).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/message",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5)

        # Wait for stats to render
        time.sleep(8)

        # Read screen
        result = subprocess.run(
            ["curl", "-s", "-N", f"http://localhost:{port}/internal/screen", "--max-time", "3"],
            capture_output=True, text=True, timeout=10,
        )
        screen = ""
        for line in result.stdout.splitlines():
            if line.startswith("data:"):
                screen_data = json.loads(line[5:])
                screen = screen_data.get("screen", "")
                break

        return _parse_stats(screen)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Clean up port
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5)
        except Exception:
            pass


def _parse_stats(screen: str) -> Dict[str, dict]:
    """Parse the /stats screen output into model usage data."""
    models = {}

    for line in screen.split("\n"):
        # Look for lines like: gemini-3-flash-preview     -    ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬   78%  9:07 PM (11h 55m)
        m = re.search(r'(gemini-[\w.-]+)\s+.*?(\d+)%\s+(.*?\(.*?\))', line)
        if m:
            model = m.group(1)
            used_pct = int(m.group(2))
            resets_in = m.group(3).strip()
            models[model] = {
                "used_pct": used_pct,
                "remaining_pct": 100 - used_pct,
                "resets_in": resets_in,
            }

    return models


def get_remaining_pct(model: str = "gemini-3-flash-preview", port: int = 3290) -> Optional[int]:
    """Quick check: what percentage of budget remains for a model?"""
    try:
        stats = check_gemini_usage(port=port)
        if model in stats:
            return stats[model]["remaining_pct"]
        # Try partial match
        for k, v in stats.items():
            if model in k or k in model:
                return v["remaining_pct"]
    except Exception:
        pass
    return None


def check_claude_usage(port: int = 3291, timeout: float = 30) -> Dict[str, dict]:
    """Query Claude Code usage via /status → Usage tab.

    Returns dict of budget_name -> {used_pct, remaining_pct, resets, detail}.
    """
    burst_user = "lagentworker"
    worker_path = "/home/leanagent/.local/bin:/home/leanagent/.elan/bin:/home/leanagent/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"

    proc = subprocess.Popen(
        ["agentapi", "server", "-p", str(port), "-t", "claude", "--",
         "sudo", "-n", "-u", burst_user, "env", f"PATH={worker_path}",
         f"HOME=/home/{burst_user}", "claude", "--dangerously-skip-permissions"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )

    try:
        import urllib.request
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(f"http://localhost:{port}/status")
                resp = urllib.request.urlopen(req, timeout=3)
                if resp.status == 200:
                    break
            except Exception:
                pass
            time.sleep(1)

        # Send /status
        for content in ["/status", "\r"]:
            data = json.dumps({"content": content, "type": "raw"}).encode()
            req = urllib.request.Request(
                f"http://localhost:{port}/message",
                data=data, headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass
            time.sleep(0.5)

        time.sleep(3)

        # Right-arrow twice to Usage tab
        for _ in range(2):
            data = json.dumps({"content": "\x1b[C", "type": "raw"}).encode()
            req = urllib.request.Request(
                f"http://localhost:{port}/message",
                data=data, headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass
            time.sleep(1)

        time.sleep(2)

        # Read screen
        result = subprocess.run(
            ["curl", "-s", "-N", f"http://localhost:{port}/internal/screen", "--max-time", "3"],
            capture_output=True, text=True, timeout=10,
        )
        screen = ""
        for line in result.stdout.splitlines():
            if line.startswith("data:"):
                screen_data = json.loads(line[5:])
                screen = screen_data.get("screen", "")
                break

        # Dismiss with Esc
        data = json.dumps({"content": "\x1b", "type": "raw"}).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/message",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

        return _parse_claude_stats(screen)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5)
        except Exception:
            pass


def _parse_claude_stats(screen: str) -> Dict[str, dict]:
    """Parse the Claude /status Usage tab screen."""
    budgets = {}

    lines = screen.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Look for budget labels
        for label in ["Current session", "Current week (all models)", "Current week (Sonnet only)", "Extra usage"]:
            if label in line:
                # Next line should have the bar and percentage
                if i + 1 < len(lines):
                    bar_line = lines[i + 1].strip()
                    pct_match = re.search(r'(\d+)%\s+used', bar_line)
                    if pct_match:
                        used = int(pct_match.group(1))
                        # Line after that should have reset info
                        resets = ""
                        detail = ""
                        if i + 2 < len(lines):
                            reset_line = lines[i + 2].strip()
                            if "Resets" in reset_line or "$" in reset_line:
                                resets = reset_line
                            # Check for dollar amounts
                            dollar_match = re.search(r'\$(\d+\.?\d*)\s*/\s*\$(\d+\.?\d*)', bar_line + " " + reset_line)
                            if dollar_match:
                                detail = f"${dollar_match.group(1)} / ${dollar_match.group(2)} spent"

                        budgets[label] = {
                            "used_pct": used,
                            "remaining_pct": 100 - used,
                            "resets": resets,
                            "detail": detail,
                        }
                break
        i += 1

    return budgets


def check_all_usage(gemini_port: int = 3290, claude_port: int = 3291) -> Dict[str, Dict[str, dict]]:
    """Check usage for both Gemini and Claude. Returns {"gemini": {...}, "claude": {...}}."""
    result = {}
    try:
        result["gemini"] = check_gemini_usage(port=gemini_port)
    except Exception as e:
        result["gemini"] = {"error": str(e)}
    try:
        result["claude"] = check_claude_usage(port=claude_port)
    except Exception as e:
        result["claude"] = {"error": str(e)}
    return result


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    if target in ("gemini", "all"):
        stats = check_gemini_usage()
        if stats:
            print("Gemini Usage:")
            for model, data in sorted(stats.items()):
                print(f"  {model}: {data['used_pct']}% used ({data['remaining_pct']}% remaining) resets {data['resets_in']}")
        else:
            print("Gemini: could not retrieve stats")

    if target in ("claude", "all"):
        stats = check_claude_usage()
        if stats:
            print("\nClaude Usage:")
            for budget, data in stats.items():
                print(f"  {budget}: {data['used_pct']}% used ({data['remaining_pct']}% remaining) {data.get('detail', '')} {data.get('resets', '')}")
        else:
            print("Claude: could not retrieve stats")
