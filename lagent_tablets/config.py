"""Configuration loading and policy management.

Config is loaded from a JSON file and parsed into typed dataclasses.
Policy is a separate hot-reloadable JSON file for runtime tuning.
Both are re-checked each cycle via mtime comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from lagent_tablets.adapters import ProviderConfig


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TmuxConfig:
    session_name: str
    dashboard_window_name: str
    kill_windows_after_capture: bool
    burst_user: str
    burst_group: Optional[str] = None
    burst_home: Optional[Path] = None


@dataclass
class PhaseOverride:
    """Per-phase overrides for provider models."""
    worker_model: Optional[str] = None
    reviewer_model: Optional[str] = None


@dataclass
class WorkflowConfig:
    start_phase: str
    paper_tex_path: Optional[Path]
    approved_axioms_path: Path
    allowed_import_prefixes: List[str]
    forbidden_keyword_allowlist: List[str]
    human_input_path: Path
    input_request_path: Path
    phase_overrides: Dict[str, PhaseOverride] = field(default_factory=dict)


@dataclass
class ChatConfig:
    root_dir: Path
    repo_name: str
    project_name: str
    public_base_url: str


@dataclass
class GitConfig:
    remote_url: Optional[str]
    remote_name: str
    branch: str
    author_name: str
    author_email: str


@dataclass
class BranchingConfig:
    max_current_branches: int = 2
    evaluation_cycle_budget: int = 20
    poll_seconds: float = 300.0


@dataclass
class CorrespondenceAgentConfig:
    """Config for one correspondence/soundness verification agent."""
    provider: str = "claude"
    model: str = "claude-opus-4-6"
    effort: Optional[str] = None  # codex: xhigh, claude: max, etc.
    extra_args: List[str] = field(default_factory=list)
    fallback_models: List[str] = field(default_factory=list)
    label: str = ""  # human-readable label for disagreement reporting


@dataclass
class VerificationConfig:
    """Config for the NL verification model (strongest available, with thinking)."""
    provider: str = "claude"
    model: str = "claude-opus-4-6"
    extra_args: List[str] = field(default_factory=list)
    thinking_budget: str = "high"
    max_context_tokens: int = 50000
    correspondence_agents: List[CorrespondenceAgentConfig] = field(default_factory=list)
    soundness_agents: List[CorrespondenceAgentConfig] = field(default_factory=list)


@dataclass
class Config:
    repo_path: Path
    goal_file: Path
    state_dir: Path
    worker: ProviderConfig
    reviewer: ProviderConfig
    verification: VerificationConfig
    tmux: TmuxConfig
    workflow: WorkflowConfig
    chat: ChatConfig
    git: GitConfig
    max_cycles: int
    sleep_seconds: float
    startup_timeout_seconds: float
    burst_timeout_seconds: float
    branching: BranchingConfig = field(default_factory=BranchingConfig)
    easy_worker: Optional[ProviderConfig] = None
    hard_worker: Optional[ProviderConfig] = None
    policy_path: Optional[Path] = None
    source_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Policy dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StuckRecoveryPolicy:
    mainline_max_attempts: int = 10
    branch_max_attempts: int = 4


@dataclass(frozen=True)
class BranchingPolicy:
    evaluation_cycle_budget: int = 20
    poll_seconds: float = 300.0
    proposal_cooldown_reviews: int = 5
    replacement_min_confidence: float = 0.8
    selection_recheck_increments_reviews: Tuple[int, ...] = (5,)


@dataclass(frozen=True)
class TimingPolicy:
    sleep_seconds: float = 1.0
    agent_retry_delays_seconds: Tuple[float, ...] = (3600.0, 7200.0, 10800.0)
    budget_error_max_retries: int = 20
    subprocess_timeout_seconds: float = 120.0
    burst_timeout_seconds: float = 14400.0
    stall_threshold_seconds: float = 900.0
    max_stall_recoveries_per_burst: int = 3


@dataclass(frozen=True)
class CodexBudgetPausePolicy:
    weekly_percent_left_threshold: float = 15.0
    poll_seconds: float = 300.0


@dataclass(frozen=True)
class CloseBypassPolicy:
    reviewer_interval: int = 5


@dataclass(frozen=True)
class DifficultyPolicy:
    easy_max_retries: int = 2


@dataclass(frozen=True)
class PromptNotesPolicy:
    worker: str = ""
    reviewer: str = ""
    verification: str = ""
    branching: str = ""


@dataclass(frozen=True)
class Policy:
    stuck_recovery: StuckRecoveryPolicy = field(default_factory=StuckRecoveryPolicy)
    branching: BranchingPolicy = field(default_factory=BranchingPolicy)
    timing: TimingPolicy = field(default_factory=TimingPolicy)
    codex_budget_pause: CodexBudgetPausePolicy = field(default_factory=CodexBudgetPausePolicy)
    close_bypass: CloseBypassPolicy = field(default_factory=CloseBypassPolicy)
    prompt_notes: PromptNotesPolicy = field(default_factory=PromptNotesPolicy)
    difficulty: DifficultyPolicy = field(default_factory=DifficultyPolicy)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

class ConfigError(RuntimeError):
    """Raised for configuration errors."""
    pass


PHASES: Tuple[str, ...] = (
    "paper_check",
    "planning",
    "theorem_stating",
    "proof_formalization",
    "proof_complete_style_cleanup",
)

FORBIDDEN_KEYWORDS_DEFAULT: Tuple[str, ...] = (
    "sorry",
    "axiom",
    "constant",
    "unsafe",
    "opaque",
    "partial",
    "native_decide",
    "implementedBy",
    "implemented_by",
    "extern",
    "elab",
    "macro",
    "syntax",
    "run_cmd",
    "#eval",
)


def _require(raw: Dict[str, Any], key: str, label: str) -> Any:
    if key not in raw:
        raise ConfigError(f"{label} missing required key {key!r}")
    return raw[key]


def _coerce_int(value: Any, label: str, *, minimum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be an integer, got {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{label} must be >= {minimum}, got {parsed}")
    return parsed


def _coerce_float(value: Any, label: str, *, minimum: Optional[float] = None, strictly_positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be numeric, got {value!r}") from exc
    if strictly_positive and parsed <= 0:
        raise ConfigError(f"{label} must be positive, got {parsed}")
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{label} must be >= {minimum}, got {parsed}")
    return parsed


def _sanitize_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-") or "unnamed"


def _sanitize_tmux_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_") or "session"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _parse_provider_config(raw: Any, label: str) -> ProviderConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a dict")
    provider = str(raw.get("provider", "")).strip().lower()
    if provider not in ("claude", "codex", "gemini"):
        raise ConfigError(f"{label}.provider must be claude, codex, or gemini, got {provider!r}")
    return ProviderConfig(
        provider=provider,
        model=raw.get("model") or None,
        effort=raw.get("effort") or None,
        extra_args=list(raw.get("extra_args", [])),
        fallback_models=list(raw.get("fallback_models", [])),
    )


def _parse_verification_config(raw: Any) -> VerificationConfig:
    if not isinstance(raw, dict):
        return VerificationConfig()
    corr_agents_raw = raw.get("correspondence_agents", [])
    corr_agents: List[CorrespondenceAgentConfig] = []
    if isinstance(corr_agents_raw, list):
        for i, agent_raw in enumerate(corr_agents_raw):
            if isinstance(agent_raw, dict):
                provider = str(agent_raw.get("provider", "claude")).strip().lower()
                if provider not in ("claude", "codex", "gemini"):
                    continue
                raw_model = agent_raw.get("model")
                model = str(raw_model).strip() if raw_model is not None else None
                model = model or None  # empty string -> None
                corr_agents.append(CorrespondenceAgentConfig(
                    provider=provider,
                    model=model,
                    effort=agent_raw.get("effort") or None,
                    extra_args=list(agent_raw.get("extra_args", [])),
                    fallback_models=list(agent_raw.get("fallback_models", [])),
                    label=str(agent_raw.get("label", f"{provider}/{model or 'auto'}")),
                ))
    # Soundness agents (same format as correspondence)
    sound_agents_raw = raw.get("soundness_agents", [])
    sound_agents: List[CorrespondenceAgentConfig] = []
    if isinstance(sound_agents_raw, list):
        for i, agent_raw in enumerate(sound_agents_raw):
            if isinstance(agent_raw, dict):
                provider = str(agent_raw.get("provider", "claude")).strip().lower()
                if provider not in ("claude", "codex", "gemini"):
                    continue
                raw_model = agent_raw.get("model")
                model = str(raw_model).strip() if raw_model is not None else None
                model = model or None
                sound_agents.append(CorrespondenceAgentConfig(
                    provider=provider,
                    model=model,
                    effort=agent_raw.get("effort") or None,
                    extra_args=list(agent_raw.get("extra_args", [])),
                    fallback_models=list(agent_raw.get("fallback_models", [])),
                    label=str(agent_raw.get("label", f"{provider}/{model or 'auto'}")),
                ))

    return VerificationConfig(
        provider=str(raw.get("provider", "claude")).strip().lower(),
        model=str(raw.get("model", "claude-opus-4-6")).strip(),
        extra_args=list(raw.get("extra_args", [])),
        thinking_budget=str(raw.get("thinking_budget", "high")).strip(),
        max_context_tokens=_coerce_int(raw.get("max_context_tokens", 50000), "verification.max_context_tokens", minimum=1000),
        correspondence_agents=corr_agents,
        soundness_agents=sound_agents,
    )


def load_config(path: Path) -> Config:
    """Load and validate a config JSON file."""
    path = path.resolve()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file must contain a JSON object: {path}")

    # repo_path
    repo_path = Path(_require(raw, "repo_path", "config")).resolve()
    if not repo_path.is_dir():
        raise ConfigError(f"repo_path does not exist or is not a directory: {repo_path}")

    # goal_file
    goal_file_raw = str(raw.get("goal_file", "GOAL.md"))
    goal_file = (repo_path / goal_file_raw).resolve() if not Path(goal_file_raw).is_absolute() else Path(goal_file_raw).resolve()

    # state_dir
    state_dir_raw = str(raw.get("state_dir", ".agent-supervisor"))
    state_dir = (repo_path / state_dir_raw).resolve() if not Path(state_dir_raw).is_absolute() else Path(state_dir_raw).resolve()

    # providers
    worker = _parse_provider_config(_require(raw, "worker", "config"), "config.worker")
    reviewer = _parse_provider_config(_require(raw, "reviewer", "config"), "config.reviewer")
    verification = _parse_verification_config(raw.get("verification", {}))
    easy_worker = _parse_provider_config(raw["easy_worker"], "config.easy_worker") if "easy_worker" in raw else None
    hard_worker = _parse_provider_config(raw["hard_worker"], "config.hard_worker") if "hard_worker" in raw else None

    # tmux
    tmux_raw = _require(raw, "tmux", "config")
    if not isinstance(tmux_raw, dict):
        raise ConfigError("config.tmux must be a dict")
    burst_user = tmux_raw.get("burst_user")
    if not burst_user:
        raise ConfigError("config.tmux.burst_user is required (multi-user mode is mandatory)")
    tmux = TmuxConfig(
        session_name=_sanitize_tmux_name(str(tmux_raw.get("session_name", "lagent-tablets"))),
        dashboard_window_name=str(tmux_raw.get("dashboard_window_name", "dashboard")),
        kill_windows_after_capture=bool(tmux_raw.get("kill_windows_after_capture", True)),
        burst_user=str(burst_user),
        burst_group=tmux_raw.get("burst_group") or None,
        burst_home=Path(tmux_raw["burst_home"]) if tmux_raw.get("burst_home") else None,
    )

    # workflow
    wf_raw = raw.get("workflow", {})
    if not isinstance(wf_raw, dict):
        raise ConfigError("config.workflow must be a dict")
    start_phase = str(wf_raw.get("start_phase", "paper_check")).strip().lower()
    if start_phase not in PHASES:
        raise ConfigError(f"config.workflow.start_phase must be one of {list(PHASES)}, got {start_phase!r}")
    paper_tex = wf_raw.get("paper_tex_path")
    if paper_tex:
        paper_tex_path: Optional[Path] = (repo_path / paper_tex).resolve()
    else:
        paper_tex_path = None
    # Phase overrides
    phase_overrides_raw = wf_raw.get("phase_overrides", {})
    phase_overrides: Dict[str, PhaseOverride] = {}
    for phase_name, overrides in phase_overrides_raw.items():
        if isinstance(overrides, dict):
            phase_overrides[phase_name] = PhaseOverride(
                worker_model=overrides.get("worker_model"),
                reviewer_model=overrides.get("reviewer_model"),
            )

    workflow = WorkflowConfig(
        start_phase=start_phase,
        paper_tex_path=paper_tex_path,
        approved_axioms_path=(repo_path / str(wf_raw.get("approved_axioms_path", "APPROVED_AXIOMS.json"))).resolve(),
        allowed_import_prefixes=list(wf_raw.get("allowed_import_prefixes", ["Mathlib"])),
        forbidden_keyword_allowlist=list(wf_raw.get("forbidden_keyword_allowlist", [])),
        human_input_path=(repo_path / str(wf_raw.get("human_input_path", "HUMAN_INPUT.md"))).resolve(),
        input_request_path=(repo_path / str(wf_raw.get("input_request_path", "INPUT_REQUEST.md"))).resolve(),
        phase_overrides=phase_overrides,
    )

    # chat
    chat_raw = raw.get("chat", {})
    if not isinstance(chat_raw, dict):
        raise ConfigError("config.chat must be a dict")
    chat = ChatConfig(
        root_dir=Path(str(chat_raw.get("root_dir", Path.home() / "lagent-chats"))).resolve(),
        repo_name=_sanitize_name(str(chat_raw.get("repo_name", repo_path.name))),
        project_name=str(chat_raw.get("project_name", "") or chat_raw.get("repo_name", repo_path.name)),
        public_base_url=str(chat_raw.get("public_base_url", "https://example.com/lagent-chats/")),
    )

    # git
    git_raw = raw.get("git", {})
    if not isinstance(git_raw, dict):
        raise ConfigError("config.git must be a dict")
    git = GitConfig(
        remote_url=git_raw.get("remote_url") or None,
        remote_name=str(git_raw.get("remote_name", "origin")),
        branch=str(git_raw.get("branch", "main")),
        author_name=str(git_raw.get("author_name", "lagent-supervisor")),
        author_email=str(git_raw.get("author_email", "lagent@localhost")),
    )

    # branching
    br_raw = raw.get("branching", {})
    if not isinstance(br_raw, dict):
        raise ConfigError("config.branching must be a dict")
    branching = BranchingConfig(
        max_current_branches=_coerce_int(br_raw.get("max_current_branches", 2), "branching.max_current_branches", minimum=1),
        evaluation_cycle_budget=_coerce_int(br_raw.get("evaluation_cycle_budget", 20), "branching.evaluation_cycle_budget", minimum=1),
        poll_seconds=_coerce_float(br_raw.get("poll_seconds", 300.0), "branching.poll_seconds", strictly_positive=True),
    )

    # policy_path
    policy_path_raw = raw.get("policy_path")
    if policy_path_raw:
        policy_path: Optional[Path] = Path(policy_path_raw).resolve() if Path(policy_path_raw).is_absolute() else (path.parent / policy_path_raw).resolve()
    else:
        policy_path = path.with_suffix(".policy.json").resolve()

    return Config(
        repo_path=repo_path,
        goal_file=goal_file,
        state_dir=state_dir,
        worker=worker,
        reviewer=reviewer,
        verification=verification,
        tmux=tmux,
        workflow=workflow,
        chat=chat,
        git=git,
        max_cycles=_coerce_int(raw.get("max_cycles", 0), "max_cycles", minimum=0),
        sleep_seconds=_coerce_float(raw.get("sleep_seconds", 1.0), "sleep_seconds", minimum=0.0),
        startup_timeout_seconds=_coerce_float(raw.get("startup_timeout_seconds", 120.0), "startup_timeout_seconds", strictly_positive=True),
        burst_timeout_seconds=_coerce_float(raw.get("burst_timeout_seconds", 600.0), "burst_timeout_seconds", strictly_positive=True),
        branching=branching,
        easy_worker=easy_worker,
        hard_worker=hard_worker,
        policy_path=policy_path,
        source_path=path,
    )


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------

def _parse_policy(raw: Any, defaults: Policy, *, path: Path) -> Policy:
    """Parse a raw dict into a Policy, filling missing fields from defaults."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Policy file must contain a JSON object: {path}")

    def _block(key: str) -> Dict[str, Any]:
        val = raw.get(key, {})
        if not isinstance(val, dict):
            raise ConfigError(f"Policy field {key} must be a dict: {path}")
        return val

    sr = _block("stuck_recovery")
    br = _block("branching")
    tm = _block("timing")
    cb = _block("codex_budget_pause")
    cl = _block("close_bypass")
    pn = _block("prompt_notes")
    df = _block("difficulty")

    retry_raw = tm.get("agent_retry_delays_seconds", list(defaults.timing.agent_retry_delays_seconds))
    if not isinstance(retry_raw, list):
        raise ConfigError(f"timing.agent_retry_delays_seconds must be a list: {path}")
    retry_delays = tuple(_coerce_float(d, "timing.agent_retry_delays_seconds[]", strictly_positive=True) for d in retry_raw)

    recheck_raw = br.get("selection_recheck_increments_reviews", list(defaults.branching.selection_recheck_increments_reviews))
    if not isinstance(recheck_raw, list):
        raise ConfigError(f"branching.selection_recheck_increments_reviews must be a list: {path}")
    recheck = tuple(_coerce_int(v, "branching.selection_recheck_increments_reviews[]", minimum=1) for v in recheck_raw)
    if not recheck:
        raise ConfigError(f"branching.selection_recheck_increments_reviews must be non-empty: {path}")

    return Policy(
        stuck_recovery=StuckRecoveryPolicy(
            mainline_max_attempts=_coerce_int(sr.get("mainline_max_attempts", defaults.stuck_recovery.mainline_max_attempts), "stuck_recovery.mainline_max_attempts", minimum=1),
            branch_max_attempts=_coerce_int(sr.get("branch_max_attempts", defaults.stuck_recovery.branch_max_attempts), "stuck_recovery.branch_max_attempts", minimum=1),
        ),
        branching=BranchingPolicy(
            evaluation_cycle_budget=_coerce_int(br.get("evaluation_cycle_budget", defaults.branching.evaluation_cycle_budget), "branching.evaluation_cycle_budget", minimum=1),
            poll_seconds=_coerce_float(br.get("poll_seconds", defaults.branching.poll_seconds), "branching.poll_seconds", strictly_positive=True),
            proposal_cooldown_reviews=_coerce_int(br.get("proposal_cooldown_reviews", defaults.branching.proposal_cooldown_reviews), "branching.proposal_cooldown_reviews", minimum=0),
            replacement_min_confidence=_coerce_float(br.get("replacement_min_confidence", defaults.branching.replacement_min_confidence), "branching.replacement_min_confidence", minimum=0.0),
            selection_recheck_increments_reviews=recheck,
        ),
        timing=TimingPolicy(
            sleep_seconds=_coerce_float(tm.get("sleep_seconds", defaults.timing.sleep_seconds), "timing.sleep_seconds", minimum=0.0),
            agent_retry_delays_seconds=retry_delays,
            budget_error_max_retries=_coerce_int(tm.get("budget_error_max_retries", defaults.timing.budget_error_max_retries), "timing.budget_error_max_retries", minimum=1),
            subprocess_timeout_seconds=_coerce_float(tm.get("subprocess_timeout_seconds", defaults.timing.subprocess_timeout_seconds), "timing.subprocess_timeout_seconds", strictly_positive=True),
            burst_timeout_seconds=_coerce_float(tm.get("burst_timeout_seconds", defaults.timing.burst_timeout_seconds), "timing.burst_timeout_seconds", strictly_positive=True),
            stall_threshold_seconds=_coerce_float(tm.get("stall_threshold_seconds", defaults.timing.stall_threshold_seconds), "timing.stall_threshold_seconds", strictly_positive=True),
            max_stall_recoveries_per_burst=_coerce_int(tm.get("max_stall_recoveries_per_burst", defaults.timing.max_stall_recoveries_per_burst), "timing.max_stall_recoveries_per_burst", minimum=0),
        ),
        codex_budget_pause=CodexBudgetPausePolicy(
            weekly_percent_left_threshold=_coerce_float(cb.get("weekly_percent_left_threshold", defaults.codex_budget_pause.weekly_percent_left_threshold), "codex_budget_pause.weekly_percent_left_threshold", minimum=0.0),
            poll_seconds=_coerce_float(cb.get("poll_seconds", defaults.codex_budget_pause.poll_seconds), "codex_budget_pause.poll_seconds", strictly_positive=True),
        ),
        close_bypass=CloseBypassPolicy(
            reviewer_interval=_coerce_int(cl.get("reviewer_interval", defaults.close_bypass.reviewer_interval), "close_bypass.reviewer_interval", minimum=1),
        ),
        prompt_notes=PromptNotesPolicy(
            worker=str(pn.get("worker", defaults.prompt_notes.worker)).strip(),
            reviewer=str(pn.get("reviewer", defaults.prompt_notes.reviewer)).strip(),
            verification=str(pn.get("verification", defaults.prompt_notes.verification)).strip(),
            branching=str(pn.get("branching", defaults.prompt_notes.branching)).strip(),
        ),
        difficulty=DifficultyPolicy(
            easy_max_retries=_coerce_int(df.get("easy_max_retries", defaults.difficulty.easy_max_retries), "difficulty.easy_max_retries", minimum=1),
        ),
    )


def policy_to_dict(policy: Policy) -> Dict[str, Any]:
    """Serialize a Policy to a plain dict for JSON persistence."""
    return {
        "stuck_recovery": {"mainline_max_attempts": policy.stuck_recovery.mainline_max_attempts, "branch_max_attempts": policy.stuck_recovery.branch_max_attempts},
        "branching": {"evaluation_cycle_budget": policy.branching.evaluation_cycle_budget, "poll_seconds": policy.branching.poll_seconds, "proposal_cooldown_reviews": policy.branching.proposal_cooldown_reviews, "replacement_min_confidence": policy.branching.replacement_min_confidence, "selection_recheck_increments_reviews": list(policy.branching.selection_recheck_increments_reviews)},
        "timing": {"sleep_seconds": policy.timing.sleep_seconds, "agent_retry_delays_seconds": list(policy.timing.agent_retry_delays_seconds), "budget_error_max_retries": policy.timing.budget_error_max_retries, "subprocess_timeout_seconds": policy.timing.subprocess_timeout_seconds, "burst_timeout_seconds": policy.timing.burst_timeout_seconds, "stall_threshold_seconds": policy.timing.stall_threshold_seconds, "max_stall_recoveries_per_burst": policy.timing.max_stall_recoveries_per_burst},
        "codex_budget_pause": {"weekly_percent_left_threshold": policy.codex_budget_pause.weekly_percent_left_threshold, "poll_seconds": policy.codex_budget_pause.poll_seconds},
        "close_bypass": {"reviewer_interval": policy.close_bypass.reviewer_interval},
        "prompt_notes": {"worker": policy.prompt_notes.worker, "reviewer": policy.prompt_notes.reviewer, "verification": policy.prompt_notes.verification, "branching": policy.prompt_notes.branching},
        "difficulty": {"easy_max_retries": policy.difficulty.easy_max_retries},
    }


class PolicyManager:
    """Manages hot-reloading of the policy file."""

    def __init__(self, config: Config):
        self.config = config
        self.path = (config.policy_path or config.state_dir / "policy.json").resolve()
        self.defaults = Policy()
        self._policy: Optional[Policy] = None
        self._mtime_ns: Optional[int] = None
        self._digest: Optional[str] = None

    def current(self) -> Policy:
        return self.reload()

    def reload(self, *, force: bool = False) -> Policy:
        """Reload policy from disk if changed. Create default file if missing."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(
                json.dumps(policy_to_dict(self.defaults), indent=2) + "\n",
                encoding="utf-8",
            )

        stat = self.path.stat()
        if not force and self._policy is not None and self._mtime_ns == stat.st_mtime_ns:
            return self._policy

        try:
            raw_text = self.path.read_text(encoding="utf-8")
            raw = json.loads(raw_text)
            policy = _parse_policy(raw, self.defaults, path=self.path)
            self._mtime_ns = stat.st_mtime_ns
            self._digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]
            self._policy = policy
            return policy
        except (ConfigError, json.JSONDecodeError) as exc:
            if self._policy is None:
                raise ConfigError(f"Could not load policy {self.path}: {exc}") from exc
            print(f"WARNING: Could not reload policy {self.path}: {exc}. Keeping last known good.")
            return self._policy


# ---------------------------------------------------------------------------
# Config hot-reload support
# ---------------------------------------------------------------------------

class ConfigManager:
    """Manages hot-reloading of the config file."""

    def __init__(self, config: Config):
        self._config = config
        self._path = config.source_path
        self._mtime_ns: Optional[int] = None
        if self._path:
            try:
                self._mtime_ns = self._path.stat().st_mtime_ns
            except OSError:
                pass

    @property
    def config(self) -> Config:
        return self._config

    def check_reload(self) -> bool:
        """Check if config file changed. Returns True if reloaded."""
        if not self._path or not self._path.exists():
            return False
        try:
            stat = self._path.stat()
        except OSError:
            return False
        if self._mtime_ns == stat.st_mtime_ns:
            return False
        try:
            new_config = load_config(self._path)
            self._config = new_config
            self._mtime_ns = stat.st_mtime_ns
            return True
        except ConfigError as exc:
            print(f"WARNING: Could not reload config {self._path}: {exc}. Keeping last known good.")
            return False
