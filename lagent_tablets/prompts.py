"""Prompt assembly for worker, reviewer, and verification model.

All prompts are built from templates on disk (hot-reloadable) plus dynamic context.
Templates use Python .format() substitution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from lagent_tablets.artifacts import prompt_artifact_paths
from lagent_tablets.config import Config, Policy
from lagent_tablets.state import (
    normalize_paper_focus_ranges,
    SupervisorState,
    TabletNode,
    TabletState,
    normalize_open_rejections,
    normalize_orphan_resolutions,
)
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


def _human_input_section(state) -> str:
    """Return a human feedback section if there's active feedback."""
    if hasattr(state, 'human_input') and state.human_input and state.human_input.strip():
        at_cycle = getattr(state, 'human_input_at_cycle', 0)
        current = getattr(state, 'cycle', 0)
        age = current - at_cycle if at_cycle and current else 0
        age_str = f", {age} cycle{'s' if age != 1 else ''} ago" if age > 0 else ""
        return f"--- HUMAN FEEDBACK (received at cycle {at_cycle}{age_str}) ---\n{state.human_input}\n"
    return ""


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


def _check_script_path(config: Config) -> Path:
    return config.state_dir / "scripts" / "check.py"


def _artifact_prompt_values(config: Config, canonical_name: str) -> Dict[str, str]:
    paths = prompt_artifact_paths(config.state_dir, config.repo_path, canonical_name)
    return {
        "canonical_output_path": str(paths["canonical"]),
        "raw_output_path": str(paths["raw"]),
        "done_path": str(paths["done"]),
        "check_script": str(_check_script_path(config)),
    }


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _tablet_status_text(tablet: TabletState, repo_path: Path) -> str:
    """Build a compact tablet status summary for prompts."""
    lines = []
    m = tablet.metrics()
    lines.append(f"Tablet: {m['closed_nodes']}/{m['total_nodes']} nodes closed")
    lines.append("")
    lines.append("| Name | Kind | Status | Difficulty | Title | Imports |")
    lines.append("|------|------|--------|------------|-------|---------|")

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
        diff_marker = node.difficulty
        if node.easy_attempts > 0:
            diff_marker += f" ({node.easy_attempts} attempts)"
        lines.append(f"| {name} | {node.kind} | {status_marker} | {diff_marker} | {node.title} | {imports_str} |")

    return "\n".join(lines)


def _paper_reference_text(config: Config) -> str:
    """Tell the agent where to read the source paper from disk."""
    paper_path = config.workflow.paper_tex_path
    if not paper_path or not paper_path.exists():
        return ""
    lines = [
        "--- SOURCE PAPER ---",
        f"Read the source paper directly from `{paper_path}`.",
        "The prompt does not inline the full paper; use the file on disk as the authoritative source.",
        "",
    ]
    return "\n".join(lines)


def _paper_focus_excerpt_text(
    config: Config,
    paper_focus_ranges: Any,
    *,
    max_chars: int = 20000,
) -> str:
    """Render reviewer-selected paper excerpts for the next worker prompt."""
    paper_path = config.workflow.paper_tex_path
    if not paper_path or not paper_path.exists():
        return ""

    ranges = normalize_paper_focus_ranges(paper_focus_ranges)
    if not ranges:
        return ""

    paper_lines = paper_path.read_text(encoding="utf-8", errors="replace").splitlines()
    intro = (
        "--- RELEVANT PAPER EXCERPTS ---\n"
        "The reviewer selected these source-paper ranges for focused context.\n"
        f"Treat `{paper_path}` as authoritative if anything here is truncated.\n\n"
    )
    parts = [intro]
    used = len(intro)

    for entry in ranges:
        start = max(1, min(entry["start_line"], len(paper_lines)))
        end = max(1, min(entry["end_line"], len(paper_lines)))
        if end < start:
            start, end = end, start

        reason = entry.get("reason", "")
        header = f"[Lines {start}-{end}]"
        if reason:
            header += f" {reason}"
        excerpt = "\n".join(paper_lines[start - 1:end]).strip()
        if not excerpt:
            continue
        block = f"{header}\n{excerpt}\n\n"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(block) <= remaining:
            parts.append(block)
            used += len(block)
            continue
        trimmed_body_budget = max(0, remaining - len(header) - 3)
        if trimmed_body_budget <= 0:
            break
        parts.append(f"{header}\n{_trim(excerpt, trimmed_body_budget)}\n\n")
        used = max_chars
        break

    if len(parts) == 1:
        return ""
    return "".join(parts)


def _tablet_file_reference_text(
    tablet: TabletState,
    repo_path: Path,
    *,
    header: str = "--- CURRENT TABLET FILES ---",
) -> str:
    """List current tablet files without inlining their contents."""
    lines = [
        header,
        "Read these files from disk as needed. The summary table above is only an index, not a complete substitute for the file contents.",
    ]

    support_files = [
        repo_path / "Tablet.lean",
        repo_path / "Tablet" / "INDEX.md",
        repo_path / "Tablet" / "README.md",
        repo_path / "Tablet" / "Preamble.lean",
    ]
    for path in support_files:
        if path.exists():
            lines.append(f"- {path.relative_to(repo_path)}")

    for name in sorted(tablet.nodes.keys()):
        if name == PREAMBLE_NAME:
            continue
        lean_path = node_lean_path(repo_path, name)
        tex_path = node_tex_path(repo_path, name)
        file_bits: List[str] = []
        if lean_path.exists():
            file_bits.append(str(lean_path.relative_to(repo_path)))
        if tex_path.exists():
            file_bits.append(str(tex_path.relative_to(repo_path)))
        if file_bits:
            lines.append(f"- {name}: {', '.join(file_bits)}")

    lines.append("")
    return "\n".join(lines)


def _open_rejections_text(
    open_rejections: Any,
    *,
    header: str = "--- CURRENT OPEN REJECTIONS ---",
    include_completion_note: bool = False,
) -> str:
    """Render the persisted theorem-stating rejection list for prompts."""
    rejections = normalize_open_rejections(open_rejections)
    if not rejections:
        return ""

    lines = [header]
    if include_completion_note:
        lines.append("Theorem-stating continues until this list is empty. Resolve these items before treating the tablet as complete.")
    for entry in rejections:
        lines.append(f"- [{entry['phase']}] {entry['node']}: {entry['reason']}")
    lines.append("")
    return "\n".join(lines)


def _orphan_resolutions_text(
    orphan_resolutions: Any,
    *,
    header: str = "--- ORPHAN NODE ACTIONS ---",
    include_completion_note: bool = False,
) -> str:
    """Render reviewer decisions about current orphan-node candidates."""
    resolutions = normalize_orphan_resolutions(orphan_resolutions)
    if not resolutions:
        return ""

    lines = [header]
    if include_completion_note:
        lines.append(
            "Resolve these orphan-node candidates before treating the tablet structure as complete."
        )
    for entry in resolutions:
        parents = entry.get("suggested_parents", [])
        if entry["action"] == "remove":
            lines.append(f"- [remove] {entry['node']}: {entry['reason']}")
        else:
            parent_text = f" Suggested parent nodes: {', '.join(parents)}." if parents else ""
            lines.append(
                f"- [keep_and_add_dependency] {entry['node']}: {entry['reason']}{parent_text}"
            )
    lines.append("")
    return "\n".join(lines)


def _active_node_context(
    node_name: str,
    tablet: TabletState,
    repo_path: Path,
) -> str:
    """Build context for the active node: its .lean file, and the .lean
    declarations of its children. The worker is told to read .tex files
    from disk as needed."""
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

    # Current Lean file (the worker needs this to know what to prove)
    lean_path = node_lean_path(repo_path, node_name)
    if lean_path.exists():
        lines.append(f"--- {node_name}.lean ---")
        lines.append(_read_file(lean_path))
        lines.append("")

    # Children's Lean declarations (what's available to import)
    if lean_path.exists():
        imports = extract_tablet_imports(lean_path.read_text(encoding="utf-8"))
        if imports:
            lines.append("--- Imported nodes ---")
            for imp_name in imports:
                imp_lean = node_lean_path(repo_path, imp_name)
                if imp_lean.exists():
                    lines.append(f"--- {imp_name}.lean ---")
                    lines.append(_read_file(imp_lean))
            lines.append("")

    lines.append(f"Read `Tablet/{node_name}.tex` and any other `.tex` files for NL context.")
    lines.append(f"You have read access to all files in `Tablet/`.")
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
    difficulty: str = "hard",
) -> str:
    """Build the complete worker prompt."""
    node_name = state.active_node or tablet.active_node
    repo_path = config.repo_path

    sections = []

    # 1. Basic model + role
    sections.append(_load_template("basic_model.md"))
    if difficulty == "easy":
        sections.append(f"YOUR ROLE: **Worker** (proof_formalization phase, EASY node). You are proving `{node_name}` using ONLY its existing children. No new imports, no new files.\n")
    else:
        sections.append("YOUR ROLE: **Worker** (proof_formalization phase). You are eliminating `sorry` from one node at a time. You do not decide which node to work on -- the reviewer assigns your node.\n")
    goal_text = _read_file(config.goal_file)
    if goal_text.strip():
        sections.append(f"GOAL:\n{goal_text}\n")

    # Human feedback (persistent across cycles)
    hi = _human_input_section(state)
    if hi:
        sections.append(hi)

    # 2. Feedback from previous cycle
    feedback = _previous_cycle_feedback(state, previous_outcome)
    if feedback.strip():
        sections.append(feedback)

    # 3. Active node context
    sections.append(_active_node_context(node_name, tablet, repo_path))

    # 4. Tablet status
    sections.append(_tablet_status_text(tablet, repo_path))

    # 5. Source paper
    paper_ref = _paper_reference_text(config)
    if paper_ref:
        sections.append(paper_ref)
    paper_focus = _paper_focus_excerpt_text(
        config,
        state.last_review.get("paper_focus_ranges", []) if state.last_review else [],
    )
    if paper_focus:
        sections.append(paper_focus)

    # 6. Plan and tasks
    plan_text = _read_file(config.repo_path / "PLAN.md")
    if plan_text.strip():
        sections.append(f"--- PLAN.md ---\n{_trim(plan_text, 5000)}\n")

    tasks_text = _read_file(config.repo_path / "TASKS.md")
    if tasks_text.strip():
        sections.append(f"--- TASKS.md ---\n{_trim(tasks_text, 5000)}\n")

    # 7. Instructions
    worker_handoff_artifacts = _artifact_prompt_values(config, "worker_handoff.json")

    # Skill file reference
    skill_path = repo_path / ".agent-supervisor" / "skills" / "LEAN_WORKER.md"
    if not skill_path.exists():
        skill_path = Path(__file__).resolve().parent.parent / "skills" / "LEAN_WORKER.md"

    template_name = "easy_worker_instructions.md" if difficulty == "easy" else "worker_instructions.md"
    instructions = _load_template(template_name).format(
        node_name=node_name,
        skill_path=skill_path,
        repo_path=repo_path,
        **worker_handoff_artifacts,
    )
    sections.append(instructions)

    # 7. Policy notes
    if policy.prompt_notes.worker:
        sections.append(f"--- ADDITIONAL NOTES ---\n{policy.prompt_notes.worker}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Theorem-stating worker prompt
# ---------------------------------------------------------------------------

def build_theorem_stating_prompt(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
    *,
    previous_outcome: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the worker prompt for the theorem_stating phase.

    The worker must:
    1. Read the paper and goal
    2. Create Tablet nodes (.lean + .tex pairs) for each main result and key intermediate step
    3. Set up Preamble.lean with ONLY the specific Mathlib imports needed (never bare `import Mathlib`)
    4. Write the Tablet.lean root import file
    5. Ensure `lake build Tablet` passes (sorry is allowed in this phase)
    """
    repo_path = config.repo_path
    sections = []

    # 1. Basic model + role
    sections.append(_load_template("basic_model.md"))
    sections.append("YOUR ROLE: **Worker** (theorem_stating phase). You are creating the tablet structure -- declaring nodes with Lean statements and rigorous NL proofs. You are NOT proving theorems in Lean yet; `sorry` is expected.\n")
    goal_text = _read_file(config.goal_file)
    if goal_text.strip():
        sections.append(f"GOAL:\n{goal_text}\n")

    hi = _human_input_section(state)
    if hi:
        sections.append(hi)

    # 2. Paper content
    paper_ref = _paper_reference_text(config)
    if paper_ref:
        sections.append(paper_ref)
    paper_focus = _paper_focus_excerpt_text(
        config,
        state.last_review.get("paper_focus_ranges", []) if state.last_review else [],
    )
    if paper_focus:
        sections.append(paper_focus)

    # 3. Feedback from previous cycle
    if state.last_review:
        next_prompt = state.last_review.get("next_prompt", "")
        if next_prompt:
            sections.append(f"REVIEWER GUIDANCE:\n{next_prompt}\n")
    open_rejections = state.open_rejections
    if not open_rejections and state.last_review:
        open_rejections = state.last_review.get("open_rejections", [])
    rejections_text = _open_rejections_text(
        open_rejections,
        include_completion_note=True,
    )
    if rejections_text:
        sections.append(rejections_text)
    orphan_resolutions = None
    if state.last_review:
        orphan_resolutions = state.last_review.get("orphan_resolutions", [])
    orphan_text = _orphan_resolutions_text(
        orphan_resolutions,
        include_completion_note=True,
    )
    if orphan_text:
        sections.append(orphan_text)

    # 4. Current tablet state (may be empty on first cycle)
    if tablet.nodes:
        sections.append(_tablet_status_text(tablet, repo_path))
        sections.append(_tablet_file_reference_text(tablet, repo_path))
    else:
        sections.append("The tablet is currently empty. You are creating it from scratch.\n")

    # 5. Preamble
    # Skill file reference (theorem_stating uses its own skill file)
    skill_path = repo_path / ".agent-supervisor" / "skills" / "LEAN_THEOREM_STATING.md"
    if not skill_path.exists():
        skill_path = Path(__file__).resolve().parent.parent / "skills" / "LEAN_THEOREM_STATING.md"

    # 6. Instructions
    instructions = _load_template("theorem_stating_instructions.md").format(
        skill_path=skill_path,
        repo_path=repo_path,
        **_artifact_prompt_values(config, "worker_handoff.json"),
    )
    sections.append(instructions)

    if policy.prompt_notes.worker:
        sections.append(f"--- ADDITIONAL NOTES ---\n{policy.prompt_notes.worker}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Theorem-stating reviewer prompt
# ---------------------------------------------------------------------------

def build_theorem_stating_reviewer_prompt(
    config: Config,
    state: SupervisorState,
    tablet: TabletState,
    policy: Policy,
    *,
    worker_handoff: Optional[Dict[str, Any]] = None,
    worker_output: str = "",
    nl_verification: Optional[List[Dict[str, Any]]] = None,
    orphan_candidates: Optional[List[str]] = None,
) -> str:
    """Build the reviewer prompt for the theorem_stating phase."""
    sections = []

    sections.append(_load_template("basic_model.md"))
    sections.append("YOUR ROLE: **Reviewer** (theorem_stating phase). You evaluate whether the worker's tablet structure is correct and complete. You decide whether to continue refining or advance to proof_formalization. You are the final arbiter on NL verification disputes.\n")
    goal_text = _read_file(config.goal_file)
    if goal_text.strip():
        sections.append(f"GOAL:\n{goal_text}\n")

    hi = _human_input_section(state)
    if hi:
        sections.append(hi)

    # Paper
    paper_ref = _paper_reference_text(config)
    if paper_ref:
        sections.append(paper_ref)

    # Current tablet
    if tablet.nodes:
        sections.append(_tablet_status_text(tablet, config.repo_path))
        sections.append(_tablet_file_reference_text(tablet, config.repo_path))

    # Worker handoff
    if worker_handoff:
        sections.append(f"--- WORKER HANDOFF ---\n{json.dumps(worker_handoff, indent=2)}\n")

    # Worker output
    if worker_output:
        sections.append(f"--- WORKER OUTPUT (trimmed) ---\n{_trim(worker_output, 15000)}\n")

    # NL verification results
    if nl_verification:
        sections.append("--- NL VERIFICATION RESULTS ---")
        for result in nl_verification:
            check_type = result.get("check", "verification")
            overall = result.get("overall", "?")
            summary = result.get("summary", "")
            sections.append(f"  {check_type}: {overall}")
            if summary:
                sections.append(f"    {summary}")
            for key in ("correspondence", "paper_faithfulness", "soundness"):
                sub = result.get(key, {})
                if isinstance(sub, dict) and sub.get("issues"):
                    for issue in sub["issues"]:
                        sections.append(f"    [{key}] {issue.get('node', '?')}: {issue.get('description', '')}")
        sections.append("")

    previous_rejections = _open_rejections_text(
        state.open_rejections,
        header="--- PREVIOUS OPEN REJECTIONS ---",
    )
    if previous_rejections:
        sections.append(previous_rejections)

    if orphan_candidates:
        sections.append("--- CURRENT ORPHAN CANDIDATES ---")
        sections.append(
            "These nodes are not paper_main_result nodes and are not currently imported by any other node."
        )
        sections.append(
            "For each one, decide whether the node should be removed or whether the worker missed a real downstream dependency/citation."
        )
        for name in orphan_candidates:
            sections.append(f"- {name}")
        sections.append("")

    # Skill file reference
    skill_path = config.repo_path / ".agent-supervisor" / "skills" / "LEAN_REVIEWER.md"
    if not skill_path.exists():
        skill_path = Path(__file__).resolve().parent.parent / "skills" / "LEAN_REVIEWER.md"

    # Recent reviews
    if state.review_log:
        recent = state.review_log[-5:]
        sections.append("--- RECENT REVIEWS ---")
        for entry in recent:
            sections.append(f"  Cycle {entry.get('cycle', '?')}: {entry.get('decision', '?')} -- {entry.get('reason', '')[:100]}")
        sections.append("")

    instructions = _load_template("theorem_stating_reviewer_instructions.md").format(
        skill_path=skill_path,
        phase="theorem_stating",
        **_artifact_prompt_values(config, "reviewer_decision.json"),
    )
    sections.append(instructions)

    if policy.prompt_notes.reviewer:
        sections.append(f"--- ADDITIONAL NOTES ---\n{policy.prompt_notes.reviewer}\n")

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

    sections.append(_load_template("basic_model.md"))
    sections.append("YOUR ROLE: **Reviewer** (proof_formalization phase). You evaluate the worker's proof attempts, choose which node to assign next, and provide specific mathematical guidance. You are the final arbiter on NL verification disputes.\n")
    goal_text = _read_file(config.goal_file)
    if goal_text.strip():
        sections.append(f"GOAL:\n{goal_text}\n")

    hi = _human_input_section(state)
    if hi:
        sections.append(hi)

    # Paper
    paper_ref = _paper_reference_text(config)
    if paper_ref:
        sections.append(paper_ref)

    # Tablet status
    sections.append(_tablet_status_text(tablet, config.repo_path))
    sections.append(f"\nYou have read access to all tablet files in `Tablet/`.\n")

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
            sections.append(f"{len(nl_verification)} verification check(s) were run:")
            for i, result in enumerate(nl_verification, 1):
                check_name = result.get("check", f"check-{i}")
                overall = result.get("overall", "?")
                summary = result.get("summary", "")

                # Multi-agent correspondence results
                agent_results = result.get("agent_results")
                if agent_results and overall == "DISAGREE":
                    sections.append(f"\n  {check_name}: **AGENTS DISAGREE** -- you must arbitrate")
                    if summary:
                        sections.append(f"  {summary}")
                    for ar in agent_results:
                        agent_label = ar.get("agent", "?")
                        agent_overall = ar.get("overall", "?")
                        agent_summary = ar.get("summary", "")
                        sections.append(f"\n    [{agent_label}] -> {agent_overall}")
                        if agent_summary:
                            sections.append(f"      Summary: {agent_summary}")
                        for phase in ("correspondence", "paper_faithfulness"):
                            phase_result = ar.get(phase, {})
                            if isinstance(phase_result, dict):
                                issues = phase_result.get("issues", [])
                                if issues:
                                    sections.append(f"      {phase}: {phase_result.get('decision', '?')}")
                                    for issue in issues:
                                        sections.append(f"        - {issue.get('node', '?')}: {issue.get('description', '')}")
                elif agent_results:
                    sections.append(f"\n  {check_name}: {overall} (unanimous from {len(agent_results)} agents)")
                    if summary:
                        sections.append(f"  {summary}")
                else:
                    sections.append(f"\n  {check_name}: {overall}")
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

    # Skill file reference
    skill_path = config.repo_path / ".agent-supervisor" / "skills" / "LEAN_REVIEWER.md"
    if not skill_path.exists():
        skill_path = Path(__file__).resolve().parent.parent / "skills" / "LEAN_REVIEWER.md"

    # Instructions
    instructions = _load_template("reviewer_instructions.md").format(
        skill_path=skill_path,
        phase="proof_formalization",
        **_artifact_prompt_values(config, "reviewer_decision.json"),
    )
    sections.append(instructions)

    if policy.prompt_notes.reviewer:
        sections.append(f"--- ADDITIONAL NOTES ---\n{policy.prompt_notes.reviewer}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Verification model prompt
# ---------------------------------------------------------------------------

def _node_check_list(
    config: Config,
    tablet: TabletState,
    node_names: List[str],
) -> List[str]:
    """Build the node listing sections shared by both verification prompts."""
    sections = []
    if node_names:
        sections.append("=== NODES TO CHECK ===\n")
        for name in sorted(node_names):
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

    # Imported nodes' NL statements (context)
    imported_names: set = set()
    for name in node_names:
        lean_path = node_lean_path(config.repo_path, name)
        if lean_path.exists():
            imports = extract_tablet_imports(lean_path.read_text(encoding="utf-8"))
            imported_names.update(imports)
    imported_names -= set(node_names)
    imported_names.discard(PREAMBLE_NAME)

    if imported_names:
        sections.append("=== IMPORTED NODES (NL statements for reference) ===\n")
        for name in sorted(imported_names):
            tex_path = node_tex_path(config.repo_path, name)
            if tex_path.exists():
                sections.append(f"--- {name} ---")
                sections.append(_read_file(tex_path))
                sections.append("")

    return sections


def build_correspondence_prompt(
    config: Config,
    tablet: TabletState,
    *,
    node_names: List[str],
    paper_tex: str = "",
    human_input: str = "",
    output_file: str = "correspondence_result.json",
    previous_results: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the Lean/NL correspondence verification prompt.

    Checks: does each node's Lean statement capture its NL statement?
    Is each node a faithful intermediate step toward the paper's results?

    The correspondence agent gets full tablet read access because verifying
    meaning requires following definition chains through transitive imports.
    """
    sections = []

    sections.append(_load_template("basic_model.md"))
    sections.append("YOUR ROLE: **Correspondence Verification Agent**. You check whether each node's Lean statement genuinely captures the same claim as its NL statement. You report your findings; the reviewer makes the final decision.\n")
    if human_input and human_input.strip():
        sections.append(f"--- HUMAN FEEDBACK ---\n{human_input}\n")
    sections.append(_load_template("correspondence_role.md"))

    # List nodes to check — agent reads files from disk
    if node_names:
        sections.append("=== NODES TO CHECK ===\n")
        sections.append("For each node below, read `Tablet/{name}.lean` and `Tablet/{name}.tex` to verify correspondence.\n")
        for name in sorted(node_names):
            node = tablet.nodes.get(name)
            if not node:
                continue
            # Show imports so agent knows the DAG structure
            lean_path = node_lean_path(config.repo_path, name)
            imports = []
            if lean_path.exists():
                imports = extract_tablet_imports(lean_path.read_text(encoding="utf-8"))
            imports_str = ", ".join(imports) if imports else "none"
            sections.append(f"- **{name}** (kind: {node.kind}, difficulty: {node.difficulty}) — imports: {imports_str}")
        sections.append("")

    sections.append("You have read access to all files in `Tablet/`. Read each node's `.lean` and `.tex` files, and follow import chains to verify definitions.\n")

    if config.workflow.paper_tex_path:
        sections.append(f"The source paper is at `{config.workflow.paper_tex_path}`. Read it as needed for context.\n")

    if previous_results:
        sections.append("=== PREVIOUS CYCLE'S VERIFICATION RESULTS ===\n")
        sections.append("The worker was asked to fix these issues. Check whether each fix is genuine or superficial. Do NOT approve a node just because the worker claims to have fixed it — verify independently.\n")
        for r in previous_results:
            agent = r.get("agent", r.get("check", "?"))
            for phase in ("correspondence", "paper_faithfulness"):
                issues = r.get(phase, {}).get("issues", []) if isinstance(r.get(phase), dict) else []
                for issue in issues:
                    sections.append(f"- **{issue.get('node', '?')}** ({phase}): {issue.get('description', '')[:300]}")
        sections.append("")

    response_fmt = _load_template("correspondence_response_format.md").format(
        **_artifact_prompt_values(config, output_file),
    )
    sections.append(response_fmt)
    return "\n".join(sections)


def build_nl_proof_prompt(
    config: Config,
    tablet: TabletState,
    *,
    node_names: List[str],
    paper_tex: str = "",
    human_input: str = "",
    output_file: str = "nl_proof_result.json",
) -> str:
    """Build the NL proof soundness verification prompt.

    Checks: does each node's NL proof rigorously follow from its
    children's NL statements? This is a purely mathematical check
    with no Lean involved.
    """
    sections = []

    sections.append(_load_template("basic_model.md"))
    sections.append("YOUR ROLE: **NL Proof Soundness Agent**. You check whether each node's natural-language proof rigorously establishes its result from its children's NL statements. This is a purely mathematical check -- no Lean code is involved. You report your findings; the reviewer makes the final decision.\n")
    if human_input and human_input.strip():
        sections.append(f"--- HUMAN FEEDBACK ---\n{human_input}\n")
    sections.append(_load_template("nl_proof_role.md"))

    # For NL proof checking, only show .tex content (no Lean needed)
    if node_names:
        sections.append("=== NODES TO CHECK ===\n")
        for name in sorted(node_names):
            node = tablet.nodes.get(name)
            if not node:
                continue
            tex_path = node_tex_path(config.repo_path, name)
            tex_content = _read_file(tex_path, "(no .tex file)")
            sections.append(f"--- Node: {name} (kind: {node.kind}) ---")
            sections.append(f"NL content (.tex):\n{tex_content}")
            sections.append("")

        # Show children's NL statements for reference
        imported_names: set = set()
        for name in node_names:
            lean_path = node_lean_path(config.repo_path, name)
            if lean_path.exists():
                imports = extract_tablet_imports(lean_path.read_text(encoding="utf-8"))
                imported_names.update(imports)
        imported_names -= set(node_names)
        imported_names.discard(PREAMBLE_NAME)

        if imported_names:
            sections.append("=== CHILD NODES (NL statements the proofs may cite) ===\n")
            for name in sorted(imported_names):
                tex_path = node_tex_path(config.repo_path, name)
                if tex_path.exists():
                    sections.append(f"--- {name} ---")
                    sections.append(_read_file(tex_path))
                    sections.append("")

    sections.append("If a proof references a node not shown above, you can find its NL content at `Tablet/{name}.tex`. All tablet `.tex` files are available for reading.\n")

    if paper_tex:
        sections.append("=== SOURCE PAPER (for reference) ===\n")
        sections.append(_trim(paper_tex, 15000))
        sections.append("")

    sections.append(
        _load_template("nl_proof_response_format.md").format(
            **_artifact_prompt_values(config, output_file),
        )
    )
    return "\n".join(sections)


def build_node_soundness_prompt(
    config: Config,
    tablet: TabletState,
    *,
    node_name: str,
    paper_tex: str = "",
    human_input: str = "",
    output_file: str = "nl_proof_result.json",
    previous_issues: Optional[List[str]] = None,
) -> str:
    """Build a per-node NL proof soundness prompt.

    Focused check: does this ONE node's NL proof rigorously establish
    its result from its children's NL statements?
    Can also flag STRUCTURAL issues (children don't provide what's needed).
    """
    sections = []
    sections.append(_load_template("basic_model.md"))
    sections.append(
        "YOUR ROLE: **NL Proof Soundness Agent**. You check whether one node's "
        "natural-language proof rigorously establishes its result from its children's "
        "NL statements. This is a purely mathematical check.\n"
    )
    if human_input and human_input.strip():
        sections.append(f"--- HUMAN FEEDBACK ---\n{human_input}\n")
    sections.append(_load_template("nl_proof_role.md"))

    node = tablet.nodes.get(node_name)
    if not node:
        sections.append(f"ERROR: Node {node_name} not found.\n")
        return "\n".join(sections)

    # The node being checked
    tex_content = _read_file(node_tex_path(config.repo_path, node_name), "(no .tex file)")
    sections.append(f"=== NODE TO CHECK: {node_name} (kind: {node.kind}) ===\n")
    sections.append(f"NL content (.tex):\n{tex_content}\n")

    # Its children (the NL statements it may cite)
    lean_path = node_lean_path(config.repo_path, node_name)
    children = []
    if lean_path.exists():
        children = extract_tablet_imports(lean_path.read_text(encoding="utf-8"))

    if children:
        sections.append("=== CHILDREN (NL statements this proof may cite) ===\n")
        for child in sorted(children):
            if child == PREAMBLE_NAME:
                continue
            child_tex = node_tex_path(config.repo_path, child)
            if child_tex.exists():
                sections.append(f"--- {child} ---")
                sections.append(_read_file(child_tex))
                sections.append("")
            else:
                sections.append(f"--- {child} --- (WARNING: .tex file missing)\n")
    else:
        sections.append("This node has NO children (leaf node). Its proof must be self-contained.\n")

    if paper_tex:
        sections.append("=== SOURCE PAPER (for reference) ===\n")
        sections.append(_trim(paper_tex, 15000))
        sections.append("")

    if previous_issues:
        sections.append("=== PREVIOUS CYCLE'S ISSUES FOR THIS NODE ===\n")
        sections.append("The worker was asked to fix these. Verify independently whether the fix is genuine:\n")
        for issue in previous_issues:
            sections.append(f"- {issue[:300]}")
        sections.append("")

    artifact_values = _artifact_prompt_values(config, output_file)
    sections.append(f"""=== YOUR RESPONSE ===

Evaluate this node's NL proof. Write your assessment as JSON to `{artifact_values["raw_output_path"]}`:

{{
  "node": "{node_name}",
  "soundness": {{
    "decision": "SOUND" or "UNSOUND" or "STRUCTURAL",
    "explanation": "detailed assessment"
  }},
  "overall": "APPROVE" or "REJECT",
  "summary": "brief assessment"
}}

Verdicts:
- **SOUND**: The NL proof rigorously establishes the result from the children's statements.
- **UNSOUND**: The proof has gaps or errors but the DAG structure is reasonable. The proof text needs fixing.
- **STRUCTURAL**: The children do NOT provide what is needed to prove this node. The DAG needs restructuring — new intermediate nodes or different dependencies are required.

MANDATORY:
1. Write the JSON to `{artifact_values["raw_output_path"]}`.
2. Run `python3 {artifact_values["check_script"]} soundness-result {artifact_values["raw_output_path"]} --node {node_name}`.
3. If that passes, write the completion marker `{artifact_values["done_path"]}` and stop.

The supervisor will rerun the same checker and then write the canonical result file `{artifact_values["canonical_output_path"]}`.
""")
    return "\n".join(sections)


def build_verification_prompt(
    config: Config,
    tablet: TabletState,
    *,
    new_nodes: List[str],
    modified_nodes: List[str],
    paper_tex: str = "",
    max_context_tokens: int = 50000,
) -> str:
    """Build a combined verification prompt (backward compatibility).

    Prefer using build_correspondence_prompt and build_nl_proof_prompt
    separately for clearer separation of concerns.
    """
    all_nodes = list(set(new_nodes) | set(modified_nodes))
    return build_correspondence_prompt(
        config, tablet, node_names=all_nodes, paper_tex=paper_tex,
    )
