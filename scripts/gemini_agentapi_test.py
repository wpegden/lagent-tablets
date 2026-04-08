"""Run 4 Gemini tests via agentapi: Lean proof + NL verification, each with pro and auto models."""

import json
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lagent_tablets.adapters import ProviderConfig
from lagent_tablets.agents.agentapi_backend import run, stop_server
from lagent_tablets.burst import extract_json_decision, _clean_terminal_json

WORK_DIR = Path("/home/leanagent/src/lagent-tablets")

# The Lean proof problem: threshold_limit node
LEAN_PROMPT = """You are a Lean 4 mathematician. Prove the following theorem by replacing the `sorry`.

```lean
import Mathlib

theorem threshold_limit (c : ℝ) :
    Filter.Tendsto (fun n : ℕ => (n : ℝ) * (1 - (Real.log n + c) / n) ^ (n - 1))
      Filter.atTop (nhds (Real.exp (-c))) := by
  sorry
```

This is the classic asymptotic result: n * (1 - (log n + c)/n)^(n-1) → e^{-c} as n → ∞.

The key idea: (1 - x/n)^n → e^{-x}, so (1 - (log n + c)/n)^(n-1) ≈ e^{-(log n + c)} = e^{-c}/n,
and multiplying by n gives e^{-c}.

Write ONLY the proof term or tactic proof. Do NOT create or modify any files.
Reply with the Lean 4 proof inside a ```lean code block."""

# The NL verification question
NL_PROMPT = """You are a mathematical verification expert. Determine whether the following
Lean 4 statement correctly captures the natural-language claim.

## Natural Language Statement

For p = (log n + c)/n where c is a fixed real constant, the expected number
of isolated vertices satisfies n(1 - (log n + c)/n)^(n-1) → e^{-c} as n → ∞.

## Lean 4 Statement

```lean
theorem threshold_limit (c : ℝ) :
    Filter.Tendsto (fun n : ℕ => (n : ℝ) * (1 - (Real.log n + c) / n) ^ (n - 1))
      Filter.atTop (nhds (Real.exp (-c)))
```

## Task

Does the Lean statement correctly formalize the NL claim? Consider:
1. Does it capture the limit correctly?
2. Is the parameterization by c correct?
3. Does the Lean type (ℕ → ℝ with Filter.Tendsto/atTop/nhds) match the NL "as n → ∞"?
4. Is anything missing from the NL claim that the Lean statement doesn't capture?

Do NOT create or modify any files.

Respond with a JSON object:
{"decision": "PASS" or "FAIL", "reason": "explanation", "issues": ["list any issues or empty"]}"""


def run_test(name: str, model: str | None, prompt: str, port: int, timeout: float = 300) -> dict:
    """Run a single test and return results."""
    print(f"[{name}] Starting (model={'auto' if model is None else model}, port={port})...")

    config = ProviderConfig(
        provider="gemini",
        model=model,
        effort=None,
        extra_args=[],
    )

    result = run(
        config, prompt,
        role="worker",
        work_dir=WORK_DIR,
        port=port,
        timeout=timeout,
    )

    # Clean up
    stop_server(port)

    # Parse output
    output = result.captured_output
    cleaned = _clean_terminal_json(output)

    # Try JSON extraction
    decision = extract_json_decision(cleaned) if cleaned else None

    return {
        "name": name,
        "model": model or "auto",
        "ok": result.ok,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
        "output_length": len(output),
        "output": output[:2000],
        "decision": decision,
    }


def main():
    tests = [
        ("lean_pro",  "gemini-3.1-pro-preview", LEAN_PROMPT, 3291, 600),
        ("lean_auto", None,                       LEAN_PROMPT, 3292, 600),
        ("nl_pro",    "gemini-3.1-pro-preview", NL_PROMPT,   3293, 300),
        ("nl_auto",   None,                       NL_PROMPT,   3294, 300),
    ]

    results = {}

    # Run all 4 tests in parallel
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(run_test, name, model, prompt, port, timeout): name
            for name, model, prompt, port, timeout in tests
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                results[name] = result
                status = "OK" if result["ok"] else "FAIL"
                print(f"[{name}] {status} in {result['duration_seconds']:.1f}s ({result['output_length']} chars)")
            except Exception as e:
                results[name] = {"name": name, "error": str(e), "ok": False}
                print(f"[{name}] ERROR: {e}")

    # Print summary
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    for name in ["lean_pro", "lean_auto", "nl_pro", "nl_auto"]:
        r = results.get(name, {})
        status = "OK" if r.get("ok") else "FAIL"
        duration = r.get("duration_seconds", 0)
        model = r.get("model", "?")
        error = r.get("error", "")

        print(f"\n--- {name} (model={model}) ---")
        print(f"  Status: {status}  Duration: {duration:.1f}s")
        if error:
            print(f"  Error: {error}")
        if r.get("decision"):
            print(f"  Decision: {json.dumps(r['decision'], indent=2)}")

        # Show truncated output
        output = r.get("output", "")
        if output:
            # Strip the ✦ marker and show key content
            lines = [l.rstrip() for l in output.split("\n") if l.strip()]
            # Filter out UI chrome
            content_lines = [l for l in lines if not any(
                l.strip().startswith(p) for p in ["▀▀▀", "▄▄▄", "────", "YOLO", "Shift+Tab", "workspace", "~/"]
            ) and "for shortcuts" not in l]
            print(f"  Output preview:")
            for line in content_lines[:15]:
                print(f"    {line.strip()}")
            if len(content_lines) > 15:
                print(f"    ... ({len(content_lines) - 15} more lines)")

    # Save full results
    out_path = WORK_DIR / "test_results_gemini.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
