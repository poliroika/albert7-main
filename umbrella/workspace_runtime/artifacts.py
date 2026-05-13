"""
Runtime artifact collection with compatibility wrappers.

This module keeps the runtime-local artifact store and collector, while
reusing the canonical observability models from ``umbrella.artifacts.models``
so we do not maintain a second independent manifest schema.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from umbrella.artifacts.models import (
    ArtifactCategory,
    ArtifactManifest as CanonicalArtifactManifest,
    ArtifactMeta,
    ErrorSeverity,
    ErrorSignature,
    LogSummary as CanonicalLogSummary,
    RawLogPointer,
    RunManifest as CanonicalRunManifest,
    RunStatus,
    StageTransition,
)
from umbrella.workspace_runtime.models import (
    ArtifactRef,
    ArtifactType,
    WorkspaceRunResult,
)


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _serialize_path(value: Path | None) -> str | None:
    return str(value) if value else None


def _parse_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _run_status_from_workspace_result(result: WorkspaceRunResult) -> RunStatus:
    try:
        return RunStatus(result.status.value)
    except ValueError:
        if result.errors:
            return RunStatus.FAILED
        return RunStatus.UNKNOWN


def _run_status_from_value(value: str | None) -> RunStatus:
    if not value:
        return RunStatus.UNKNOWN
    try:
        return RunStatus(value)
    except ValueError:
        return RunStatus.UNKNOWN


def _serialize_error_signature(signature: ErrorSignature) -> dict[str, Any]:
    return signature.to_dict()


def _parse_error_signature(data: dict[str, Any]) -> ErrorSignature:
    severity = ErrorSeverity(data.get("severity", ErrorSeverity.ERROR.value))
    return ErrorSignature(
        error_id=str(data.get("error_id", "")),
        error_type=str(data.get("error_type", "")),
        severity=severity,
        message=str(data.get("message", "")),
        timestamp=_parse_datetime(data.get("timestamp")),
        agent_id=data.get("agent_id"),
        stack_trace=str(data.get("stack_trace", "")),
        context=dict(data.get("context", {})),
        raw_line=str(data.get("raw_line", "")),
    )


def _serialize_stage_transition(stage: StageTransition) -> dict[str, Any]:
    return stage.to_dict()


def _parse_stage_transition(data: dict[str, Any]) -> StageTransition:
    timestamp = _parse_datetime(data.get("timestamp")) or datetime.now()
    return StageTransition(
        stage=str(data.get("stage", "")),
        timestamp=timestamp,
        agent_id=data.get("agent_id"),
        status=str(data.get("status", "started")),
        duration_ms=data.get("duration_ms"),
        details=str(data.get("details", "")),
    )


def _serialize_raw_log_pointer(
    pointer: RawLogPointer | None,
) -> dict[str, Any] | None:
    return pointer.to_dict() if pointer else None


def _parse_raw_log_pointer(data: dict[str, Any] | None) -> RawLogPointer | None:
    if not data:
        return None
    path = data.get("path")
    if not path:
        return None
    return RawLogPointer(
        path=Path(path),
        start_line=int(data.get("start_line", 0)),
        end_line=data.get("end_line"),
        total_lines=int(data.get("total_lines", 0)),
        size_bytes=int(data.get("size_bytes", 0)),
        encoding=str(data.get("encoding", "utf-8")),
    )


def _serialize_artifact_meta(meta: ArtifactMeta) -> dict[str, Any]:
    return meta.to_dict()


def _parse_artifact_meta(data: dict[str, Any]) -> ArtifactMeta:
    return ArtifactMeta(
        artifact_id=str(data.get("artifact_id", "")),
        name=str(data.get("name", "")),
        path=Path(str(data.get("path", ""))),
        category=ArtifactCategory(
            str(data.get("category", ArtifactCategory.CUSTOM.value))
        ),
        size_bytes=int(data.get("size_bytes", 0)),
        mime_type=str(data.get("mime_type", "application/octet-stream")),
        created_at=_parse_datetime(data.get("created_at")),
        description=str(data.get("description", "")),
        agent_id=data.get("agent_id"),
        metadata=dict(data.get("metadata", {})),
    )


def _artifact_type_to_category(artifact_type: ArtifactType) -> ArtifactCategory:
    mapping = {
        ArtifactType.LOG: ArtifactCategory.LOG,
        ArtifactType.REPORT: ArtifactCategory.REPORT,
        ArtifactType.SNAPSHOT: ArtifactCategory.SNAPSHOT,
        ArtifactType.RUN_MANIFEST: ArtifactCategory.SUMMARY,
        ArtifactType.ARTIFACT_MANIFEST: ArtifactCategory.SUMMARY,
        ArtifactType.LOG_SUMMARY: ArtifactCategory.SUMMARY,
        ArtifactType.GRAPH_SNAPSHOT: ArtifactCategory.GRAPH,
        ArtifactType.MEMORY_DUMP: ArtifactCategory.MEMORY,
        ArtifactType.EVALUATION_RESULT: ArtifactCategory.CUSTOM,
        ArtifactType.PATCH_NOTE: ArtifactCategory.CUSTOM,
        ArtifactType.CUSTOM: ArtifactCategory.CUSTOM,
    }
    return mapping.get(artifact_type, ArtifactCategory.CUSTOM)


def _artifact_ref_to_meta(artifact: ArtifactRef) -> ArtifactMeta:
    return ArtifactMeta(
        artifact_id=artifact.artifact_id,
        name=artifact.path.name,
        path=artifact.path,
        category=_artifact_type_to_category(artifact.artifact_type),
        size_bytes=artifact.size_bytes,
        mime_type=artifact.mime_type,
        created_at=artifact.created_at,
        description=artifact.description,
        metadata=dict(artifact.metadata),
    )


class ArtifactStore:
    """
    Manages artifact collection for a workspace run.

    Provides structured access to run outputs without raw log parsing.
    """

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self._artifacts: dict[str, ArtifactRef] = {}
        self._artifact_counter: dict[ArtifactType, int] = {}

    def add_artifact(self, artifact: ArtifactRef) -> ArtifactRef:
        self._artifacts[artifact.artifact_id] = artifact
        self._artifact_counter[artifact.artifact_type] = (
            self._artifact_counter.get(artifact.artifact_type, 0) + 1
        )
        self._persist_artifact_registry()
        return artifact

    def get_artifact(self, artifact_id: str) -> ArtifactRef | None:
        return self._artifacts.get(artifact_id)

    def get_artifacts_by_type(self, artifact_type: ArtifactType) -> list[ArtifactRef]:
        return [a for a in self._artifacts.values() if a.artifact_type == artifact_type]

    def get_all_artifacts(self) -> list[ArtifactRef]:
        return list(self._artifacts.values())

    def get_artifact_count(self, artifact_type: ArtifactType | None = None) -> int:
        if artifact_type:
            return self._artifact_counter.get(artifact_type, 0)
        return len(self._artifacts)

    def _persist_artifact_registry(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        registry_path = self.run_dir / "artifacts.json"
        registry = {
            "artifacts": {
                artifact_id: {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type.value,
                    "path": str(artifact.path),
                    "mime_type": artifact.mime_type,
                    "size_bytes": artifact.size_bytes,
                    "created_at": artifact.created_at.isoformat(),
                    "description": artifact.description,
                    "metadata": artifact.metadata,
                }
                for artifact_id, artifact in self._artifacts.items()
            },
            "counts": {
                artifact_type.value: count
                for artifact_type, count in self._artifact_counter.items()
            },
        }
        registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    @classmethod
    def load_from_run_dir(cls, run_dir: Path) -> "ArtifactStore":
        store = cls(run_dir)
        registry_path = run_dir / "artifacts.json"
        if not registry_path.exists():
            return store

        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            for artifact_data in data.get("artifacts", {}).values():
                artifact = ArtifactRef(
                    artifact_id=str(artifact_data["artifact_id"]),
                    artifact_type=ArtifactType(str(artifact_data["artifact_type"])),
                    path=Path(str(artifact_data["path"])),
                    mime_type=str(
                        artifact_data.get("mime_type", "application/octet-stream")
                    ),
                    size_bytes=int(artifact_data.get("size_bytes", 0)),
                    created_at=_parse_datetime(artifact_data.get("created_at"))
                    or datetime.now(),
                    description=str(artifact_data.get("description", "")),
                    metadata=dict(artifact_data.get("metadata", {})),
                )
                store._artifacts[artifact.artifact_id] = artifact
                store._artifact_counter[artifact.artifact_type] = (
                    store._artifact_counter.get(artifact.artifact_type, 0) + 1
                )
        except Exception:
            pass
        return store


@dataclass
class RunManifest(CanonicalRunManifest):
    """Compatibility wrapper around the canonical run manifest model."""

    @classmethod
    def from_run_result(cls, result: WorkspaceRunResult) -> "RunManifest":
        return cls(
            run_id=result.run_id,
            workspace_id=result.workspace_id,
            task_id=result.task_id,
            status=_run_status_from_workspace_result(result),
            started_at=result.start_timestamp,
            completed_at=result.end_timestamp,
            duration_seconds=result.duration_seconds,
            final_agent_id=result.final_agent_id,
            final_answer=result.final_answer,
            summary=result.summary,
            total_tokens=result.total_tokens,
            error_count=result.error_count or len(result.errors),
            warning_count=len(result.warnings),
            result_summary_path=result.run_manifest_path,
            artifact_manifest_path=result.artifact_manifest_path,
            log_summary_path=result.log_summary_path,
            run_dir=result.run_dir,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "created_at": _serialize_datetime(self.created_at),
            "started_at": _serialize_datetime(self.started_at),
            "completed_at": _serialize_datetime(self.completed_at),
            "duration_seconds": self.duration_seconds,
            "execution_order": list(self.execution_order),
            "agents_executed": list(self.agents_executed),
            "parallel_groups": self.parallel_groups,
            "final_agent_id": self.final_agent_id,
            "final_answer": self.final_answer,
            "summary": self.summary,
            "result_summary_path": _serialize_path(self.result_summary_path),
            "events_path": _serialize_path(self.events_path),
            "notifications_path": _serialize_path(self.notifications_path),
            "report_path": _serialize_path(self.report_path),
            "idea_path": _serialize_path(self.idea_path),
            "total_tokens": self.total_tokens,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "artifact_manifest_path": _serialize_path(self.artifact_manifest_path),
            "log_summary_path": _serialize_path(self.log_summary_path),
            "run_dir": _serialize_path(self.run_dir),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["RunManifest"]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                run_id=str(data["run_id"]),
                workspace_id=str(data["workspace_id"]),
                task_id=data.get("task_id"),
                status=_run_status_from_value(data.get("status")),
                created_at=_parse_datetime(data.get("created_at")) or datetime.now(),
                started_at=_parse_datetime(data.get("started_at")),
                completed_at=_parse_datetime(data.get("completed_at")),
                duration_seconds=float(data.get("duration_seconds", 0.0)),
                execution_order=list(data.get("execution_order", [])),
                agents_executed=list(data.get("agents_executed", [])),
                parallel_groups=int(data.get("parallel_groups", 0)),
                final_agent_id=data.get("final_agent_id"),
                final_answer=str(data.get("final_answer", "")),
                summary=str(data.get("summary", "")),
                result_summary_path=_parse_path(data.get("result_summary_path")),
                events_path=_parse_path(data.get("events_path")),
                notifications_path=_parse_path(data.get("notifications_path")),
                report_path=_parse_path(data.get("report_path")),
                idea_path=_parse_path(data.get("idea_path")),
                total_tokens=int(data.get("total_tokens", 0)),
                error_count=int(data.get("error_count", 0)),
                warning_count=int(data.get("warning_count", 0)),
                artifact_manifest_path=_parse_path(data.get("artifact_manifest_path")),
                log_summary_path=_parse_path(data.get("log_summary_path")),
                run_dir=_parse_path(data.get("run_dir")),
            )
        except Exception:
            return None


@dataclass
class LogSummary(CanonicalLogSummary):
    """Compatibility wrapper around the canonical log summary model."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "total_events": self.total_events,
            "agent_executions": self.agent_executions,
            "parallel_groups": self.parallel_groups,
            "topology_changes": self.topology_changes,
            "memory_reads": self.memory_reads,
            "memory_writes": self.memory_writes,
            "stages": [_serialize_stage_transition(stage) for stage in self.stages],
            "errors": [_serialize_error_signature(error) for error in self.errors],
            "warnings": [
                _serialize_error_signature(warning) for warning in self.warnings
            ],
            "final_status": self.final_status.value,
            "final_agent_id": self.final_agent_id,
            "final_message": self.final_message,
            "start_time": _serialize_datetime(self.start_time),
            "end_time": _serialize_datetime(self.end_time),
            "duration_seconds": self.duration_seconds,
            "total_tokens": self.total_tokens,
            "raw_log_pointer": _serialize_raw_log_pointer(self.raw_log_pointer),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["LogSummary"]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                run_id=str(data["run_id"]),
                workspace_id=str(data["workspace_id"]),
                total_events=int(data.get("total_events", 0)),
                agent_executions=int(data.get("agent_executions", 0)),
                parallel_groups=int(data.get("parallel_groups", 0)),
                topology_changes=int(data.get("topology_changes", 0)),
                memory_reads=int(data.get("memory_reads", 0)),
                memory_writes=int(data.get("memory_writes", 0)),
                stages=[
                    _parse_stage_transition(item) for item in data.get("stages", [])
                ],
                errors=[
                    _parse_error_signature(item) for item in data.get("errors", [])
                ],
                warnings=[
                    _parse_error_signature(item) for item in data.get("warnings", [])
                ],
                final_status=_run_status_from_value(data.get("final_status")),
                final_agent_id=data.get("final_agent_id"),
                final_message=str(data.get("final_message", "")),
                start_time=_parse_datetime(data.get("start_time")),
                end_time=_parse_datetime(data.get("end_time")),
                duration_seconds=float(data.get("duration_seconds", 0.0)),
                total_tokens=int(data.get("total_tokens", 0)),
                raw_log_pointer=_parse_raw_log_pointer(data.get("raw_log_pointer")),
            )
        except Exception:
            return None


@dataclass
class ArtifactManifest(CanonicalArtifactManifest):
    """Compatibility wrapper around the canonical artifact manifest model."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "artifacts": [
                _serialize_artifact_meta(artifact) for artifact in self.artifacts
            ],
            "reports": [
                _serialize_artifact_meta(artifact) for artifact in self.reports
            ],
            "memory_dumps": [
                _serialize_artifact_meta(artifact) for artifact in self.memory_dumps
            ],
            "snapshots": [
                _serialize_artifact_meta(artifact) for artifact in self.snapshots
            ],
            "logs": [_serialize_artifact_meta(artifact) for artifact in self.logs],
            "summaries": [
                _serialize_artifact_meta(artifact) for artifact in self.summaries
            ],
            "result_summary": _serialize_artifact_meta(self.result_summary)
            if self.result_summary
            else None,
            "events_log": _serialize_artifact_meta(self.events_log)
            if self.events_log
            else None,
            "main_report": _serialize_artifact_meta(self.main_report)
            if self.main_report
            else None,
            "total_artifacts": self.total_artifacts,
            "total_size_bytes": self.total_size_bytes,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["ArtifactManifest"]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                run_id=str(data["run_id"]),
                workspace_id=str(data["workspace_id"]),
                artifacts=[
                    _parse_artifact_meta(item) for item in data.get("artifacts", [])
                ],
                reports=[
                    _parse_artifact_meta(item) for item in data.get("reports", [])
                ],
                memory_dumps=[
                    _parse_artifact_meta(item) for item in data.get("memory_dumps", [])
                ],
                snapshots=[
                    _parse_artifact_meta(item) for item in data.get("snapshots", [])
                ],
                logs=[_parse_artifact_meta(item) for item in data.get("logs", [])],
                summaries=[
                    _parse_artifact_meta(item) for item in data.get("summaries", [])
                ],
                result_summary=_parse_artifact_meta(data["result_summary"])
                if data.get("result_summary")
                else None,
                events_log=_parse_artifact_meta(data["events_log"])
                if data.get("events_log")
                else None,
                main_report=_parse_artifact_meta(data["main_report"])
                if data.get("main_report")
                else None,
                total_artifacts=int(data.get("total_artifacts", 0)),
                total_size_bytes=int(data.get("total_size_bytes", 0)),
            )
        except Exception:
            return None


class ArtifactCollector:
    """
    Collects and catalogs artifacts from a workspace run.

    Runtime code uses ArtifactRef objects, while compatibility manifests
    are projected into the canonical observability schema.
    """

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.store = ArtifactStore(self.run_dir)

    def collect_logs(self) -> list[ArtifactRef]:
        logs_dir = self.run_dir / "logs"
        if not logs_dir.exists():
            return []

        artifacts = []
        for log_file in logs_dir.glob("**/*.log"):
            stat = log_file.stat()
            artifact = ArtifactRef(
                artifact_id=f"log_{log_file.stem}",
                artifact_type=ArtifactType.LOG,
                path=log_file,
                mime_type="text/plain",
                size_bytes=stat.st_size,
                description=f"Log file: {log_file.name}",
            )
            artifacts.append(artifact)
            self.store.add_artifact(artifact)
        return artifacts

    def collect_reports(self) -> list[ArtifactRef]:
        reports_dir = self.run_dir / "reports"
        if not reports_dir.exists():
            return []

        artifacts = []
        for report_file in reports_dir.glob("**/*.md"):
            stat = report_file.stat()
            artifact = ArtifactRef(
                artifact_id=f"report_{report_file.stem}",
                artifact_type=ArtifactType.REPORT,
                path=report_file,
                mime_type="text/markdown",
                size_bytes=stat.st_size,
                description=f"Report file: {report_file.name}",
            )
            artifacts.append(artifact)
            self.store.add_artifact(artifact)
        return artifacts

    def collect_snapshots(self) -> list[ArtifactRef]:
        snapshots_dir = self.run_dir / "snapshots"
        if not snapshots_dir.exists():
            return []

        artifacts = []
        for snapshot_file in snapshots_dir.glob("**/*"):
            if not snapshot_file.is_file():
                continue
            stat = snapshot_file.stat()
            artifact = ArtifactRef(
                artifact_id=f"snapshot_{snapshot_file.stem}",
                artifact_type=ArtifactType.SNAPSHOT,
                path=snapshot_file,
                size_bytes=stat.st_size,
                description=f"Snapshot: {snapshot_file.name}",
            )
            artifacts.append(artifact)
            self.store.add_artifact(artifact)
        return artifacts

    def collect_graph_snapshots(self) -> list[ArtifactRef]:
        artifacts = []
        for graph_file in self.run_dir.glob("**/*graph*.json"):
            stat = graph_file.stat()
            artifact = ArtifactRef(
                artifact_id=f"graph_{graph_file.stem}",
                artifact_type=ArtifactType.GRAPH_SNAPSHOT,
                path=graph_file,
                mime_type="application/json",
                size_bytes=stat.st_size,
                description=f"Graph snapshot: {graph_file.name}",
            )
            artifacts.append(artifact)
            self.store.add_artifact(artifact)
        return artifacts

    def collect_all(self) -> list[ArtifactRef]:
        all_artifacts = []
        all_artifacts.extend(self.collect_logs())
        all_artifacts.extend(self.collect_reports())
        all_artifacts.extend(self.collect_snapshots())
        all_artifacts.extend(self.collect_graph_snapshots())

        summary_file = self.run_dir / "result_summary.json"
        if summary_file.exists():
            stat = summary_file.stat()
            artifact = ArtifactRef(
                artifact_id="result_summary",
                artifact_type=ArtifactType.RUN_MANIFEST,
                path=summary_file,
                mime_type="application/json",
                size_bytes=stat.st_size,
                description="Run result summary",
            )
            all_artifacts.append(artifact)
            self.store.add_artifact(artifact)

        events_file = self.run_dir / "events.jsonl"
        if events_file.exists():
            stat = events_file.stat()
            artifact = ArtifactRef(
                artifact_id="events_log",
                artifact_type=ArtifactType.LOG,
                path=events_file,
                mime_type="application/x-ndjson",
                size_bytes=stat.st_size,
                description="Agent execution events log",
            )
            all_artifacts.append(artifact)
            self.store.add_artifact(artifact)

        return all_artifacts

    def create_artifact_manifest(self) -> ArtifactManifest:
        manifest = ArtifactManifest(
            run_id=self.run_dir.name,
            workspace_id="",
        )

        for artifact in self.store.get_all_artifacts():
            meta = _artifact_ref_to_meta(artifact)
            manifest.add_artifact(meta)
            if artifact.artifact_type == ArtifactType.RUN_MANIFEST:
                manifest.result_summary = meta
            elif artifact.artifact_id == "events_log":
                manifest.events_log = meta
            elif (
                artifact.artifact_type == ArtifactType.REPORT
                and manifest.main_report is None
            ):
                manifest.main_report = meta

        manifest_path = self.run_dir / "artifact_manifest.json"
        manifest.save(manifest_path)
        return manifest
