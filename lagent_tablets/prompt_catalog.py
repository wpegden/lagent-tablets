"""Generate a representative catalog of every prompt form the project can emit.

The catalog is built from the real prompt builders in ``lagent_tablets.prompts``
against a deterministic fixture repo. Each emitted Markdown file corresponds to a
distinct branch-representative situation in the prompt layer.
"""

from __future__ import annotations

import argparse
import copy
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Sequence

from lagent_tablets.adapters import ProviderConfig
from lagent_tablets.config import (
    ChatConfig,
    Config,
    GitConfig,
    Policy,
    PromptNotesPolicy,
    SandboxConfig,
    TmuxConfig,
    VerificationConfig,
    VerificationPolicy,
    WorkflowConfig,
)
from lagent_tablets.project_paths import project_runtime_skills_dir
from lagent_tablets.prompts import (
    build_correspondence_prompt,
    build_node_soundness_prompt,
    build_nl_proof_prompt,
    build_reviewer_prompt,
    build_theorem_stating_prompt,
    build_theorem_stating_reviewer_prompt,
    build_verification_prompt,
    build_worker_prompt,
)
from lagent_tablets.state import SupervisorState, TabletNode, TabletState
from lagent_tablets.tablet import (
    PREAMBLE_NAME,
    find_unsupported_nodes,
    generate_node_lean,
    main_result_target_issues,
    node_lean_path,
    node_tex_path,
    regenerate_support_files,
)

REPO_SENTINEL = "/EXAMPLE_PROJECT"

PLACEHOLDER_HUMAN_INPUT = "[human feedback entered through the viewer]"
PLACEHOLDER_REVIEWER_GUIDANCE = "[reviewer guidance from the prior cycle]"
PLACEHOLDER_WORKER_OUTPUT = "[worker terminal output excerpt from the prior burst]"
PLACEHOLDER_PREVIOUS_CORRESPONDENCE = "[previous correspondence-agent finding from the prior cycle]"
PLACEHOLDER_PREVIOUS_SOUNDNESS = "[previous soundness finding for this node]"
PLACEHOLDER_PREVIOUS_INVALID_DETAIL = "[deterministic validation blocker from the prior attempt]"
PLACEHOLDER_BUILD_OUTPUT = "[Lean build output from the prior invalid attempt]"
PLACEHOLDER_POLICY_WORKER = "[policy note injected for workers]"
PLACEHOLDER_POLICY_REVIEWER = "[policy note injected for reviewers]"
PLACEHOLDER_POLICY_VERIFICATION = "[policy note injected for verification agents]"
PLACEHOLDER_MAIN_RESULT_ISSUE = "[configured target coverage issue computed from the current tablet]"
PLACEHOLDER_SUPPORT_NOTE = "[reviewer decision about an unsupported node from the previous cycle]"

PLACEHOLDERS = (
    PLACEHOLDER_HUMAN_INPUT,
    PLACEHOLDER_REVIEWER_GUIDANCE,
    PLACEHOLDER_WORKER_OUTPUT,
    PLACEHOLDER_PREVIOUS_CORRESPONDENCE,
    PLACEHOLDER_PREVIOUS_SOUNDNESS,
    PLACEHOLDER_PREVIOUS_INVALID_DETAIL,
    PLACEHOLDER_BUILD_OUTPUT,
    PLACEHOLDER_POLICY_WORKER,
    PLACEHOLDER_POLICY_REVIEWER,
    PLACEHOLDER_POLICY_VERIFICATION,
    PLACEHOLDER_MAIN_RESULT_ISSUE,
    PLACEHOLDER_SUPPORT_NOTE,
)


@dataclass(frozen=True)
class CatalogScenario:
    filename: str
    builder: str
    description: str
    render: Callable[["CatalogContext"], str]


@dataclass
class CatalogContext:
    repo_path: Path
    config: Config
    policy: Policy
    tablet: TabletState
    paper_ranges: Dict[str, tuple[int, int]]
    paper_text: str

    def clone_tablet(self) -> TabletState:
        return copy.deepcopy(self.tablet)

    def clone_state(self, **overrides: object) -> SupervisorState:
        state = SupervisorState()
        for key, value in overrides.items():
            setattr(state, key, value)
        return state

    def line_target(self, key: str) -> Dict[str, int]:
        start_line, end_line = self.paper_ranges[key]
        return {"start_line": start_line, "end_line": end_line}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _output_dir_default() -> Path:
    return _repo_root() / "prompt_catalog"


def _runtime_skill_names() -> List[str]:
    return [
        "THEOREM_STATING_WORKER.md",
        "THEOREM_STATING_REVIEWER.md",
        "PROOF_FORMALIZATION_WORKER.md",
        "PROOF_FORMALIZATION_REVIEWER.md",
    ]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_proof_bearing_node(
    repo: Path,
    *,
    name: str,
    env: str,
    lean_statement: str,
    imports: Sequence[str],
    title: str,
    statement: str,
    proof: str,
    tex_label: str = "",
    closed: bool,
) -> None:
    if closed:
        lines = [f"import {imp}" for imp in imports]
        lines.extend(
            [
                "",
                f"-- TABLET NODE: {name}",
                "-- Do not rename or remove the declaration below.",
                "",
                f"{lean_statement} := by",
                "  trivial",
                "",
            ]
        )
        lean_text = "\n".join(lines)
    else:
        lean_text = generate_node_lean(name, lean_statement, list(imports))
    _write_text(node_lean_path(repo, name), lean_text)

    tex_lines = [f"\\begin{{{env}}}[{title}]"]
    if tex_label:
        tex_lines.append(f"\\label{{{tex_label}}}")
    tex_lines.extend(
        [
            statement,
            f"\\end{{{env}}}",
            "",
            "\\begin{proof}",
            proof,
            "\\end{proof}",
            "",
        ]
    )
    _write_text(node_tex_path(repo, name), "\n".join(tex_lines))


def _write_definition_node(
    repo: Path,
    *,
    name: str,
    lean_definition: str,
    imports: Sequence[str],
    title: str,
    statement: str,
    tex_label: str = "",
) -> None:
    lines = [f"import {imp}" for imp in imports]
    lines.extend(
        [
            "",
            f"-- TABLET NODE: {name}",
            "-- Do not rename or remove the declaration below.",
            "",
            lean_definition,
            "",
        ]
    )
    _write_text(node_lean_path(repo, name), "\n".join(lines))

    tex_lines = [f"\\begin{{definition}}[{title}]"]
    if tex_label:
        tex_lines.append(f"\\label{{{tex_label}}}")
    tex_lines.extend([statement, "\\end{definition}", ""])
    _write_text(node_tex_path(repo, name), "\n".join(tex_lines))


def _build_paper() -> tuple[str, Dict[str, tuple[int, int]]]:
    lines: List[str] = []
    ranges: Dict[str, tuple[int, int]] = {}

    def add_block(key: str, block: Sequence[str]) -> None:
        start = len(lines) + 1
        lines.extend(block)
        end = len(lines)
        ranges[key] = (start, end)

    add_block(
        "intro",
        [
            "\\section{Prompt Catalog Example}",
            "This paper is synthetic and exists only to exercise prompt branches.",
            "",
        ],
    )
    add_block(
        "definition",
        [
            "\\begin{definition}[Weight profile]",
            "\\label{def:weight}",
            "The weight profile is a bookkeeping device for this example paper.",
            "\\end{definition}",
            "",
        ],
    )
    add_block(
        "lemma",
        [
            "\\begin{lemma}[Key lemma]",
            "\\label{lem:key}",
            "The key lemma supplies the local counting step used by the main result.",
            "\\end{lemma}",
            "",
        ],
    )
    add_block(
        "main_theorem",
        [
            "\\begin{theorem}[Main result]",
            "\\label{thm:main}",
            "The main theorem is decomposed into two tablet nodes in this synthetic fixture.",
            "\\end{theorem}",
            "",
        ],
    )
    add_block(
        "corollary",
        [
            "\\begin{corollary}[Explicit bound]",
            "\\label{cor:bound}",
            "The explicit bound follows from the main theorem.",
            "\\end{corollary}",
            "",
        ],
    )
    add_block(
        "unlabeled_theorem",
        [
            "\\begin{theorem}[Unlabeled target]",
            "This theorem has no TeX label, so the target system must fall back to its line range.",
            "\\end{theorem}",
            "",
        ],
    )
    return "\n".join(lines) + "\n", ranges


def _copy_runtime_skills(repo: Path) -> None:
    runtime_dir = project_runtime_skills_dir(repo / ".agent-supervisor")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    skills_root = _repo_root() / "skills"
    for name in _runtime_skill_names():
        shutil.copy2(skills_root / name, runtime_dir / name)


def _git(cmd: Sequence[str], *, repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *cmd],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_config(repo: Path, paper_path: Path, ranges: Mapping[str, tuple[int, int]]) -> Config:
    workflow = WorkflowConfig(
        start_phase="proof_formalization",
        paper_tex_path=paper_path,
        approved_axioms_path=repo / "approved_axioms.json",
        allowed_import_prefixes=["Mathlib"],
        forbidden_keyword_allowlist=[],
        human_input_path=repo / "human_input.md",
        input_request_path=repo / "input_request.json",
        main_result_targets=[
            {"tex_label": "thm:main"},
            {"tex_label": "cor:bound"},
            {
                "start_line": ranges["unlabeled_theorem"][0],
                "end_line": ranges["unlabeled_theorem"][1],
            },
        ],
    )
    return Config(
        repo_path=repo,
        goal_file=repo / "GOAL.md",
        state_dir=repo / ".agent-supervisor",
        worker=ProviderConfig(provider="codex", model="gpt-5.4"),
        reviewer=ProviderConfig(provider="claude", model="claude-opus-4-6"),
        verification=VerificationConfig(),
        tmux=TmuxConfig(
            session_name="prompt-catalog",
            dashboard_window_name="dashboard",
            kill_windows_after_capture=True,
            burst_user="lagentworker",
        ),
        sandbox=SandboxConfig(enabled=True, backend="bwrap"),
        workflow=workflow,
        chat=ChatConfig(
            root_dir=repo / "chats",
            repo_name="example",
            project_name="Prompt Catalog Example",
            public_base_url="http://example.invalid",
        ),
        git=GitConfig(
            remote_url=None,
            remote_name="origin",
            branch="main",
            author_name="Prompt Catalog",
            author_email="prompt-catalog@example.invalid",
        ),
        max_cycles=0,
        sleep_seconds=1.0,
        startup_timeout_seconds=60.0,
        burst_timeout_seconds=600.0,
    )


def _base_policy() -> Policy:
    return Policy(
        prompt_notes=PromptNotesPolicy(
            worker=PLACEHOLDER_POLICY_WORKER,
            reviewer=PLACEHOLDER_POLICY_REVIEWER,
            verification=PLACEHOLDER_POLICY_VERIFICATION,
        ),
        verification=VerificationPolicy(soundness_disagree_bias="reject"),
    )


def _build_fixture_context() -> CatalogContext:
    tmpdir = Path(tempfile.mkdtemp(prefix="lagent-prompt-catalog."))
    repo = tmpdir / "example_project"
    repo.mkdir(parents=True, exist_ok=True)

    paper_text, ranges = _build_paper()
    paper_path = repo / "paper" / "ExamplePaper.tex"
    _write_text(paper_path, paper_text)
    _write_text(repo / "GOAL.md", "Formalize the selected paper targets faithfully.\n")
    _write_text(repo / "PLAN.md", "1. Maintain the configured target-support DAG.\n2. Preserve paper faithfulness while proving nodes.\n")
    _write_text(repo / "TASKS.md", "- Keep proof-bearing nodes theorem-like in Lean.\n- Use structured paper provenance for paper-anchored statements.\n")
    _write_text(repo / "approved_axioms.json", "[]\n")
    _write_text(repo / ".agent-supervisor" / "scripts" / "check.py", "#!/usr/bin/env python3\nprint('ok')\n")
    _write_text(repo / ".agent-supervisor" / "scripts" / "check_node.sh", "#!/bin/sh\necho ok\n")
    _write_text(repo / ".agent-supervisor" / "scripts" / "check_tablet.sh", "#!/bin/sh\necho ok\n")
    (repo / ".agent-supervisor" / "staging").mkdir(parents=True, exist_ok=True)
    (repo / ".agent-supervisor" / "scratch").mkdir(parents=True, exist_ok=True)
    _copy_runtime_skills(repo)

    _write_text(
        repo / "Tablet" / "Preamble.lean",
        "import Mathlib.Data.Nat.Basic\nimport Mathlib.Tactic\n",
    )
    _write_text(
        repo / "Tablet" / "Preamble.tex",
        "\n".join(
            [
                "\\begin{definition}[Ambient conventions]",
                "The paper works over a fixed finite universe.",
                "\\end{definition}",
                "",
                "\\begin{proposition}[Imported background fact]",
                "A finite combinatorial bound from Mathlib is used without proof.",
                "\\end{proposition}",
                "",
            ]
        ),
    )

    _write_definition_node(
        repo,
        name="weight_profile",
        lean_definition="def weight_profile : Nat := 0",
        imports=["Tablet.Preamble"],
        title="Weight profile",
        statement="This node packages the bookkeeping definition used downstream.",
        tex_label="def:weight",
    )
    _write_proof_bearing_node(
        repo,
        name="local_counting_helper",
        env="helper",
        lean_statement="theorem local_counting_helper : True",
        imports=["Tablet.Preamble", "Tablet.weight_profile"],
        title="Local counting helper",
        statement="This helper captures a paper-faithful local counting step.",
        proof="The paper's local argument already implies this helper directly.",
        closed=True,
    )
    _write_proof_bearing_node(
        repo,
        name="key_lemma",
        env="lemma",
        lean_statement="theorem key_lemma : True",
        imports=["Tablet.Preamble", "Tablet.weight_profile", "Tablet.local_counting_helper"],
        title="Key lemma",
        statement="This lemma records the key intermediate statement cited later in the paper.",
        proof="Combine the definition node with the helper node.",
        tex_label="lem:key",
        closed=True,
    )
    _write_proof_bearing_node(
        repo,
        name="main_result_part_a",
        env="theorem",
        lean_statement="theorem main_result_part_a : True",
        imports=["Tablet.Preamble", "Tablet.key_lemma"],
        title="Main result, part A",
        statement="This node states the coarse first half of the main theorem.",
        proof="This coarse version is intentionally weak in the baseline fixture.",
        tex_label="thm:main",
        closed=True,
    )
    _write_proof_bearing_node(
        repo,
        name="main_result_part_b",
        env="theorem",
        lean_statement="theorem main_result_part_b : True",
        imports=["Tablet.Preamble", "Tablet.main_result_part_a"],
        title="Main result, part B",
        statement="This node states the second half of the main theorem.",
        proof="This node depends on part A and will later be sharpened.",
        tex_label="thm:main",
        closed=True,
    )
    _write_proof_bearing_node(
        repo,
        name="bound_corollary",
        env="corollary",
        lean_statement="theorem bound_corollary : True",
        imports=["Tablet.Preamble", "Tablet.main_result_part_b"],
        title="Explicit bound",
        statement="The explicit bound follows from the decomposed main theorem.",
        proof="Apply the second main-result node directly.",
        tex_label="cor:bound",
        closed=False,
    )
    _write_proof_bearing_node(
        repo,
        name="unlabeled_target",
        env="theorem",
        lean_statement="theorem unlabeled_target : True",
        imports=["Tablet.Preamble", "Tablet.key_lemma"],
        title="Unlabeled target",
        statement="This node covers the unlabeled theorem by its paper line range.",
        proof="Invoke the key lemma exactly as in the paper.",
        closed=False,
    )
    _write_proof_bearing_node(
        repo,
        name="floating_note",
        env="helper",
        lean_statement="theorem floating_note : True",
        imports=["Tablet.Preamble"],
        title="Floating note",
        statement="This helper is intentionally unsupported so the prompts can show cleanup guidance.",
        proof="It has no real downstream consumer.",
        closed=False,
    )

    tablet = TabletState(
        nodes={
            PREAMBLE_NAME: TabletNode(name=PREAMBLE_NAME, kind="preamble", status="closed"),
            "weight_profile": TabletNode(
                name="weight_profile",
                kind="ordinary",
                status="closed",
                title="Weight profile",
                paper_provenance={
                    "start_line": ranges["definition"][0],
                    "end_line": ranges["definition"][1],
                    "tex_label": "def:weight",
                },
            ),
            "local_counting_helper": TabletNode(
                name="local_counting_helper",
                kind="helper_lemma",
                status="closed",
                title="Local counting helper",
                paper_provenance={
                    "start_line": ranges["lemma"][0],
                    "end_line": ranges["lemma"][1],
                },
            ),
            "key_lemma": TabletNode(
                name="key_lemma",
                kind="ordinary",
                status="closed",
                title="Key lemma",
                paper_provenance={
                    "start_line": ranges["lemma"][0],
                    "end_line": ranges["lemma"][1],
                    "tex_label": "lem:key",
                },
            ),
            "main_result_part_a": TabletNode(
                name="main_result_part_a",
                kind="ordinary",
                status="open",
                title="Main result, part A",
                paper_provenance={
                    "start_line": ranges["main_theorem"][0],
                    "end_line": ranges["main_theorem"][1],
                    "tex_label": "thm:main",
                },
                coarse=True,
            ),
            "main_result_part_b": TabletNode(
                name="main_result_part_b",
                kind="ordinary",
                status="open",
                title="Main result, part B",
                paper_provenance={
                    "start_line": ranges["main_theorem"][0],
                    "end_line": ranges["main_theorem"][1],
                    "tex_label": "thm:main",
                },
                coarse=True,
            ),
            "bound_corollary": TabletNode(
                name="bound_corollary",
                kind="ordinary",
                status="open",
                title="Explicit bound",
                paper_provenance={
                    "start_line": ranges["corollary"][0],
                    "end_line": ranges["corollary"][1],
                    "tex_label": "cor:bound",
                },
            ),
            "unlabeled_target": TabletNode(
                name="unlabeled_target",
                kind="ordinary",
                status="open",
                title="Unlabeled target",
                paper_provenance={
                    "start_line": ranges["unlabeled_theorem"][0],
                    "end_line": ranges["unlabeled_theorem"][1],
                },
            ),
            "floating_note": TabletNode(
                name="floating_note",
                kind="helper_lemma",
                status="open",
                title="Floating note",
            ),
        },
        active_node="main_result_part_b",
        seeded_at_cycle=0,
        last_modified_at_cycle=4,
    )
    regenerate_support_files(tablet, repo)

    _git(["init"], repo=repo)
    _git(["config", "user.name", "Prompt Catalog"], repo=repo)
    _git(["config", "user.email", "prompt-catalog@example.invalid"], repo=repo)
    _git(["add", "."], repo=repo)
    _git(["commit", "-m", "baseline"], repo=repo)
    _git(["tag", "cycle-3"], repo=repo)

    _write_proof_bearing_node(
        repo,
        name="main_result_part_a",
        env="theorem",
        lean_statement="theorem main_result_part_a (h : True) : True",
        imports=["Tablet.Preamble", "Tablet.key_lemma"],
        title="Main result, part A",
        statement="This node states the sharpened first half of the main theorem under an explicit hypothesis.",
        proof="The sharpened statement still follows from the same paper step, but the wording has changed since cycle 3.",
        tex_label="thm:main",
        closed=False,
    )
    _write_proof_bearing_node(
        repo,
        name="main_result_part_b",
        env="theorem",
        lean_statement="theorem main_result_part_b (h : True) : True",
        imports=["Tablet.Preamble", "Tablet.main_result_part_a"],
        title="Main result, part B",
        statement="This node now phrases the second half of the main theorem using the sharpened first half.",
        proof="Its paper-facing wording also changed since cycle 3.",
        tex_label="thm:main",
        closed=False,
    )
    _write_proof_bearing_node(
        repo,
        name="bound_corollary",
        env="corollary",
        lean_statement="theorem bound_corollary : True",
        imports=["Tablet.Preamble", "Tablet.main_result_part_b"],
        title="Explicit bound",
        statement="The explicit bound is now open so proof-formalization can target it.",
        proof="This synthetic proof remains incomplete on purpose.",
        tex_label="cor:bound",
        closed=False,
    )
    regenerate_support_files(tablet, repo)

    config = _make_config(repo, paper_path, ranges)
    return CatalogContext(
        repo_path=repo,
        config=config,
        policy=_base_policy(),
        tablet=tablet,
        paper_ranges=ranges,
        paper_text=paper_text,
    )


def _sanitize_text(text: str, repo_path: Path) -> str:
    sanitized = text.replace(str(repo_path), REPO_SENTINEL)
    return sanitized


def _format_prompt_markdown(scenario: CatalogScenario, prompt_text: str, repo_path: Path) -> str:
    sanitized = _sanitize_text(prompt_text, repo_path)
    lines = [
        f"# {scenario.filename[:-3]}",
        "",
        f"- Builder: `{scenario.builder}`",
        f"- Situation: {scenario.description}",
    ]
    used_placeholders = [placeholder for placeholder in PLACEHOLDERS if placeholder in sanitized]
    if used_placeholders:
        lines.append("- Bracketed placeholders in this file stand for dynamic runtime text from agents, humans, or policy injection:")
        for placeholder in used_placeholders:
            lines.append(f"  - `{placeholder}`")
    lines.extend(["", "```text", sanitized.rstrip(), "```", ""])
    return "\n".join(lines)


def _proof_worker_easy_local(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.active_node = "bound_corollary"
    tablet.nodes["bound_corollary"].difficulty = "easy"
    state = ctx.clone_state(
        cycle=4,
        phase="proof_formalization",
        active_node="bound_corollary",
        human_input=PLACEHOLDER_HUMAN_INPUT,
        human_input_at_cycle=3,
        last_review={
            "decision": "CONTINUE",
            "reason": "local proof work is still possible",
            "next_prompt": PLACEHOLDER_REVIEWER_GUIDANCE,
            "paper_focus_ranges": [
                {
                    "start_line": ctx.paper_ranges["corollary"][0],
                    "end_line": ctx.paper_ranges["corollary"][1],
                    "reason": "explicit bound statement",
                }
            ],
        },
    )
    previous_outcome = {
        "outcome": "INVALID",
        "detail": PLACEHOLDER_PREVIOUS_INVALID_DETAIL,
        "build_output": PLACEHOLDER_BUILD_OUTPUT,
    }
    return build_worker_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        previous_outcome=previous_outcome,
        difficulty="easy",
    )


def _proof_worker_hard_local(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.active_node = "main_result_part_b"
    state = ctx.clone_state(
        cycle=5,
        phase="proof_formalization",
        active_node="main_result_part_b",
        last_review={
            "decision": "CONTINUE",
            "reason": "stay local but rethink the proof skeleton",
            "next_prompt": PLACEHOLDER_REVIEWER_GUIDANCE,
            "paper_focus_ranges": [
                {
                    "start_line": ctx.paper_ranges["main_theorem"][0],
                    "end_line": ctx.paper_ranges["main_theorem"][1],
                    "reason": "main result wording",
                }
            ],
        },
    )
    previous_outcome = {
        "outcome": "REJECTED",
        "detail": "verification found a paper-faithfulness gap",
        "rejection": {
            "summary": PLACEHOLDER_PREVIOUS_CORRESPONDENCE,
            "correspondence": {
                "decision": "FAIL",
                "issues": [{"node": "main_result_part_b", "description": PLACEHOLDER_PREVIOUS_CORRESPONDENCE}],
            },
        },
    }
    return build_worker_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        previous_outcome=previous_outcome,
        difficulty="hard",
    )


def _proof_worker_hard_restructure(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.active_node = "main_result_part_b"
    state = ctx.clone_state(
        cycle=5,
        phase="proof_formalization",
        active_node="main_result_part_b",
        proof_target_edit_mode="restructure",
        last_review={"next_prompt": PLACEHOLDER_REVIEWER_GUIDANCE},
    )
    return build_worker_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        difficulty="hard",
    )


def _proof_worker_hard_coarse_restructure(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.active_node = "main_result_part_b"
    state = ctx.clone_state(
        cycle=5,
        phase="proof_formalization",
        active_node="main_result_part_b",
        proof_target_edit_mode="coarse_restructure",
        last_review={"next_prompt": PLACEHOLDER_REVIEWER_GUIDANCE},
    )
    return build_worker_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        difficulty="hard",
    )


def _proof_worker_cleanup(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.nodes.pop("floating_note", None)
    for name, node in tablet.nodes.items():
        if name != PREAMBLE_NAME:
            node.status = "closed"
    tablet.active_node = "key_lemma"
    state = ctx.clone_state(
        cycle=9,
        phase="proof_complete_style_cleanup",
        active_node="key_lemma",
        last_review={"next_prompt": PLACEHOLDER_REVIEWER_GUIDANCE},
    )
    return build_worker_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        cleanup_check_payload_path=Path("/EXAMPLE_SCOPE/cleanup_scope.json"),
    )


def _theorem_worker_broad_initial_empty(ctx: CatalogContext) -> str:
    tablet = TabletState()
    state = ctx.clone_state(cycle=1, phase="theorem_stating")
    return build_theorem_stating_prompt(ctx.config, state, tablet, ctx.policy)


def _theorem_worker_broad_with_blockers(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    state = ctx.clone_state(
        cycle=3,
        phase="theorem_stating",
        last_review={
            "decision": "CONTINUE",
            "next_prompt": PLACEHOLDER_REVIEWER_GUIDANCE,
            "paper_focus_ranges": [
                {
                    "start_line": ctx.paper_ranges["main_theorem"][0],
                    "end_line": ctx.paper_ranges["corollary"][1],
                    "reason": "current configured target frontier",
                }
            ],
            "support_resolutions": [
                {
                    "node": "floating_note",
                    "action": "remove",
                    "reason": PLACEHOLDER_SUPPORT_NOTE,
                    "suggested_parents": [],
                }
            ],
        },
        open_blockers=[
            {
                "node": "main_result_part_a",
                "phase": "correspondence",
                "reason": "The Lean statement is still weaker than the paper statement.",
            }
        ],
        validation_summary={
            "last_outcome": "INVALID",
            "last_invalid_detail": PLACEHOLDER_PREVIOUS_INVALID_DETAIL,
            "attempt": 2,
            "consecutive_invalids": 2,
            "last_reset_to_checkpoint": "",
        },
    )
    return build_theorem_stating_prompt(ctx.config, state, tablet, ctx.policy)


def _theorem_worker_target_repair(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    state = ctx.clone_state(
        cycle=4,
        phase="theorem_stating",
        theorem_soundness_target="main_result_part_b",
        theorem_target_edit_mode="repair",
        last_review={"next_prompt": PLACEHOLDER_REVIEWER_GUIDANCE},
    )
    return build_theorem_stating_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        repair_scope_check_payload_path=Path("/EXAMPLE_SCOPE/target_repair_scope.json"),
    )


def _theorem_worker_target_restructure(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    state = ctx.clone_state(
        cycle=4,
        phase="theorem_stating",
        theorem_soundness_target="main_result_part_b",
        theorem_target_edit_mode="restructure",
        last_review={"next_prompt": PLACEHOLDER_REVIEWER_GUIDANCE},
    )
    return build_theorem_stating_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        authorized_region=["key_lemma", "main_result_part_a", "main_result_part_b", "bound_corollary"],
        scoped_tablet_check_payload_path=Path("/EXAMPLE_SCOPE/theorem_target_scope.json"),
        edit_scope_check_payload_path=Path("/EXAMPLE_SCOPE/theorem_edit_scope.json"),
    )


def _proof_reviewer_standard(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    state = ctx.clone_state(
        cycle=6,
        phase="proof_formalization",
        review_log=[
            {"cycle": 4, "decision": "CONTINUE", "reason": "keep the proof local"},
            {"cycle": 5, "decision": "CONTINUE", "reason": "repair the weakened statement"},
        ],
        human_input=PLACEHOLDER_HUMAN_INPUT,
        human_input_at_cycle=5,
    )
    nl_verification = [
        {
            "check": "correspondence",
            "overall": "DISAGREE",
            "summary": "Two agents disagreed about whether the new theorem statement still matches the paper.",
            "agent_results": [
                {
                    "agent": "Verifier A",
                    "overall": "APPROVE",
                    "summary": PLACEHOLDER_PREVIOUS_CORRESPONDENCE,
                    "correspondence": {"decision": "PASS", "issues": []},
                },
                {
                    "agent": "Verifier B",
                    "overall": "REJECT",
                    "summary": PLACEHOLDER_PREVIOUS_CORRESPONDENCE,
                    "correspondence": {
                        "decision": "FAIL",
                        "issues": [
                            {
                                "node": "main_result_part_b",
                                "description": "The sharpened hypothesis is not justified by the paper statement.",
                            }
                        ],
                    },
                },
            ],
        },
        {
            "check": "nl_proof",
            "overall": "REJECT",
            "summary": "The NL proof panel split on the active node.",
            "node_verdicts": [
                {
                    "node": "main_result_part_b",
                    "overall": "REJECT",
                    "panel_split": True,
                    "agent_results": [
                        {"agent": "Verifier A", "overall": "APPROVE"},
                        {"agent": "Verifier B", "overall": "REJECT"},
                    ],
                }
            ],
        },
    ]
    return build_reviewer_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        worker_handoff={"summary": "Tried a local rewrite.", "status": "NOT_STUCK"},
        worker_output=PLACEHOLDER_WORKER_OUTPUT,
        validation_summary={
            "outcome": "INVALID",
            "detail": PLACEHOLDER_PREVIOUS_INVALID_DETAIL,
            "consecutive_invalids": 2,
        },
        nl_verification=nl_verification,
    )


def _proof_reviewer_cleanup(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.nodes.pop("floating_note", None)
    for name, node in tablet.nodes.items():
        if name != PREAMBLE_NAME:
            node.status = "closed"
    state = ctx.clone_state(
        cycle=11,
        phase="proof_complete_style_cleanup",
        review_log=[{"cycle": 10, "decision": "CONTINUE", "reason": "style cleanup is still productive"}],
    )
    return build_reviewer_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        worker_handoff={"summary": "Normalized theorem docstrings.", "status": "DONE"},
        worker_output=PLACEHOLDER_WORKER_OUTPUT,
        validation_summary={"outcome": "PROGRESS", "detail": "cleanup preserved semantics"},
    )


def _theorem_reviewer_with_unsupported_nodes(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    state = ctx.clone_state(
        cycle=4,
        phase="theorem_stating",
        theorem_soundness_target="main_result_part_b",
        theorem_target_edit_mode="repair",
        open_blockers=[
            {
                "node": "main_result_part_a",
                "phase": "correspondence",
                "reason": "The sharpened theorem statement still drops one paper hypothesis.",
            }
        ],
        review_log=[{"cycle": 3, "decision": "CONTINUE", "reason": "repair the target-support slice"}],
    )
    unsupported_nodes = find_unsupported_nodes(
        tablet,
        ctx.config.repo_path,
        ctx.config.workflow.main_result_targets,
    )
    return build_theorem_stating_reviewer_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        worker_handoff={"summary": "Rephrased the target theorem.", "status": "STUCK"},
        worker_output=PLACEHOLDER_WORKER_OUTPUT,
        nl_verification=[
            {
                "check": "correspondence",
                "overall": "REJECT",
                "summary": "The frontier still has a paper-faithfulness gap.",
                "correspondence": {
                    "decision": "FAIL",
                    "issues": [
                        {
                            "node": "main_result_part_a",
                            "description": "The Lean statement is weaker than the cited paper theorem.",
                        }
                    ],
                },
            },
            {
                "check": "nl_proof",
                "overall": "REJECT",
                "summary": "The soundness panel reported a structural objection.",
                "node_verdicts": [
                    {
                        "node": "main_result_part_b",
                        "overall": "REJECT",
                        "agent_results": [
                            {"agent": "Verifier A", "overall": "APPROVE", "soundness": {"decision": "SOUND"}},
                            {"agent": "Verifier B", "overall": "REJECT", "soundness": {"decision": "STRUCTURAL"}},
                        ],
                    }
                ],
            },
        ],
        validation_summary={"outcome": "PROGRESS", "detail": "worker handoff accepted"},
        unsupported_nodes=unsupported_nodes,
    )


def _theorem_reviewer_with_target_issues(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.nodes.pop("bound_corollary", None)
    target_issues = main_result_target_issues(
        tablet,
        ctx.config.repo_path,
        ctx.config.workflow.main_result_targets,
    )
    state = ctx.clone_state(cycle=4, phase="theorem_stating")
    return build_theorem_stating_reviewer_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        main_result_issues=target_issues,
    )


def _theorem_reviewer_invalid_with_reset(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    state = ctx.clone_state(cycle=4, phase="theorem_stating")
    return build_theorem_stating_reviewer_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        worker_handoff={"summary": "Fundamental gap encountered.", "status": "CRISIS"},
        worker_output=PLACEHOLDER_WORKER_OUTPUT,
        validation_summary={
            "outcome": "INVALID",
            "detail": PLACEHOLDER_PREVIOUS_INVALID_DETAIL,
            "consecutive_invalids": 5,
        },
        available_reset_checkpoints=[
            {"ref": "initial", "label": "initial setup commit"},
            {"ref": "cycle-3", "label": "cycle 3 | reviewer/final | theorem_stating | PROGRESS"},
        ],
    )


def _theorem_reviewer_target_resolved(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    state = ctx.clone_state(
        cycle=4,
        phase="theorem_stating",
        theorem_soundness_target="main_result_part_b",
        theorem_target_edit_mode="repair",
    )
    return build_theorem_stating_reviewer_prompt(
        ctx.config,
        state,
        tablet,
        ctx.policy,
        nl_verification=[
            {
                "check": "nl_proof",
                "overall": "APPROVE",
                "summary": "The current target passed the panel.",
                "node_verdicts": [
                    {
                        "node": "main_result_part_b",
                        "overall": "APPROVE",
                        "agent_results": [
                            {"agent": "Verifier A", "overall": "APPROVE", "soundness": {"decision": "SOUND"}},
                            {"agent": "Verifier B", "overall": "APPROVE", "soundness": {"decision": "SOUND"}},
                        ],
                    }
                ],
            }
        ],
    )


def _correspondence_basic(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    return build_correspondence_prompt(
        ctx.config,
        tablet,
        node_names=["main_result_part_b"],
        human_input=PLACEHOLDER_HUMAN_INPUT,
        output_file="correspondence_result_0.json",
    )


def _correspondence_single_changed(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.nodes["main_result_part_a"].verification_at_cycle = 3
    tablet.nodes["main_result_part_a"].correspondence_status = "pass"
    return build_correspondence_prompt(
        ctx.config,
        tablet,
        node_names=["main_result_part_a"],
        output_file="correspondence_result_1.json",
        previous_results=[
            {
                "agent": "Verifier A",
                "correspondence": {
                    "issues": [
                        {
                            "node": "main_result_part_a",
                            "description": PLACEHOLDER_PREVIOUS_CORRESPONDENCE,
                        }
                    ]
                },
                "paper_faithfulness": {"issues": []},
            }
        ],
    )


def _correspondence_full_context(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    tablet.nodes["main_result_part_a"].verification_at_cycle = 3
    tablet.nodes["main_result_part_a"].correspondence_status = "pass"
    tablet.nodes["main_result_part_b"].verification_at_cycle = 3
    tablet.nodes["main_result_part_b"].correspondence_status = "pass"
    return build_correspondence_prompt(
        ctx.config,
        tablet,
        node_names=["Preamble", "main_result_part_a", "main_result_part_b"],
        human_input=PLACEHOLDER_HUMAN_INPUT,
        output_file="correspondence_result_2.json",
        previous_results=[
            {
                "agent": "Verifier A",
                "correspondence": {
                    "issues": [
                        {
                            "node": "main_result_part_a",
                            "description": PLACEHOLDER_PREVIOUS_CORRESPONDENCE,
                        }
                    ]
                },
                "paper_faithfulness": {
                    "issues": [
                        {
                            "node": "main_result_part_b",
                            "description": PLACEHOLDER_PREVIOUS_CORRESPONDENCE,
                        }
                    ]
                },
            }
        ],
    )


def _nl_proof_batch(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    return build_nl_proof_prompt(
        ctx.config,
        tablet,
        node_names=["main_result_part_a", "main_result_part_b"],
        paper_tex=ctx.paper_text,
        human_input=PLACEHOLDER_HUMAN_INPUT,
        output_file="nl_proof_result.json",
    )


def _node_soundness_with_children(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    return build_node_soundness_prompt(
        ctx.config,
        tablet,
        node_name="main_result_part_b",
        paper_tex=ctx.paper_text,
        human_input=PLACEHOLDER_HUMAN_INPUT,
        output_file="nl_proof_main_result_part_b_0.json",
        previous_issues=[PLACEHOLDER_PREVIOUS_SOUNDNESS],
    )


def _node_soundness_leaf(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    return build_node_soundness_prompt(
        ctx.config,
        tablet,
        node_name="floating_note",
        output_file="nl_proof_floating_note_0.json",
    )


def _verification_wrapper(ctx: CatalogContext) -> str:
    tablet = ctx.clone_tablet()
    return build_verification_prompt(
        ctx.config,
        tablet,
        new_nodes=["main_result_part_a"],
        modified_nodes=["main_result_part_b"],
        paper_tex=ctx.paper_text,
    )


SCENARIOS: List[CatalogScenario] = [
    CatalogScenario(
        filename="proof_worker_easy_local.md",
        builder="build_worker_prompt",
        description="Proof-formalization worker on an easy local node with prior invalid feedback and targeted paper focus.",
        render=_proof_worker_easy_local,
    ),
    CatalogScenario(
        filename="proof_worker_hard_local.md",
        builder="build_worker_prompt",
        description="Proof-formalization worker on a hard local node with reviewer guidance and prior verification rejection.",
        render=_proof_worker_hard_local,
    ),
    CatalogScenario(
        filename="proof_worker_hard_restructure.md",
        builder="build_worker_prompt",
        description="Proof-formalization worker in reviewer-authorized restructure mode for the active target's impact region.",
        render=_proof_worker_hard_restructure,
    ),
    CatalogScenario(
        filename="proof_worker_hard_coarse_restructure.md",
        builder="build_worker_prompt",
        description="Proof-formalization worker in reviewer-authorized coarse-restructure mode with accepted coarse-package mutation allowed.",
        render=_proof_worker_hard_coarse_restructure,
    ),
    CatalogScenario(
        filename="proof_worker_cleanup.md",
        builder="build_worker_prompt",
        description="Proof-complete style cleanup worker prompt.",
        render=_proof_worker_cleanup,
    ),
    CatalogScenario(
        filename="theorem_worker_broad_initial_empty.md",
        builder="build_theorem_stating_prompt",
        description="Theorem-stating worker at cycle start with an empty tablet.",
        render=_theorem_worker_broad_initial_empty,
    ),
    CatalogScenario(
        filename="theorem_worker_broad_with_blockers_and_retry.md",
        builder="build_theorem_stating_prompt",
        description="Theorem-stating worker in broad mode with reviewer guidance, open blockers, support actions, and a preserved invalid retry.",
        render=_theorem_worker_broad_with_blockers,
    ),
    CatalogScenario(
        filename="theorem_worker_target_repair.md",
        builder="build_theorem_stating_prompt",
        description="Theorem-stating worker locked to a current soundness target in repair mode.",
        render=_theorem_worker_target_repair,
    ),
    CatalogScenario(
        filename="theorem_worker_target_restructure.md",
        builder="build_theorem_stating_prompt",
        description="Theorem-stating worker on a current soundness target with reviewer-authorized restructure and scoped checks.",
        render=_theorem_worker_target_restructure,
    ),
    CatalogScenario(
        filename="proof_reviewer_standard.md",
        builder="build_reviewer_prompt",
        description="Proof-formalization reviewer with worker output, invalid history, disagreement in verification, and unsupported-node warning.",
        render=_proof_reviewer_standard,
    ),
    CatalogScenario(
        filename="proof_reviewer_cleanup.md",
        builder="build_reviewer_prompt",
        description="Proof-complete style cleanup reviewer prompt.",
        render=_proof_reviewer_cleanup,
    ),
    CatalogScenario(
        filename="theorem_reviewer_with_unsupported_nodes.md",
        builder="build_theorem_stating_reviewer_prompt",
        description="Theorem-stating reviewer with current verification results, a held soundness target, and unsupported-node decisions to make.",
        render=_theorem_reviewer_with_unsupported_nodes,
    ),
    CatalogScenario(
        filename="theorem_reviewer_with_main_result_target_issues.md",
        builder="build_theorem_stating_reviewer_prompt",
        description="Theorem-stating reviewer prompt when configured main-result targets are still missing or helper-only.",
        render=_theorem_reviewer_with_target_issues,
    ),
    CatalogScenario(
        filename="theorem_reviewer_invalid_with_reset_options.md",
        builder="build_theorem_stating_reviewer_prompt",
        description="Theorem-stating reviewer on an invalid attempt with a worker crisis report and supervisor-approved reset checkpoints.",
        render=_theorem_reviewer_invalid_with_reset,
    ),
    CatalogScenario(
        filename="theorem_reviewer_target_resolved.md",
        builder="build_theorem_stating_reviewer_prompt",
        description="Theorem-stating reviewer after the current soundness target has already passed this cycle.",
        render=_theorem_reviewer_target_resolved,
    ),
    CatalogScenario(
        filename="correspondence_basic.md",
        builder="build_correspondence_prompt",
        description="Basic correspondence / paper-faithfulness verification for one node.",
        render=_correspondence_basic,
    ),
    CatalogScenario(
        filename="correspondence_single_changed_node.md",
        builder="build_correspondence_prompt",
        description="Correspondence verification with old-vs-new context for one node that reopened the frontier.",
        render=_correspondence_single_changed,
    ),
    CatalogScenario(
        filename="correspondence_full_context_multiple_changed_nodes.md",
        builder="build_correspondence_prompt",
        description="Correspondence verification including preamble items, provenance excerpts, previous results, and multiple changed nodes.",
        render=_correspondence_full_context,
    ),
    CatalogScenario(
        filename="nl_proof_batch.md",
        builder="build_nl_proof_prompt",
        description="Batch NL-proof soundness verification prompt.",
        render=_nl_proof_batch,
    ),
    CatalogScenario(
        filename="node_soundness_with_children_and_previous_issues.md",
        builder="build_node_soundness_prompt",
        description="Single-node soundness prompt for a node with children, paper context, and prior issues.",
        render=_node_soundness_with_children,
    ),
    CatalogScenario(
        filename="node_soundness_leaf.md",
        builder="build_node_soundness_prompt",
        description="Single-node soundness prompt for a leaf node.",
        render=_node_soundness_leaf,
    ),
    CatalogScenario(
        filename="verification_wrapper_compat.md",
        builder="build_verification_prompt",
        description="Backward-compatible combined verification wrapper prompt.",
        render=_verification_wrapper,
    ),
]


def _readme_text(output_dir: Path) -> str:
    lines = [
        "# Prompt Catalog",
        "",
        "This folder is fully generated from the live prompt builders in `lagent_tablets/prompts.py`.",
        "",
        "Regenerate it with:",
        "",
        "```bash",
        "python3 scripts/generate_prompt_catalog.py",
        "```",
        "",
        "Conventions:",
        f"- Absolute example-repo paths are normalized to `{REPO_SENTINEL}`.",
        "- Bracketed text such as `[worker terminal output excerpt from the prior burst]` marks dynamic runtime text whose exact contents depend on prior agent or human activity.",
        "- Each Markdown file corresponds to one branch-representative situation, not an arbitrary sample.",
        "",
        "Scenarios:",
    ]
    for scenario in SCENARIOS:
        lines.append(f"- [{scenario.filename}]({scenario.filename}): {scenario.description}")
    lines.append("")
    return "\n".join(lines)


def generate_prompt_catalog(output_dir: Path) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in output_dir.glob("*.md"):
        path.unlink()

    ctx = _build_fixture_context()
    written: List[Path] = []
    readme_path = output_dir / "README.md"
    _write_text(readme_path, _readme_text(output_dir))
    written.append(readme_path)

    for scenario in SCENARIOS:
        prompt_text = scenario.render(ctx)
        markdown = _format_prompt_markdown(scenario, prompt_text, ctx.repo_path)
        path = output_dir / scenario.filename
        _write_text(path, markdown)
        written.append(path)

    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a checked-in catalog of prompt outputs.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_output_dir_default(),
        help="Directory to populate with generated Markdown prompt files.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    generate_prompt_catalog(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
