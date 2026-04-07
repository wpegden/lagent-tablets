#!/usr/bin/env python3
"""Benchmark: 3 agents review NL/Lean statement correspondence.

Uses the SAME run_burst code as the supervisor -- no manual subprocess calls.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lagent_tablets.burst import run_burst, extract_json_decision
from lagent_tablets.adapters import ProviderConfig

NL_STATEMENT = r"""
\begin{theorem}[Connectivity threshold limit]
For $p = (\log n + c)/n$ where $c$ is a fixed real constant, the expected number
of isolated vertices satisfies
$$n\left(1 - \frac{\log n + c}{n}\right)^{n-1} \to e^{-c}$$
as $n \to \infty$.
\end{theorem}
""".strip()

LEAN_STATEMENT = """
theorem threshold_limit (c : ℝ) :
    Filter.Tendsto (fun n : ℕ => (n : ℝ) * (1 - (Real.log n + c) / n) ^ (n - 1))
      Filter.atTop (nhds (Real.exp (-c)))
""".strip()

PROMPT = f"""You are a mathematical verification agent. Your job is to check whether a natural language
mathematical statement expresses the same mathematical content as a Lean 4 formal statement.

Think carefully and systematically.

=== NATURAL LANGUAGE STATEMENT ===
{NL_STATEMENT}

=== LEAN 4 STATEMENT ===
{LEAN_STATEMENT}

=== YOUR TASK ===
Does the Lean statement fully capture ALL mathematical claims made by the NL statement?

CRITICAL: The Lean must formalize EVERY claim in the NL. If the NL mentions graphs, probability,
expected values, or any mathematical structure that the Lean omits, that is a FAIL.
A Lean statement that proves only PART of the NL claim is NOT a valid correspondence.

Specifically check:
1. Does the NL make claims about mathematical OBJECTS (graphs, random variables, etc.) that the Lean omits?
2. Does the Lean prove only an analytical fact while the NL claims something about a specific domain?
3. Are there implicit assumptions or interpretations in the NL absent from the Lean?

Be STRICT. Return JSON:
{{"decision": "PASS" or "FAIL", "correspondence_issues": [{{"category": "...", "description": "..."}}], "summary": "..."}}
"""

TESTS = {
    "claude_opus_max": ProviderConfig(provider="claude", model="opus", effort="max"),
    "gemini_top":      ProviderConfig(provider="gemini", model="gemini-2.5-pro"),
    "codex_xhigh":     ProviderConfig(provider="codex", extra_args=["-c", 'model_reasoning_effort="high"']),
}


def run_one(name: str) -> dict:
    config = TESTS[name]
    work_dir = Path(f"/tmp/lagent-nlbench-{name}")
    work_dir.mkdir(parents=True, exist_ok=True)
    log_dir = work_dir / "logs"

    print(f"[{name}] Starting...", flush=True)
    t0 = time.time()

    result = run_burst(
        config, PROMPT,
        role="reviewer",
        session_name=f"nlbench-{name}",
        work_dir=work_dir,
        burst_user="lagentworker",
        startup_timeout_seconds=30,
        burst_timeout_seconds=300,
        log_dir=log_dir,
    )
    elapsed = time.time() - t0
    print(f"[{name}] Done in {elapsed:.0f}s, ok={result.ok}, exit={result.exit_code}", flush=True)

    # Parse decision from output
    decision = extract_json_decision(result.captured_output)

    # Try to get cost from Claude JSON wrapper
    cost = None
    try:
        wrapper = json.loads(result.captured_output.strip())
        cost = wrapper.get("total_cost_usd")
    except (json.JSONDecodeError, AttributeError):
        pass

    return {
        "name": name,
        "elapsed_seconds": round(elapsed, 1),
        "exit_code": result.exit_code,
        "ok": result.ok,
        "decision": decision,
        "cost_usd": cost,
        "output_preview": result.captured_output[:300],
        "error": result.error,
    }


if __name__ == "__main__":
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(run_one, name): name for name in TESTS}
        for f in as_completed(futures):
            name = futures[f]
            try:
                results[name] = f.result()
            except Exception as e:
                results[name] = {"name": name, "error": str(e)}

    print("\n" + "=" * 100)
    print(f"{'Name':<25} {'Time':>6} {'Decision':>10} {'Cost':>8} {'Summary'}")
    print("-" * 100)
    for name in TESTS:
        r = results.get(name, {})
        t = f"{r.get('elapsed_seconds', '?')}s"
        d = r.get("decision", {})
        dec = d.get("decision", "?") if isinstance(d, dict) else "?"
        cost = f"${r['cost_usd']:.2f}" if r.get("cost_usd") else ""
        summary = d.get("summary", "")[:60] if isinstance(d, dict) else r.get("error", "")[:60]
        print(f"{name:<25} {t:>6} {dec:>10} {cost:>8} {summary}")
    print("=" * 100)

    Path("/tmp/lagent-bench-nl-results.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"\nFull results saved to /tmp/lagent-bench-nl-results.json")
