#!/usr/bin/env python3
"""Benchmark: 3 agents review NL/Lean statement correspondence."""

import json
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))
from lagent_tablets.prompts import _read_file

# The NL and Lean statements to check
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

# This is the exact prompt the supervisor's verification model would use
VERIFICATION_PROMPT = f"""You are a mathematical verification agent. Your job is to check whether a natural language
mathematical statement expresses the same mathematical content as a Lean 4 formal statement.

Think carefully and systematically about whether these express the same mathematical content.
Check quantifier scope, type constraints, implicit assumptions, and edge cases.

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
1. Does the NL statement make claims about mathematical OBJECTS (graphs, random variables, etc.) that the Lean does not mention?
2. Does the Lean statement prove only an analytical fact while the NL claims something about a specific mathematical domain?
3. Are there implicit assumptions, domain context, or interpretations in the NL that are absent from the Lean?

Be STRICT. If the NL says "the expected number of isolated vertices" but the Lean just proves a limit about a real sequence, that is a FAIL -- the probabilistic/graph-theoretic interpretation is missing from the Lean.

Return a JSON object:
{{
  "decision": "PASS" or "FAIL",
  "correspondence_issues": [
    {{"category": "missing_in_lean | missing_in_nl | different_claim | implicit_assumption", "description": "..."}}
  ],
  "summary": "Brief overall assessment"
}}
"""


def run_verification(name: str, provider: str, model: str, effort: str = "") -> dict:
    """Run the verification prompt via CLI."""
    print(f"[{name}] Starting...", flush=True)
    t0 = time.time()

    if provider == "claude":
        cmd = ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json",
               "--model", model]
        if effort:
            cmd.extend(["--effort", effort])
    elif provider == "gemini":
        cmd = ["gemini", "--approval-mode=yolo", "-p"]
        if model:
            cmd.extend(["--model", model])
    elif provider == "codex":
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox", "--ephemeral"]
        if effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])

    try:
        proc = subprocess.run(
            ["sudo", "-n", "-u", "lagentworker", "env",
             "PATH=/home/leanagent/.local/bin:/home/leanagent/.elan/bin:/home/leanagent/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin",
             "HOME=/home/lagentworker",
             "ELAN_HOME=/home/leanagent/.elan",
             *cmd],
            input=VERIFICATION_PROMPT,
            capture_output=True, text=True,
            timeout=600,
        )
        elapsed = time.time() - t0
        output = proc.stdout + "\n" + proc.stderr
        print(f"[{name}] Done in {elapsed:.0f}s, exit={proc.returncode}", flush=True)

        # Try to extract the JSON decision
        decision = None
        # Claude wraps in JSON
        try:
            wrapper = json.loads(proc.stdout.strip())
            if "result" in wrapper:
                inner = wrapper["result"]
                # Try to parse inner as JSON
                try:
                    decision = json.loads(inner)
                except json.JSONDecodeError:
                    # Look for JSON in markdown code block
                    import re
                    m = re.search(r'```(?:json)?\s*\n?(.*?)```', inner, re.DOTALL)
                    if m:
                        decision = json.loads(m.group(1).strip())
            elif "decision" in wrapper:
                decision = wrapper
            cost = wrapper.get("total_cost_usd")
        except json.JSONDecodeError:
            cost = None
            # Try to find JSON in raw output
            import re
            for candidate in re.finditer(r'\{[^{}]*"decision"[^{}]*\}', output, re.DOTALL):
                try:
                    decision = json.loads(candidate.group())
                    break
                except json.JSONDecodeError:
                    pass

        return {
            "name": name,
            "provider": provider,
            "model": model,
            "effort": effort,
            "elapsed_seconds": round(elapsed, 1),
            "exit_code": proc.returncode,
            "decision": decision,
            "cost_usd": cost,
            "output_chars": len(output),
            "raw_output_preview": output[:500],
        }
    except subprocess.TimeoutExpired:
        return {"name": name, "error": "Timed out after 600s", "elapsed_seconds": 600}
    except Exception as e:
        return {"name": name, "error": str(e)}


TESTS = {
    "claude_opus_max":    ("claude", "opus", "max"),
    "gemini_top":         ("gemini", "gemini-2.5-pro", ""),
    "codex_xhigh":        ("codex", "", "high"),
}

if __name__ == "__main__":
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(run_verification, name, *args): name for name, args in TESTS.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
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
