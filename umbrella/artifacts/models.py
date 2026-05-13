from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, Enum):
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    FAILED = "failed"
    RUNNING = "running"


class ArtifactCategory(str, Enum):
    REPORT = "report"
    LOG = "log"
    SNAPSHOT = "snapshot"
    SUMMARY = "summary"
    GRAPH = "graph"
    MEMORY = "memory"
    CUSTOM = "custom"


class ErrorSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class ErrorSignature:
    error_id: str
    error_type: str
    severity: ErrorSeverity = ErrorSeverity.ERROR
    message: str = ""
    timestamp: datetime | None = None
    agent_id: str | None = None
    stack_trace: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    raw_line: str = ""

    @property
    def is_critical(self) -> bool:
        return self.severity == ErrorSeverity.CRITICAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_id": self.error_id,
            "error_type": self.error_type,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "agent_id": self.agent_id,
            "stack_trace": self.stack_trace,
            "context": self.context,
            "raw_line": self.raw_line,
        }


@dataclass
class StageTransition:
    stage: str
    timestamp: datetime
    agent_id: str | None = None
    status: str = "started"
    duration_ms: float | None = None
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "timestamp": self.timestamp.isoformat(),
            "agent_id": self.agent_id,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "details": self.details,
        }


@dataclass
class RawLogPointer:
    path: Path
    start_line: int = 0
    end_line: int | None = None
    total_lines: int = 0
    size_bytes: int = 0
    encoding: str = "utf-8"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "start_line": self.start_line,
            "end_line": self.end_line,
            "total_lines": self.total_lines,
            "size_bytes": self.size_bytes,
            "encoding": self.encoding,
        }


@dataclass
class ArtifactMeta:
    artifact_id: str
    name: str
    path: Path
    category: ArtifactCategory
    size_bytes: int = 0
    mime_type: str = "application/octet-stream"
    created_at: datetime | None = None
    description: str = ""
    agent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "path": str(self.path),
            "category": self.category.value,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "description": self.description,
            "agent_id": self.agent_id,
            "metadata": self.metadata,
        }


@dataclass
class RunManifest:
    run_id: str
    workspace_id: str
    task_id: str | None = None
    status: RunStatus = RunStatus.UNKNOWN
    created_at: datetime = field(default_factory=_now_utc)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    execution_order: list[str] = field(default_factory=list)
    agents_executed: list[str] = field(default_factory=list)
    parallel_groups: int = 0
    final_agent_id: str | None = None
    final_answer: str = ""
    summary: str = ""
    result_summary_path: Path | None = None
    events_path: Path | None = None
    notifications_path: Path | None = None
    report_path: Path | None = None
    idea_path: Path | None = None
    total_tokens: int = 0
    error_count: int = 0
    warning_count: int = 0
    artifact_manifest_path: Path | None = None
    log_summary_path: Path | None = None
    run_dir: Path | None = None


@dataclass
class LogSummary:
    run_id: str
    workspace_id: str
    total_events: int = 0
    agent_executions: int = 0
    parallel_groups: int = 0
    topology_changes: int = 0
    memory_reads: int = 0
    memory_writes: int = 0
    stages: list[StageTransition] = field(default_factory=list)
    errors: list[ErrorSignature] = field(default_factory=list)
    warnings: list[ErrorSignature] = field(default_factory=list)
    final_status: RunStatus = RunStatus.UNKNOWN
    final_agent_id: str | None = None
    final_message: str = ""
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_seconds: float = 0.0
    total_tokens: int = 0
    raw_log_pointer: RawLogPointer | None = None

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)


@dataclass
class ArtifactManifest:
    run_id: str
    workspace_id: str
    artifacts: list[ArtifactMeta] = field(default_factory=list)
    reports: list[ArtifactMeta] = field(default_factory=list)
    memory_dumps: list[ArtifactMeta] = field(default_factory=list)
    snapshots: list[ArtifactMeta] = field(default_factory=list)
    logs: list[ArtifactMeta] = field(default_factory=list)
    summaries: list[ArtifactMeta] = field(default_factory=list)
    result_summary: ArtifactMeta | None = None
    events_log: ArtifactMeta | None = None
    main_report: ArtifactMeta | None = None
    total_artifacts: int = 0
    total_size_bytes: int = 0

    def add_artifact(self, meta: ArtifactMeta) -> None:
        self.artifacts.append(meta)
        self.total_artifacts += 1
        self.total_size_bytes += int(meta.size_bytes)
        if meta.category == ArtifactCategory.REPORT:
            self.reports.append(meta)
        elif meta.category == ArtifactCategory.MEMORY:
            self.memory_dumps.append(meta)
        elif meta.category == ArtifactCategory.SNAPSHOT:
            self.snapshots.append(meta)
        elif meta.category == ArtifactCategory.LOG:
            self.logs.append(meta)
        elif meta.category == ArtifactCategory.SUMMARY:
            self.summaries.append(meta)


@dataclass
class WorkspaceRunIndex:
    workspace_id: str
    runs: list[RunManifest] = field(default_factory=list)

    @property
    def total_runs(self) -> int:
        return len(self.runs)

    @property
    def latest_run(self) -> RunManifest | None:
        if not self.runs:
            return None
        return self.runs[0]
