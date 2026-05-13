"""
Core data models for the unified workspace runtime.

This module defines the stable runtime contract that works across multiple workspaces,
providing structured run records and artifact references.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4


class WorkspaceRunStatus(str, Enum):
    """Status of a workspace run."""

    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class WorkspaceLifecycleStage(str, Enum):
    """Standard lifecycle stages for workspace execution."""

    PREPARE = "prepare"
    RETRIEVE_CONTEXT = "retrieve_context"
    RUN = "run"
    INSPECT = "inspect"
    PROPOSE_PATCH = "propose_patch"
    APPLY_PATCH = "apply_patch"
    RE_RUN = "re_run"
    EVALUATE = "evaluate"
    SNAPSHOT = "snapshot"
    PROMOTE_OR_ARCHIVE = "promote_or_archive"


class ArtifactType(str, Enum):
    """Types of artifacts produced by workspace runs."""

    LOG = "log"
    REPORT = "report"
    SNAPSHOT = "snapshot"
    EVALUATION_RESULT = "evaluation_result"
    PATCH_NOTE = "patch_note"
    RUN_MANIFEST = "run_manifest"
    ARTIFACT_MANIFEST = "artifact_manifest"
    LOG_SUMMARY = "log_summary"
    GRAPH_SNAPSHOT = "graph_snapshot"
    MEMORY_DUMP = "memory_dump"
    CUSTOM = "custom"


@dataclass(frozen=True)
class ArtifactRef:
    """
    Reference to an artifact produced by a workspace run.

    Artifacts are stable references that the manager can inspect
    without needing to read raw logs.
    """

    artifact_id: str
    artifact_type: ArtifactType
    path: Path
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        """Check if the artifact file exists."""
        return self.path.exists()


@dataclass(frozen=True)
class RunManifestRef:
    """
    Reference to a run manifest.

    The run manifest is the primary structured output of a workspace run,
    containing metadata, metrics, and artifact references.
    """

    run_id: str
    path: Path
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def exists(self) -> bool:
        """Check if the manifest file exists."""
        return self.path.exists()


@dataclass(frozen=True)
class LogSummaryRef:
    """
    Reference to a log summary.

    Log summaries are structured extracts from raw logs,
    designed for manager consumption without full log parsing.
    """

    run_id: str
    path: Path
    line_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def exists(self) -> bool:
        """Check if the summary file exists."""
        return self.path.exists()


@dataclass
class WorkspaceRunRequest:
    """
    Request to run a workspace.

    Contains all parameters needed to execute a workspace run.
    """

    # Task identification
    task_id: str | None = None
    query: str = ""

    # Run configuration
    live: bool = False
    mock_loops: bool = False
    max_agent_executions: int = 32

    # LLM configuration (for live runs)
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens: int = 1600
    tool_choice: str = "auto"

    # Output configuration
    report_name: str = "latest_report.md"
    idea_report_name: str = "latest_idea.md"

    # Checkpoint/resume
    resume_from_checkpoint: str | None = None

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceRunResult:
    """
    Stable run result model containing all outputs from a workspace run.

    This is the primary output of the workspace runtime, designed to be
    machine-readable for manager consumption.
    """

    # Identification
    run_id: str = field(
        default_factory=lambda: (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid4().hex[:8]}"
        )
    )
    workspace_id: str = ""
    task_id: str | None = None

    # Status
    status: WorkspaceRunStatus = WorkspaceRunStatus.PENDING

    # Timing
    start_timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    end_timestamp: datetime | None = None

    # Duration in seconds
    duration_seconds: float = 0.0

    # Artifacts
    artifacts: list[ArtifactRef] = field(default_factory=list)
    run_manifest_path: Path | None = None
    artifact_manifest_path: Path | None = None
    log_summary_path: Path | None = None

    # Results
    final_agent_id: str | None = None
    final_answer: str = ""
    summary: str = ""

    # Metrics
    total_tokens: int = 0
    agent_count: int = 0
    error_count: int = 0

    # Errors
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Custom metrics
    metrics: dict[str, Any] = field(default_factory=dict)

    # Run directory
    run_dir: Path | None = None

    def add_artifact(self, artifact: ArtifactRef) -> None:
        """Add an artifact reference to the result."""
        self.artifacts.append(artifact)

    def get_artifacts_by_type(self, artifact_type: ArtifactType) -> list[ArtifactRef]:
        """Get all artifacts of a specific type."""
        return [a for a in self.artifacts if a.artifact_type == artifact_type]

    @property
    def is_successful(self) -> bool:
        """Check if the run completed successfully."""
        return self.status == WorkspaceRunStatus.COMPLETED and not self.errors

    @property
    def duration_str(self) -> str:
        """Human-readable duration."""
        if self.duration_seconds < 60:
            return f"{self.duration_seconds:.1f}s"
        minutes = int(self.duration_seconds // 60)
        seconds = self.duration_seconds % 60
        return f"{minutes}m {seconds:.1f}s"


@dataclass
class WorkspaceInstance:
    """
    A workspace instance ready for execution.

    This represents a prepared workspace that can be run through the lifecycle.
    """

    # Instance identification
    instance_id: str = field(default_factory=lambda: str(uuid4())[:8])
    workspace_id: str = ""
    seed_workspace_id: str | None = None

    # Paths
    path: Path = field(default_factory=lambda: Path("."))
    run_dir: Path = field(default_factory=lambda: Path("runs"))

    # Configuration
    config: dict[str, Any] = field(default_factory=dict)

    # Task contract
    task_main_path: Path | None = None

    # Status
    status: str = "created"
    run_count: int = 0
    last_run_id: str | None = None

    # Creation metadata
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_from_seed: bool = False

    @property
    def runs_path(self) -> Path:
        """Path to the runs directory."""
        return self.path / self.run_dir


@dataclass
class WorkspaceSnapshot:
    """
    A snapshot of a workspace instance at a point in time.

    Snapshots enable checkpointing and rollback.
    """

    snapshot_id: str = field(default_factory=lambda: str(uuid4())[:8])
    instance_id: str = ""
    workspace_id: str = ""

    # Snapshot metadata
    label: str = ""
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Paths
    snapshot_path: Path = field(default_factory=lambda: Path("."))
    source_path: Path = field(default_factory=lambda: Path("."))

    # Included content
    includes_graph: bool = False
    includes_memory: bool = False
    includes_prompts: bool = False
    includes_artifacts: bool = False

    # Run reference
    run_id: str | None = None

    @property
    def exists(self) -> bool:
        """Check if the snapshot directory exists."""
        return self.snapshot_path.exists()


@dataclass
class RunReportNode:
    """A node (agent/step) in a workspace run report, suitable for graph display."""

    node_id: str
    display_name: str = ""
    status: str = "idle"
    tokens: int = 0
    duration_ms: float = 0.0
    output_preview: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReportEdge:
    """An edge (transition) between nodes in a workspace run report."""

    source: str
    target: str
    label: str = ""
    executed: bool = False


@dataclass
class RunReportEvent:
    """A generic timestamped event from a workspace run."""

    event_type: str
    timestamp: str = ""
    agent_id: str = ""
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceRunReport:
    """
    Universal run report that any workspace adapter can fill.

    This is the dashboard-facing representation of what happened
    inside a workspace run — agents, graph, events, outputs.
    Works for GMAS, LangGraph, CrewAI, or any custom pipeline.
    """

    run_id: str = ""
    workspace_id: str = ""
    workspace_type: str = ""
    status: str = "unknown"
    query: str = ""

    duration_seconds: float = 0.0
    total_tokens: int = 0

    nodes: list[RunReportNode] = field(default_factory=list)
    edges: list[RunReportEdge] = field(default_factory=list)
    events: list[RunReportEvent] = field(default_factory=list)

    artifacts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: str = ""
    final_answer: str = ""

    layout_hint: str = "left-to-right"

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass
class WorkspaceCheckpoint:
    """
    A checkpoint for resuming workspace execution.

    Checkpoints enable the control plane to resume after interruption.
    """

    checkpoint_id: str = field(default_factory=lambda: str(uuid4())[:8])
    instance_id: str = ""
    run_id: str = ""

    # Checkpoint state
    stage: WorkspaceLifecycleStage = WorkspaceLifecycleStage.PREPARE
    stage_progress: float = 0.0

    # Paths
    checkpoint_path: Path = field(default_factory=lambda: Path("."))

    # State
    state: dict[str, Any] = field(default_factory=dict)

    # Timing
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def exists(self) -> bool:
        """Check if the checkpoint file exists."""
        return self.checkpoint_path.exists()


@dataclass
class WorkspaceInspection:
    """
    Result of inspecting a workspace run.

    Provides structured access to run state without reading raw logs.
    """

    run_id: str = ""
    workspace_id: str = ""

    # Status
    status: WorkspaceRunStatus = WorkspaceRunStatus.PENDING

    # Execution summary
    agents_executed: list[str] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)

    # Key outputs
    final_answer: str = ""
    key_artifacts: list[ArtifactRef] = field(default_factory=list)

    # Issues
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked_reason: str | None = None

    # Metrics
    total_tokens: int = 0
    duration_seconds: float = 0.0

    # Recommendations
    recommendations: list[str] = field(default_factory=list)


@dataclass
class PreparedWorkspace:
    """
    A workspace that has been prepared for execution.

    This is the result of the prepare lifecycle stage.
    """

    instance: WorkspaceInstance
    config_valid: bool = True
    validation_issues: list[str] = field(default_factory=list)

    # Prepared resources
    graph_path: Path | None = None
    profiles_loaded: bool = False
    tools_registered: bool = False

    # Ready status
    ready: bool = False
    not_ready_reason: str | None = None
