"""Benchmark different models on proving tablet nodes.

Usage:
    python3 -u scripts/model_benchmark.py                    # all models, all nodes
    python3 -u scripts/model_benchmark.py --model claude-opus-max --node expected_isolated_limit
"""

import argparse
import grp
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lagent_tablets.adapters import ProviderConfig
from lagent_tablets.agents.agentapi_backend import run, stop_server

REPO = Path("/home/leanagent/math/connectivity_gnp_tablets")
RESULTS_DIR = Path("/tmp/model_tests/results")

ALL_MODELS = [
    ("gemini", None, None, "gemini-auto"),
    ("gemini", "gemini-3.1-pro-preview", None, "gemini-pro"),
    ("claude", "claude-opus-4-6", "max", "claude-opus-max"),
    ("claude", "claude-sonnet-4-6", None, "claude-sonnet"),
]

ALL_NODES = ["expected_isolated_limit", "prob_no_isolated_limit"]


def setup_permissions(node_name: str):
    """Set file permissions so lagentworker can write to the node."""
    tablet_dir = REPO / "Tablet"
    try:
        gid = grp.getgrnam("leanagent").gr_gid
    except KeyError:
        return

    # Tablet directory: setgid, group-writable
    os.chown(str(tablet_dir), -1, gid)
    os.chmod(str(tablet_dir), 0o2775)

    # Active node: group-writable
    for ext in (".lean", ".tex"):
        path = tablet_dir / f"{node_name}{ext}"
        if path.exists():
            try:
                os.chown(str(path), -1, gid)
                os.chmod(str(path), 0o664)
            except PermissionError:
                subprocess.run(["sudo", "-n", "-u", "lagentworker", "chmod", "664", str(path)],
                              capture_output=True, timeout=5)

    # worker_handoff.json
    handoff = REPO / "worker_handoff.json"
    handoff.unlink(missing_ok=True)

    # Repo root: group-writable for file creation
    try:
        os.chmod(str(REPO), 0o2775)
    except PermissionError:
        pass


def restore_sorry(node_name: str):
    """Restore a node to its sorry state."""
    sorry_path = Path(f"/tmp/model_tests/{node_name}_sorry.lean")
    dest = REPO / "Tablet" / f"{node_name}.lean"
    try:
        if dest.exists():
            dest.unlink()
    except PermissionError:
        subprocess.run(["sudo", "-n", "-u", "lagentworker", "rm", "-f", str(dest)],
                      capture_output=True, timeout=5)
    shutil.copy2(sorry_path, dest)
    os.chmod(str(dest), 0o664)


def restore_proven(node_name: str):
    """Restore a node to its proven state."""
    src = Path(f"/tmp/model_tests/proven/{node_name}.lean")
    dest = REPO / "Tablet" / f"{node_name}.lean"
    try:
        if dest.exists():
            dest.unlink()
    except PermissionError:
        subprocess.run(["sudo", "-n", "-u", "lagentworker", "rm", "-f", str(dest)],
                      capture_output=True, timeout=5)
    shutil.copy2(src, dest)
    os.chmod(str(dest), 0o664)


def run_test(provider, model, effort, label, node_name, port=3290):
    """Run a single test. Returns result dict."""
    print(f"\n{'='*60}")
    print(f"  {label} on {node_name}")
    print(f"{'='*60}")

    # Restore sorry state and fix permissions
    restore_sorry(node_name)
    setup_permissions(node_name)

    # Config — don't add extra_args that _agent_command already handles
    config = ProviderConfig(
        provider=provider,
        model=model,
        effort=effort,
        extra_args=[],
    )

    # Build prompt
    tex_path = REPO / "Tablet" / f"{node_name}.tex"
    lean_path = REPO / "Tablet" / f"{node_name}.lean"
    tex_content = tex_path.read_text() if tex_path.exists() else ""
    lean_content = lean_path.read_text() if lean_path.exists() else ""
    skill_path = REPO / ".agent-supervisor" / "skills" / "LEAN_WORKER.md"

    prompt = f"""You are a Lean 4 formalization worker.

Read the skill file at `{skill_path}` for Loogle usage and proof strategies.

YOUR ACTIVE NODE: `{node_name}`
YOUR SINGLE GOAL: Eliminate the `sorry` in `Tablet/{node_name}.lean`.

--- {node_name}.lean ---
{lean_content}

--- {node_name}.tex ---
{tex_content}

You have read access to all files in `Tablet/`.
Edit only the proof body (everything after `:=`). Do NOT modify the declaration line.
Do NOT use `import Mathlib` — only specific submodule imports.
Run `lake build Tablet.{node_name}` to check compilation.
When done, write `worker_handoff.json` with status DONE or STUCK.
"""

    handoff = REPO / "worker_handoff.json"
    handoff.unlink(missing_ok=True)

    start = time.monotonic()
    result = run(
        config, prompt,
        role="worker",
        work_dir=REPO,
        burst_user="lagentworker",
        timeout=900,
        port=port,
        fresh=True,
    )

    # Agent may still be writing files after run() returns.
    # Wait for the handoff file or file stabilization.
    print("  Waiting for agent to finish writing...")
    for _ in range(60):  # up to 10 min extra
        time.sleep(10)
        if handoff.exists():
            print("  Handoff file written.")
            break
        try:
            import urllib.request
            req = urllib.request.Request(f"http://localhost:{port}/status")
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read().decode())
            if data.get("status") != "running":
                # Check one more time after a pause
                time.sleep(5)
                if handoff.exists():
                    break
                # Check if file changed from sorry state
                current = lean_path.read_text() if lean_path.exists() else ""
                if "sorry" not in current.replace("--", "") or len(current.split("\n")) > 50:
                    break
        except Exception:
            break  # Server down

    duration = time.monotonic() - start

    # Check result
    final_lean = lean_path.read_text() if lean_path.exists() else ""
    # Crude sorry check (ignore comments)
    import re
    masked = re.sub(r'--.*$', '', final_lean, flags=re.MULTILINE)
    has_sorry = bool(re.search(r'\bsorry\b', masked))
    lines = len(final_lean.split("\n"))

    # Check if it compiles
    compiles = False
    try:
        build = subprocess.run(
            ["lake", "build", f"Tablet.{node_name}"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )
        compiles = build.returncode == 0
    except Exception:
        pass

    # Restore proven version
    restore_proven(node_name)

    test_result = {
        "label": label,
        "node": node_name,
        "provider": provider,
        "model": model or "auto",
        "effort": effort,
        "ok": result.ok,
        "duration_seconds": round(duration, 1),
        "output_chars": len(result.captured_output),
        "has_sorry": has_sorry,
        "compiles": compiles,
        "proved": not has_sorry and compiles,
        "proof_lines": lines,
        "error": result.error[:200] if result.error else "",
    }

    status = "PROVED" if test_result["proved"] else ("SORRY" if has_sorry else "COMPILE_FAIL")
    if not result.ok:
        status = "BURST_FAIL"
    print(f"  Result: {status} in {duration:.0f}s ({lines} lines, compiles={compiles})")
    if result.error:
        print(f"  Error: {result.error[:100]}")

    return test_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Run only this model label")
    parser.add_argument("--node", help="Run only this node")
    args = parser.parse_args()

    models = ALL_MODELS
    nodes = ALL_NODES

    if args.model:
        models = [m for m in ALL_MODELS if m[3] == args.model]
        if not models:
            print(f"Unknown model: {args.model}. Available: {[m[3] for m in ALL_MODELS]}")
            return 1

    if args.node:
        nodes = [args.node]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_results = []

    for node_name in nodes:
        for provider, model, effort, label in models:
            try:
                result = run_test(provider, model, effort, label, node_name)
                all_results.append(result)
            except Exception as e:
                print(f"  EXCEPTION: {e}")
                import traceback
                traceback.print_exc()
                all_results.append({
                    "label": label, "node": node_name, "error": str(e),
                    "proved": False, "duration_seconds": 0,
                })

            # Save incrementally
            with open(RESULTS_DIR / "benchmark.json", "w") as f:
                json.dump(all_results, f, indent=2)

            # Clean up
            stop_server(3290)
            time.sleep(5)

    # Print summary
    print(f"\n{'='*70}")
    print("BENCHMARK RESULTS")
    print(f"{'='*70}")
    print(f"{'Model':<20} {'Node':<30} {'Result':<12} {'Time':>6} {'Lines':>6}")
    print("-" * 76)
    for r in all_results:
        status = "PROVED" if r.get("proved") else "FAIL"
        print(f"{r.get('label','?'):<20} {r.get('node','?'):<30} {status:<12} {r.get('duration_seconds',0):>5.0f}s {r.get('proof_lines',0):>5}")


if __name__ == "__main__":
    sys.exit(main() or 0)
