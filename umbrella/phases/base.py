from dataclasses import dataclass, field
from typing import Any, Literal, Iterable

from umbrella.contracts import ProofSpec


def _json_ready(value: Any) -> Any:
    if isinstance(value, (frozenset, set, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return value

# ── Prompts ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PromptFiles:
    system: tuple[str, ...]
    user_overlay: tuple[str, ...] = ()
    charter_blocks: tuple[str, ...] = ()

# ── Memory policy ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MemoryAlwaysOnRule:
    store: str
    tier: str

@dataclass(frozen=True)
class MemoryHotRule:
    store: str
    tags: tuple[str, ...]

@dataclass(frozen=True)
class MemoryWarmSearchRule:
    store: str
    n: int = 6
    filter: dict[str, Any] | None = None

@dataclass(frozen=True)
class MemoryGraphPolicy:
    walk_edges: tuple[str, ...]
    hops: int = 1

@dataclass(frozen=True)
class WriteRule:
    store: str
    tier: str
    scope: str
    verified: bool = False

@dataclass(frozen=True)
class MemoryPolicy:
    always_on: tuple[MemoryAlwaysOnRule, ...]
    hot: tuple[MemoryHotRule, ...]
    warm_search: tuple[MemoryWarmSearchRule, ...]
    graph: MemoryGraphPolicy | None = None
    write_rules: dict[str, WriteRule] = field(default_factory=dict)

# ── Permissions ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PermissionRule:
    action: Literal["allow", "deny"]
    tools: tuple[str, ...] | None = None        # None = applies to all tools
    path_patterns: tuple[str, ...] = ()
    cmd_re: str | None = None
    scope_arg: str | None = None

@dataclass(frozen=True)
class PermissionPolicy:
    rules: tuple[PermissionRule, ...]

# ── Budgets ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Budgets:
    max_tokens: int | None = None
    max_seconds: int | None = None
    max_tool_calls: int | None = None

# ── Exit criteria ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RequiredPalaceWrite:
    store: str
    tag: str | None = None
    n: int = 1

@dataclass(frozen=True)
class ExitCriteria:
    required_calls: tuple[str, ...] = ()
    required_prior_calls: tuple[str, ...] = ()
    required_palace_writes: tuple[RequiredPalaceWrite, ...] = ()
    min_palace_writes: tuple[RequiredPalaceWrite, ...] = ()

# ── Phase manifest ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PhaseManifest:
    id: str
    version: int
    description: str
    prompt_files: PromptFiles
    allowed_tools: frozenset[str]
    forbidden_tools: frozenset[str]
    allowed_skills: frozenset[str]
    memory: MemoryPolicy
    permissions: PermissionPolicy
    exit_criteria: ExitCriteria
    mini_review_after: str | None
    budgets: Budgets
    temp_tools_allowed: bool = False

    def to_payload(self) -> dict[str, Any]:
        import dataclasses
        return _json_ready(dataclasses.asdict(self))  # type: ignore[return-value]

# ── Success test ──────────────────────────────────────────────────────────
# ── Subtask card ──────────────────────────────────────────────────────────
@dataclass
class SubtaskCard:
    id: str
    title: str
    goal: str
    allowed_tools: frozenset[str]
    allowed_skills: frozenset[str]
    proof: ProofSpec | None = None
    codeptr_refs: list[str] = field(default_factory=list)
    mcp_refs: list[str] = field(default_factory=list)
    files_to_create: list[str] = field(default_factory=list)
    files_to_change: list[str] = field(default_factory=list)
    files_affected: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    status: Literal["pending", "running", "done", "failed"] = "pending"
    review_verdict: Literal["ok", "revise", "abort"] | None = None
    completion: dict[str, Any] | None = None

# ── Plan edit ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PlanEdit:
    timestamp: float
    actor: str
    patch: dict[str, Any]

# ── Phase node ────────────────────────────────────────────────────────────
@dataclass
class PhaseNode:
    id: str
    manifest_id: str
    status: Literal["pending", "running", "done", "skipped", "failed"] = "pending"
    subtasks: list[SubtaskCard] | None = None
    overlay: dict[str, Any] | None = None
    started_at: float | None = None
    ended_at: float | None = None
    parent_phase_id: str | None = None

# ── Phase plan ────────────────────────────────────────────────────────────
@dataclass
class PhasePlan:
    plan_id: str
    workspace_id: str
    run_id: str
    nodes: list[PhaseNode]
    version: int = 0
    edits_log: list[PlanEdit] = field(default_factory=list)

    def next_pending(self) -> PhaseNode | None:
        return next((n for n in self.nodes if n.status == "pending"), None)

    def get_node(self, node_id: str) -> PhaseNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def mutate(self, patch: dict[str, Any], actor: str = "agent") -> None:
        import time
        self.edits_log.append(PlanEdit(timestamp=time.time(), actor=actor, patch=patch))
        self.version += 1
        for key, val in patch.items():
            if hasattr(self, key):
                setattr(self, key, val)

# ── Phase result ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PhaseResult:
    phase_id: str
    outcome: Literal["done", "failed", "loop_back", "skipped"]
    loop_back_target: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

# ── Watcher signal ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WatcherSignal:
    signal_id: str
    created_at: float
    kind: Literal["abort_phase", "restart_phase", "mutate_phase_plan", "force_verify", "inject_lesson", "ok"]
    reason: str
    trigger: str
    payload: dict[str, Any] | None = None
