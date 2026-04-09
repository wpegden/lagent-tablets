#!/usr/bin/env python3
"""Benchmark: 6 agents prove threshold_limit in parallel."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lagent_tablets.burst import run_worker_burst
from lagent_tablets.adapters import ProviderConfig
from lagent_tablets.check import check_node

CONFIGS = {
    "claude_opus":   ProviderConfig(provider="claude", model="opus", effort="max"),
    "claude_sonnet": ProviderConfig(provider="claude", model="sonnet", effort="high"),
    "gemini_auto":   ProviderConfig(provider="gemini"),
    "gemini_top":    ProviderConfig(provider="gemini", model="gemini-2.5-pro"),
    "codex_xhigh":   ProviderConfig(provider="codex", extra_args=["-c", "model_reasoning_effort=\"high\""]),
    "codex_high":    ProviderConfig(provider="codex", extra_args=["-c", "model_reasoning_effort=\"medium\""]),
}

PROMPT_TEMPLATE = """You are a Lean 4 formalization worker.

YOUR SINGLE GOAL: Prove the theorem in Tablet/threshold_limit.lean by eliminating the `sorry`.

The theorem states:
  theorem threshold_limit (c : ℝ) :
      Filter.Tendsto (fun n : ℕ => (n : ℝ) * (1 - (Real.log n + c) / n) ^ (n - 1))
          Filter.atTop (nhds (Real.exp (-c)))

This is a standard analysis result: n*(1-(log n+c)/n)^(n-1) → e^{-c} as n → ∞.

Strategy hint: Take logs, use the bounds -u²/(1-u) ≤ log(1-u)+u ≤ 0 where u=(log n+c)/n → 0,
then squeeze and apply continuity of exp.

You may:
- Edit the proof body in Tablet/threshold_limit.lean (after :=)
- Add imports (Tablet.* or Mathlib.*) to Tablet/threshold_limit.lean
- Add imports to Tablet/Preamble.lean
- Do NOT change the theorem statement

You MUST verify your proof compiles before finishing:
  lake env lean Tablet/threshold_limit.lean

When done, write worker_handoff.json with:
{{"summary": "description of proof", "status": "DONE"}}

Do not delegate to sub-agents. Work directly.
"""


def run_one(name: str) -> dict:
    config = CONFIGS[name]
    repo = Path(f"/tmp/lagent-bench-{name}")
    log_dir = repo / ".agent-supervisor" / "logs" / "bench"

    print(f"[{name}] Starting...", flush=True)
    t0 = time.time()

    result = run_worker_burst(
        config, PROMPT_TEMPLATE,
        session_name=f"bench-{name}",
        work_dir=repo,
        burst_user="lagentworker",
        timeout_seconds=3600,
        startup_timeout_seconds=60,
        log_dir=log_dir,
    )

    elapsed = time.time() - t0
    print(f"[{name}] Burst done in {elapsed:.0f}s, ok={result.ok}, exit={result.exit_code}", flush=True)

    # Check the result
    lean_path = repo / "Tablet" / "threshold_limit.lean"
    lean_content = lean_path.read_text() if lean_path.exists() else ""
    lines = len(lean_content.splitlines())
    has_sorry = "sorry" in lean_content.lower().split("--")[0]  # crude check

    # Run check.py
    check = check_node(
        repo, "threshold_limit",
        allowed_prefixes=["Mathlib"],
        forbidden_keywords=[
            "sorry", "axiom", "constant", "unsafe", "opaque", "partial",
            "native_decide", "implementedBy", "implemented_by", "extern",
            "elab", "macro", "syntax", "run_cmd", "#eval",
        ],
        approved_axioms_path=repo / "APPROVED_AXIOMS.json",
    )

    # Parse usage from output
    usage = {}
    if config.provider == "claude":
        try:
            wrapper = json.loads(result.captured_output.strip())
            usage = {"cost_usd": wrapper.get("total_cost_usd"), "model": wrapper.get("modelUsage", {}).keys()}
        except (json.JSONDecodeError, AttributeError):
            pass
    elif config.provider == "codex":
        for line in result.captured_output.strip().splitlines():
            try:
                rec = json.loads(line)
                if rec.get("type") == "turn.completed" and "usage" in rec:
                    u = rec["usage"]
                    usage = {k: v for k, v in u.items()}
            except json.JSONDecodeError:
                pass

    return {
        "name": name,
        "provider": config.provider,
        "model": config.model or "default",
        "effort": config.effort or (config.extra_args[1].split("=")[1].strip('"') if config.extra_args else "default"),
        "elapsed_seconds": round(elapsed, 1),
        "exit_code": result.exit_code,
        "burst_ok": result.ok,
        "lean_lines": lines,
        "sorry_free": check.get("sorry_free", False),
        "compiles": check.get("compiles", False),
        "closed": check.get("ok", False),
        "errors": check.get("errors", [])[:2],
        "usage": usage,
        "output_chars": len(result.captured_output),
    }


if __name__ == "__main__":
    import concurrent.futures

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(run_one, name): name for name in CONFIGS}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                results[name] = {"name": name, "error": str(e)}
                print(f"[{name}] ERROR: {e}", flush=True)

    # Print results table
    print("\n" + "=" * 100)
    print(f"{'Name':<20} {'Time':>6} {'Lines':>6} {'Sorry-free':>10} {'Compiles':>9} {'Closed':>7} {'Cost':>8}")
    print("-" * 100)
    for name in CONFIGS:
        r = results.get(name, {})
        t = f"{r.get('elapsed_seconds', '?')}s"
        lines = str(r.get('lean_lines', '?'))
        sf = "YES" if r.get('sorry_free') else "no"
        comp = "YES" if r.get('compiles') else "no"
        closed = "YES" if r.get('closed') else "no"
        cost = ""
        if r.get("usage", {}).get("cost_usd"):
            cost = f"${r['usage']['cost_usd']:.2f}"
        elif r.get("usage", {}).get("input_tokens"):
            cost = f"{r['usage'].get('input_tokens', 0)}in"
        print(f"{name:<20} {t:>6} {lines:>6} {sf:>10} {comp:>9} {closed:>7} {cost:>8}")

    print("=" * 100)

    # Save full results
    Path("/tmp/lagent-bench-results.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"\nFull results saved to /tmp/lagent-bench-results.json")
