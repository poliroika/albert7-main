from pathlib import Path

from umbrella.artifacts.log_access import read_result_summary
from umbrella.artifacts.models import (
    ArtifactCategory,
    ArtifactManifest,
    ArtifactMeta,
    RunManifest,
    RunStatus,
)


def _status_from_summary(summary: dict | None) -> RunStatus:
    if not summary:
        return RunStatus.UNKNOWN
    status = str(summary.get("status") or "").lower()
    if status in {"completed", "complete", "verified"}:
        return RunStatus.COMPLETED
    if status in {
        "failed",
        "error",
        "incomplete",
        "failed_verification",
        "failed_hygiene",
    }:
        return RunStatus.FAILED
    if status == "running":
        return RunStatus.RUNNING
    if summary.get("success") is True:
        return RunStatus.COMPLETED
    if summary.get("success") is False:
        return RunStatus.FAILED
    return RunStatus.UNKNOWN


def build_run_manifest(run_dir: Path, workspace_id: str) -> RunManifest:
    summary = read_result_summary(run_dir / "result_summary.json")
    run_id = str((summary or {}).get("run_id") or run_dir.name)
    manifest = RunManifest(
        run_id=run_id,
        workspace_id=workspace_id,
        status=_status_from_summary(summary),
        final_agent_id=(summary or {}).get("final_agent_id"),
        final_answer=str((summary or {}).get("final_answer") or ""),
        execution_order=list((summary or {}).get("execution_order", [])),
        total_tokens=int((summary or {}).get("total_tokens", 0) or 0),
        duration_seconds=float((summary or {}).get("total_time", 0.0) or 0.0),
        summary=str((summary or {}).get("summary") or ""),
        result_summary_path=run_dir / "result_summary.json",
        events_path=Path(str((summary or {}).get("events_path")))
        if (summary or {}).get("events_path")
        else None,
        notifications_path=Path(str((summary or {}).get("notifications_path")))
        if (summary or {}).get("notifications_path")
        else None,
        report_path=Path(str((summary or {}).get("report_path")))
        if (summary or {}).get("report_path")
        else None,
        idea_path=Path(str((summary or {}).get("idea_path")))
        if (summary or {}).get("idea_path")
        else None,
        run_dir=run_dir,
    )
    return manifest


def build_artifact_manifest(run_dir: Path, workspace_id: str) -> ArtifactManifest:
    summary = read_result_summary(run_dir / "result_summary.json") or {}
    manifest = ArtifactManifest(run_id=run_dir.name, workspace_id=workspace_id)

    def _add(
        path: Path, category: ArtifactCategory, artifact_id: str, mime: str
    ) -> ArtifactMeta:
        meta = ArtifactMeta(
            artifact_id=artifact_id,
            name=path.name,
            path=path,
            category=category,
            size_bytes=path.stat().st_size if path.exists() else 0,
            mime_type=mime,
        )
        manifest.add_artifact(meta)
        return meta

    result_summary_path = run_dir / "result_summary.json"
    if result_summary_path.exists():
        manifest.result_summary = _add(
            result_summary_path,
            ArtifactCategory.SUMMARY,
            "result_summary",
            "application/json",
        )

    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        manifest.events_log = _add(
            events_path, ArtifactCategory.LOG, "events_log", "application/x-ndjson"
        )

    report_path_raw = summary.get("report_path")
    if report_path_raw:
        report_path = Path(str(report_path_raw))
        if report_path.exists():
            manifest.main_report = _add(
                report_path, ArtifactCategory.REPORT, "main_report", "text/markdown"
            )

    for local_report in run_dir.glob("*.md"):
        _add(
            local_report,
            ArtifactCategory.REPORT,
            f"report_{local_report.stem}",
            "text/markdown",
        )

    for memory_file in (run_dir / "memory").glob("**/*"):
        if memory_file.is_file():
            _add(
                memory_file,
                ArtifactCategory.MEMORY,
                f"memory_{memory_file.stem}",
                "text/plain",
            )

    return manifest
