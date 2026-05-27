"""Typed models for compiled phase LLM input."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextSourceRef:
    kind: str
    path: str | None = None
    artifact_id: str | None = None
    memory_id: str | None = None
    tool_event_id: str | None = None
    phase: str | None = None
    run_id: str | None = None
    hash: str | None = None


@dataclass(frozen=True)
class LLMContextItem:
    id: str
    role: str
    title: str
    text: str
    source: ContextSourceRef
    freshness: str = "current_run"
    trust: str = "system"
    include_reason: str | None = None


@dataclass(frozen=True)
class MemorySelection:
    id: str
    kind: str
    tier: str
    trust: str
    scope: str | None
    phase: str | None
    run_id: str | None
    evidence_refs: list[dict[str, Any]]
    selected_reason: str
    freshness: str
    text: str
    surface: str = "supplemental_evidence"
    directive: bool = False


@dataclass(frozen=True)
class ToolContractView:
    allowed_tools: list[str]
    forbidden_tools: list[str]
    required_calls: list[str]
    tool_filter_hash: str


@dataclass(frozen=True)
class CapabilityContractView:
    phase: str
    workspace_write: dict[str, Any]
    shell: dict[str, Any]
    memory_write: dict[str, Any]
    verification: dict[str, Any]
    source: ContextSourceRef
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessContractView:
    schema_version: str
    mode: str
    selected_ids: list[str]
    reason: str
    profiles: list[dict[str, Any]]
    source: ContextSourceRef


@dataclass(frozen=True)
class WorkspaceFileDigest:
    path: str
    exists: bool
    size_bytes: int
    line_count: int
    sha256: str
    kind: str
    symbols: list[str] = field(default_factory=list)
    last_ledger_event_id: str | None = None


@dataclass(frozen=True)
class WorkspaceInventorySnapshot:
    workspace_hash: str
    file_count: int
    source_count: int
    test_count: int
    config_count: int
    active_declared_files: list[WorkspaceFileDigest]
    recently_changed_files: list[WorkspaceFileDigest]
    missing_declared_files: list[str]


@dataclass(frozen=True)
class CurrentPhaseEnvelope:
    goal: str
    active_subtask: str
    allowed_files: tuple[str, ...] = ()
    last_failure: str = ""
    open_issues: tuple[str, ...] = ()
    forbidden_repeats: tuple[str, ...] = ()


@dataclass(frozen=True)
class LLMInputBundle:
    schema_version: str
    run_id: str
    workspace_id: str
    task_id: str
    phase_id: str
    manifest_id: str
    system_sections: list[LLMContextItem] = field(default_factory=list)
    user_sections: list[LLMContextItem] = field(default_factory=list)
    memory_items: list[MemorySelection] = field(default_factory=list)
    authoritative_artifacts: list[LLMContextItem] = field(default_factory=list)
    contract_items: list[LLMContextItem] = field(default_factory=list)
    active_subtask_id: str | None = None
    active_subtask: dict[str, Any] | None = None
    workspace_inventory: WorkspaceInventorySnapshot | None = None
    tool_contract: ToolContractView | None = None
    capability_contract: CapabilityContractView | None = None
    harness_contract: HarnessContractView | None = None
    source_refs: list[ContextSourceRef] = field(default_factory=list)
    input_hash: str = ""
