"""
Data models for the workspace registry.

Defines types for:
- Workspace references and configurations
- Seed workspace profiles with capability tags
- Task instance profiles with lineage
- Validation and selection types
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4


class WorkspaceType(str, Enum):
    """Types of workspaces in the registry."""

    SEED = "seed"  # Human-created stable template
    INSTANCE = "instance"  # Task-specific derivative
    UNKNOWN = "unknown"  # Unclassified


class WorkspaceMaturity(str, Enum):
    """Maturity levels for workspaces."""

    EXPERIMENTAL = "experimental"  # Early stage, may change significantly
    DEVELOPING = "developing"  # Active development, not yet stable
    STABLE = "stable"  # Well-tested, reliable for production use
    DEPRECATED = "deprecated"  # No longer recommended for new tasks


class ValidationSeverity(str, Enum):
    """Severity levels for validation issues."""

    ERROR = "error"  # Must be fixed before workspace is usable
    WARNING = "warning"  # Should be addressed but not blocking
    INFO = "info"  # Informational note


@dataclass(frozen=True)
class RegistryManifest:
    """Contents of workspaces/registry.toml (declared seeds and instances)."""

    version: str
    seeds: tuple[str, ...]
    instances: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceCapability:
    """A capability tag for workspace selection."""

    name: str
    description: str = ""
    weight: float = 1.0

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class WorkspaceSelectionHint:
    """Hints for workspace selection matching."""

    task_classes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    preferred_for_domains: list[str] = field(default_factory=list)
    avoided_for_domains: list[str] = field(default_factory=list)


@dataclass
class WorkspaceRef:
    """
    Reference to a workspace with its configuration.

    This is the minimal contract for a workspace,
    parsed from workspace.toml.
    """

    workspace_id: str
    name: str
    description: str
    path: Path

    # File references
    task_main_file: str = "TASK_MAIN.md"
    graph_file: str | None = None
    agents_dir: str | None = None
    prompts_dir: str | None = None
    tools_allowlist_file: str | None = None
    models_file: str | None = None
    policies_file: str | None = None

    # Directory references
    evals_dir: str = "evals"
    experiments_dir: str = "experiments"
    runs_dir: str = "runs"
    snapshots_dir: str = "snapshots"
    reports_dir: str = "reports"

    # Mutable paths (relative to workspace root)
    mutable_paths: list[str] = field(default_factory=list)

    # Metadata
    engine: str = "gmas"
    engine_mutable: bool = False
    owner: str = "manual"
    notes: str = ""

    @property
    def task_main_path(self) -> Path:
        """Full path to TASK_MAIN.md."""
        return self.path / self.task_main_file

    def has_task_main(self) -> bool:
        """Check if TASK_MAIN.md exists."""
        return self.task_main_path.exists()


@dataclass
class SeedWorkspaceProfile:
    """
    Profile for a seed workspace with capability metadata.

    Seed workspaces are human-created templates that serve as
    starting points for task-specific instances.
    """

    ref: WorkspaceRef

    # Type classification
    workspace_type: WorkspaceType = WorkspaceType.SEED

    # Maturity level
    maturity: WorkspaceMaturity = WorkspaceMaturity.EXPERIMENTAL

    # Capabilities
    capabilities: list[WorkspaceCapability] = field(default_factory=list)

    # Selection hints
    selection_hints: WorkspaceSelectionHint = field(
        default_factory=WorkspaceSelectionHint
    )

    # Task classes this workspace is designed for
    primary_task_classes: list[str] = field(default_factory=list)

    # Allowed mutation surfaces (what can be changed in instances)
    allowed_mutation_surfaces: list[str] = field(
        default_factory=lambda: [
            "graph",
            "agents",
            "prompts",
            "tools",
            "models",
            "evals",
            "experiments",
            "reports",
            "runs",
            "snapshots",
        ]
    )

    # Required tools that must be available
    required_tools: list[str] = field(default_factory=list)

    # Evaluation hooks (module:function references)
    eval_hooks: list[str] = field(default_factory=list)

    # Human dependency level (how much human oversight is needed)
    human_dependency_level: str = "medium"  # low, medium, high

    # Quality metrics
    successful_runs: int = 0
    total_runs: int = 0

    @property
    def success_rate(self) -> float:
        """Calculate success rate from run history."""
        if self.total_runs == 0:
            return 0.0
        return self.successful_runs / self.total_runs

    @property
    def workspace_id(self) -> str:
        """Convenience accessor for workspace ID."""
        return self.ref.workspace_id

    @property
    def path(self) -> Path:
        """Convenience accessor for workspace path."""
        return self.ref.path


@dataclass
class WorkspaceLineageRecord:
    """
    Lineage record tracking the origin and history of a workspace.

    This enables tracing task instances back to their seeds
    and understanding the evolution of workspaces.
    """

    # Unique identifier for this lineage record
    lineage_id: str = field(default_factory=lambda: str(uuid4())[:8])

    # Seed workspace this instance was created from
    seed_workspace_id: str = ""

    # Parent task instance (if this is a derivative of another instance)
    parent_instance_id: str | None = None

    # Creation metadata
    creation_timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    creation_reason: str = ""

    # Task reference
    task_id: str | None = None
    task_brief_summary: str = ""

    # Promotion eligibility
    promotion_eligible: bool = False
    promotion_candidate: bool = False
    promotion_score: float = 0.0

    # Modification history (list of modification summaries)
    modifications: list[dict[str, Any]] = field(default_factory=list)

    # Evaluation history
    evaluation_results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TaskInstanceProfile:
    """
    Profile for a task-specific workspace instance.

    Task instances are derived from seed workspaces and are
    the primary mutable problem-solving units.
    """

    ref: WorkspaceRef

    # Type classification
    workspace_type: WorkspaceType = WorkspaceType.INSTANCE

    # Lineage information
    lineage: WorkspaceLineageRecord = field(default_factory=WorkspaceLineageRecord)

    # Reference to the seed profile this was derived from
    seed_profile: SeedWorkspaceProfile | None = None

    # Task-specific metadata
    task_brief: str = ""
    task_class: str = ""

    # Current status
    status: str = "created"  # created, running, completed, failed, archived

    # Run count for this instance
    run_count: int = 0

    @property
    def workspace_id(self) -> str:
        """Convenience accessor for workspace ID."""
        return self.ref.workspace_id

    @property
    def path(self) -> Path:
        """Convenience accessor for workspace path."""
        return self.ref.path

    @property
    def is_promotion_candidate(self) -> bool:
        """Check if this instance is a candidate for promotion to seed."""
        return self.lineage.promotion_candidate


@dataclass
class TaskBrief:
    """
    A structured task description for workspace selection.

    This is the input to the workspace selection system.
    """

    # Main task description
    description: str

    # Optional task ID for tracking
    task_id: str | None = None

    # Task class hint (e.g., "article_writing", "code_generation")
    task_class: str | None = None

    # Domain hints
    domains: list[str] = field(default_factory=list)

    # Required capabilities
    required_capabilities: list[str] = field(default_factory=list)

    # Preferred workspace ID (if any)
    preferred_workspace_id: str | None = None

    # Constraints
    constraints: dict[str, Any] = field(default_factory=dict)

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceMatch:
    """
    Result of matching a task brief to a workspace.

    Includes scoring information for selection decisions.
    """

    profile: SeedWorkspaceProfile
    score: float
    matched_capabilities: list[str] = field(default_factory=list)
    matched_task_classes: list[str] = field(default_factory=list)
    match_reasons: list[str] = field(default_factory=list)

    @property
    def workspace_id(self) -> str:
        """Convenience accessor for workspace ID."""
        return self.profile.workspace_id


@dataclass
class ValidationIssue:
    """
    A validation issue found in a workspace.
    """

    severity: ValidationSeverity
    message: str
    field: str | None = None
    path: Path | None = None
    suggestion: str | None = None

    def __str__(self) -> str:
        parts = [f"[{self.severity.value.upper()}]"]
        if self.field:
            parts.append(f" field={self.field}")
        if self.path:
            parts.append(f" path={self.path}")
        parts.append(f" {self.message}")
        if self.suggestion:
            parts.append(f" Suggestion: {self.suggestion}")
        return "".join(parts)
