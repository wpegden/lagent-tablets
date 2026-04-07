"""Prompt assembly for worker, reviewer, and verification model.

All prompts are built from templates on disk (hot-reloadable) plus dynamic context.
Templates use Python .format() substitution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.config import Config, Policy
from lagent_tablets.state import SupervisorState, TabletNode, TabletState
from lagent_tablets.tablet import (
    PREAMBLE_NAME,
    extract_tablet_imports,
    node_lean_path,
    node_tex_path,
)


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_template(name: str) -> str:
    """Load a prompt template from the prompts/ directory."""
    path = PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _read_file(path: Path, default: str = "") -> str:
    """Read a file, returning default if missing."""
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return default


def _trim(text: str, max_chars: int = 50000) -> str:
    """Trim text to max_chars, keeping head and tail."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n\n[... trimmed {len(text) - max_chars} chars ...]\n\n" + text[-half:]


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _tablet_status_text(tablet: TabletState, repo_path: Path) -> str:
    """Build a compact tablet status summary for prompts."""
    lines = []
    m = tablet.metrics()
    lines.append(f"Tablet: {m['closed_nodes']}/{m['total_nodes']} nodes closed")
    lines.append("")
    lines.append("| Name | Kind | Status | Title | Imports |")
    lines.append("|------|------|--------|-------|---------|")

    for name in sorted(tablet.nodes.keys()):
        node = tablet.nodes[name]
        if name == PREAMBLE_NAME:
            continue
        lean_path = node_lean_path(repo_path, name)
        imports_str = ""
        if lean_path.exists():
            imports = extract_tablet_imports(lean_path.read_text(encoding="utf-8"))
            imports_str = ", ".join(imports) if imports else "-"
        status_marker = "CLOSED" if node.status == "closed" else "open"
        lines.append(f"| {name} | {node.kind} | {status_marker} | {node.title} | {imports_str} |")

    return "\n".join(lines)


def _active_node_context(
    node_name: str,
    tablet: TabletState,
    repo_path: Path,
) -> str:
    """Build detailed context for the active node."""
    node = tablet.nodes.get(node_name)
    if not node:
        return f"Active node '{node_name}' not found in tablet."

    lines = [f"=== Active Node: {node_name} ==="]
    lines.append(f"Kind: {node.kind}")
    lines.append(f"Status: {node.status}")
    if node.title:
        lines.append(f"Title: {node.title}")
    if node.paper_provenance:
        lines.append(f"Paper reference: {node.paper_provenance}")
    lines.append("")

    # NL statement and proof from .tex
    tex_path = node_tex_path(repo_path, node_name)
    if tex_path.exists():
        lines.append("--- NL content (.tex) ---")
        lines.append(_read_file(tex_path))
        lines.append("")

    # Current Lean file
    lean_path = node_lean_path(repo_path, node_name)
    if lean_path.exists():
        lines.append("--- Current Lean file (.lean) ---")
        lines.append(_read_file(lean_path))
        lines.append("")

    # Imported node declarations (what's available to the proof)
    if lean_path.exists():
        imports = extract_tablet_imports(lean_path.read_text(encoding="utf-8"))
        if imports:
            lines.append("--- Available from imports ---")
            for imp_name in imports:
                imp_node = tablet.nodes.get(imp_name)
                imp_lean = node_lean_path(repo_path, imp_name)
                if imp_node and imp_lean.exists():
                    content = imp_lean.read_text(encoding="utf-8")
                    # Extract just the declaration line
                    from lagent_tablets.tablet import declaration_line
                    decl = declaration_line(content)
                    status = "CLOSED" if imp_node.status == "closed" else "open"
                    lines.append(f"  {imp_name} ({status}): {decl or '(declaration not found)'}")
            lines.append("")

    return "\n".join(lines)


def _previous_cycle_feedback(
    state: SupervisorState,
    previous_outcome: Optional[Dict[str, Any]],
) -> str:
    """Build feedback from the previous cycle.

    The outcome dict has:
      outcome: PROGRESS | NO_PROGRESS | INVALID | REJECTED
      detail: human-readable explanation
      build_output: (for INVALID) the lake env lean error
      rejection: (for REJECTED) the verification model's feedback
    """
    lines = []

    # Reviewer's guidance
    if state.last_review:
        next_prompt = state.last_review.get("next_prompt", "")
        if next_prompt:
            lines.append("REVIEWER GUIDANCE:")
            lines.append(next_prompt)
            lines.append("")
        reason = state.last_review.get("reason", "")
        if reason:
            lines.append(f"Reviewer's assessment: {reason}")
            lines.append("")

    if not previous_outcome:
        return "\n".join(lines) if lines else "First cycle on this node."

    outcome = previous_outcome.get("outcome", "")
    detail = previous_outcome.get("detail", "")

    if outcome == "PROGRESS":
        lines.append(f"PREVIOUS CYCLE: PROGRESS -- {detail}")

    elif outcome == "NO_PROGRESS":
        lines.append(f"PREVIOUS CYCLE: NO PROGRESS -- {detail}")
        lines.append("The supervisor detected no meaningful changes. The node still has sorry and no new files were created.")

    elif outcome == "INVALID":
        lines.append(f"PREVIOUS CYCLE: INVALID -- {detail}")
        build_output = previous_outcome.get("build_output", "")
        if build_output:
            lines.append("Build error output:")
            lines.append("```")
            lines.append(_trim(build_output, 10000))
            lines.append("```")
        lines.append("")
        lines.append("Fix the issue and try again. Run check_node.sh before handing off.")

    elif outcome == "REJECTED":
        lines.append(f"PREVIOUS CYCLE: REJECTED by verification model -- {detail}")
        rejection = previous_outcome.get("rejection", {})
        if isinstance(rejection, dict):
            summary = rejection.get("summary", "")
            if summary:
                lines.append(f"Verification summary: {summary}")
            for phase in ("correspondence", "paper_faithfulness", "soundness"):
                phase_result = rejection.get(phase, {})
                if isinstance(phase_result, dict) and phase_result.get("decision") == "FAIL":
                    issues = phase_result.get("issues", [])
                    for issue in issues:
                        lines.append(f"  [{phase}] {issue.get('node', '?')}: {issue.get('description', '')}")
        lines.append("")
        lines.append("Address the verification feedback and try again.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worker prompt
# ---------------------------------------------------------------------------

def build_worker_prompt(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
    *,
    previous_outcome: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the complete worker prompt."""
    node_name = state.active_node or tablet.active_node
    repo_path = config.repo_path

    sections = []

    # 1. Role and goal
    goal_text = _read_file(config.goal_file, "(No goal file found)")
    sections.append("You are a Lean 4 formalization worker.\n")
    sections.append(f"GOAL:\n{goal_text}\n")

    # 2. Feedback from previous cycle
    feedback = _previous_cycle_feedback(state, previous_outcome)
    if feedback.strip():
        sections.append(feedback)

    # 3. Active node context
    sections.append(_active_node_context(node_name, tablet, repo_path))

    # 4. Tablet status
    sections.append(_tablet_status_text(tablet, repo_path))

    # 5. Plan and tasks
    plan_text = _read_file(config.repo_path / "PLAN.md")
    if plan_text.strip():
        sections.append(f"--- PLAN.md ---\n{_trim(plan_text, 5000)}\n")

    tasks_text = _read_file(config.repo_path / "TASKS.md")
    if tasks_text.strip():
        sections.append(f"--- TASKS.md ---\n{_trim(tasks_text, 5000)}\n")

    # 6. Instructions
    check_node = config.state_dir / "scripts" / "check_node.sh"
    check_tablet = config.state_dir / "scripts" / "check_tablet.sh"

    sections.append(f"""--- INSTRUCTIONS ---

YOUR ACTIVE NODE: `{node_name}`
YOUR SINGLE GOAL: Eliminate the `sorry` in `Tablet/{node_name}.lean`.

IMPORTANT WORKFLOW:
1. Work ONLY on `Tablet/{node_name}.lean`. Do NOT edit any other node's .lean file.
2. When you have a result -- whether the proof compiles, you need helpers, or you're stuck -- STOP IMMEDIATELY and write `worker_handoff.json`.
3. Do NOT move on to other nodes. The reviewer decides what to work on next.

You may:
- Edit the proof body (everything after `:=`) in `Tablet/{node_name}.lean`
- Add or remove `import Tablet.*` or `import Mathlib.*` lines in `Tablet/{node_name}.lean`
- Add `import Mathlib.*` lines to `Tablet/Preamble.lean` (additions only, no removals)
- Create new helper nodes: write both `Tablet/{{name}}.lean` and `Tablet/{{name}}.tex` files
- Update `Tablet/{node_name}.tex` to reflect new helpers in your NL proof
- Update the STRATEGY comment block with your approach, blockers, and failed attempts

You must NOT:
- Edit any other existing node's `.lean` file (they are read-only)
- Modify the declaration line (`theorem {node_name} ...` -- this is frozen)
- Add `axiom`, `constant`, `unsafe`, `native_decide`, or other forbidden keywords
- Import anything other than `Tablet.*` or `Mathlib.*`
- Delegate to sub-agents or use web search (work directly, do not delegate)

MANDATORY BEFORE SUBMITTING: Run the self-check and fix any errors:
  {check_node} {node_name}
You MUST iterate until check_node.sh reports "Compiles: OK" before writing worker_handoff.json.
If check_node.sh reports compilation errors, fix them and run it again.

WHEN DONE -- write `worker_handoff.json` IMMEDIATELY with:
{{
  "summary": "brief description of what you did",
  "status": "NOT_STUCK | STUCK | DONE | NEED_INPUT",
  "new_nodes": ["list", "of", "new", "node", "names"]
}}
Do NOT continue working after writing this file. Stop and let the supervisor take over.
""")

    # 7. Policy notes
    if policy.prompt_notes.worker:
        sections.append(f"--- ADDITIONAL NOTES ---\n{policy.prompt_notes.worker}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Reviewer prompt
# ---------------------------------------------------------------------------

def build_reviewer_prompt(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
    *,
    worker_handoff: Optional[Dict[str, Any]] = None,
    worker_output: str = "",
    validation_summary: Optional[Dict[str, Any]] = None,
    nl_verification: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the complete reviewer prompt."""
    sections = []

    goal_text = _read_file(config.goal_file, "(No goal file found)")
    sections.append("You are the reviewer supervising a Lean formalization project.\n")
    sections.append(f"GOAL:\n{goal_text}\n")

    # Tablet status
    sections.append(_tablet_status_text(tablet, config.repo_path))
    sections.append("")

    # Worker handoff
    if worker_handoff:
        sections.append("--- WORKER HANDOFF ---")
        sections.append(json.dumps(worker_handoff, indent=2))
        sections.append("")

    # Worker terminal output (trimmed)
    if worker_output:
        sections.append("--- WORKER OUTPUT (trimmed) ---")
        sections.append(_trim(worker_output, 20000))
        sections.append("")

    # Validation / cycle outcome
    if validation_summary:
        cycle_outcome = validation_summary.get("outcome", "")
        cycle_detail = validation_summary.get("detail", "")
        consecutive_invalids = validation_summary.get("consecutive_invalids", 0)

        sections.append(f"--- CYCLE OUTCOME: {cycle_outcome} ---")
        if cycle_detail:
            sections.append(f"Detail: {cycle_detail}")
        if consecutive_invalids and consecutive_invalids > 0:
            sections.append(f"NOTE: The worker has hit {consecutive_invalids} consecutive INVALID results.")
            sections.append("The worker may need different guidance to get past this issue.")
            sections.append("Consider: suggesting a different approach, switching to a different node,")
            sections.append("or providing specific hints about what's going wrong.")
        sections.append("")

    # NL verification results
    if nl_verification:
        sections.append("--- NL VERIFICATION RESULTS ---")
        if isinstance(nl_verification, list):
            # Multiple verification agents were consulted
            sections.append(f"{len(nl_verification)} verification agent(s) were consulted:")
            for i, result in enumerate(nl_verification, 1):
                sections.append(f"\n  Agent {i}: {result.get('overall', '?')}")
                summary = result.get("summary", "")
                if summary:
                    sections.append(f"  Summary: {summary}")
                for phase in ("correspondence", "paper_faithfulness", "soundness"):
                    phase_result = result.get(phase, {})
                    if isinstance(phase_result, dict):
                        decision = phase_result.get("decision", "?")
                        issues = phase_result.get("issues", [])
                        if issues:
                            sections.append(f"  {phase}: {decision}")
                            for issue in issues:
                                sections.append(f"    - {issue.get('node', '?')}: {issue.get('description', '')}")
            sections.append("\nReview these results and decide whether to accept or reject the changes.")
        else:
            sections.append(json.dumps(nl_verification, indent=2))
        sections.append("")

    # Orphan nodes
    from lagent_tablets.tablet import find_orphan_nodes
    orphans = find_orphan_nodes(tablet, config.repo_path)
    if orphans:
        sections.append(f"WARNING: Orphan nodes (not imported by anything): {orphans}")
        sections.append("")

    # Recent review history
    if state.review_log:
        recent = state.review_log[-5:]
        sections.append("--- RECENT REVIEWS ---")
        for entry in recent:
            sections.append(f"  Cycle {entry.get('cycle', '?')}: {entry.get('decision', '?')} -- {entry.get('reason', '')[:100]}")
        sections.append("")

    # Instructions
    sections.append(f"""--- YOUR DECISION ---

Decide what to do next. Return a JSON decision:
{{
  "decision": "CONTINUE | ADVANCE_PHASE | STUCK | NEED_INPUT | DONE",
  "confidence": 0.0,
  "reason": "brief explanation",
  "next_prompt": "specific guidance for the worker's next cycle",
  "next_active_node": "name of the node the worker should focus on next",
  "suggest_branch": false
}}

Guidelines:
- CONTINUE: the worker is making progress. Pick the most impactful node to work on next.
- ADVANCE_PHASE: all proof_formalization work is done (every node closed). Move to cleanup.
- STUCK: the worker has tried multiple approaches and is not making progress. This triggers stuck recovery.
- NEED_INPUT: a human needs to provide mathematical guidance.
- DONE: the entire project is complete.

For next_active_node: pick the node that is most uncertain, most blocking, or most impactful.
Focus on nodes whose dependencies are already closed (they can be proved now).

If NL verification results are shown above, review them carefully. Verification agents may
disagree. You are the final arbiter:
- If verification agents approve unanimously: accept the changes.
- If verification agents reject: you may override if you believe the rejection is wrong, but explain why.
- If agents disagree: weigh their reasoning and make a judgment call.
""")

    if policy.prompt_notes.reviewer:
        sections.append(f"--- ADDITIONAL NOTES ---\n{policy.prompt_notes.reviewer}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Verification model prompt
# ---------------------------------------------------------------------------

def build_verification_prompt(
    config: Config,
    tablet: TabletState,
    *,
    new_nodes: List[str],
    modified_nodes: List[str],
    paper_tex: str = "",
    max_context_tokens: int = 50000,
) -> str:
    """Build the NL verification model prompt.

    Combined prompt for correspondence, paper-faithfulness, and soundness checks.
    """
    sections = []

    sections.append("""You are a mathematical verification agent. Your job is to check the natural language
mathematics of a proof tablet -- a DAG of theorem nodes, each with an NL statement and NL proof.

You must check three things for the nodes listed below:

A) NL/LEAN CORRESPONDENCE: Does the Lean statement fully capture ALL mathematical claims made
   by the NL statement? The Lean must formalize EVERY claim in the NL -- if the NL mentions
   graphs, probability, or any mathematical structure that the Lean omits, that is a FAIL.
   A Lean statement that proves only PART of the NL claim is NOT a valid correspondence.
   Check: quantifier scope, type constraints, implicit assumptions, domain-specific context.

B) PAPER-FAITHFULNESS: Is each new node a genuine, non-trivial intermediate step toward proving
   the paper's main results? Does it represent real mathematical progress, or does it merely
   repackage the difficulty without reducing it?

C) NL PROOF SOUNDNESS: Does each NL proof rigorously establish the stated result from the NL
   statements of its imported nodes? Check for gaps, circular reasoning, unstated assumptions,
   and placeholder language ("trivial", "obvious", "left to the reader", etc.).

Think carefully and systematically. Do not accept vague or hand-wavy arguments.
""")

    # Nodes to check
    all_check = set(new_nodes) | set(modified_nodes)
    if all_check:
        sections.append("=== NODES TO CHECK ===\n")
        for name in sorted(all_check):
            node = tablet.nodes.get(name)
            if not node:
                continue
            tex_path = node_tex_path(config.repo_path, name)
            lean_path = node_lean_path(config.repo_path, name)
            tex_content = _read_file(tex_path, "(no .tex file)")
            lean_content = _read_file(lean_path, "(no .lean file)")

            sections.append(f"--- Node: {name} (kind: {node.kind}) ---")
            sections.append(f"NL content (.tex):\n{tex_content}")
            sections.append(f"Lean file:\n{lean_content}")
            sections.append("")

    # Imported nodes' NL statements (context for soundness check)
    imported_names: set = set()
    for name in all_check:
        lean_path = node_lean_path(config.repo_path, name)
        if lean_path.exists():
            imports = extract_tablet_imports(lean_path.read_text(encoding="utf-8"))
            imported_names.update(imports)
    imported_names -= all_check  # don't repeat nodes we're already checking
    imported_names.discard(PREAMBLE_NAME)

    if imported_names:
        sections.append("=== IMPORTED NODES (NL statements for reference) ===\n")
        for name in sorted(imported_names):
            tex_path = node_tex_path(config.repo_path, name)
            if tex_path.exists():
                sections.append(f"--- {name} ---")
                sections.append(_read_file(tex_path))
                sections.append("")

    # Paper (if available and budget allows)
    if paper_tex:
        sections.append("=== SOURCE PAPER ===\n")
        sections.append(_trim(paper_tex, max_context_tokens // 3))
        sections.append("")

    # Output format
    sections.append("""=== YOUR RESPONSE ===

Return a JSON object:
{
  "correspondence": {
    "decision": "PASS" or "FAIL",
    "issues": [{"node": "name", "description": "..."}]
  },
  "paper_faithfulness": {
    "decision": "PASS" or "FAIL",
    "issues": [{"node": "name", "description": "..."}]
  },
  "soundness": {
    "decision": "PASS" or "FAIL",
    "issues": [{"node": "name", "description": "..."}]
  },
  "overall": "APPROVE" or "REJECT",
  "summary": "brief overall assessment"
}
""")

    return "\n".join(sections)
