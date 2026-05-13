"""Regression tests for workspace run indexing and observability."""

import json
from pathlib import Path

from umbrella.artifacts.error_signatures import extract_error_signatures
from umbrella.artifacts.log_access import tail_log
from umbrella.artifacts.log_summary import summarize_run_logs
from umbrella.artifacts.manifests import build_artifact_manifest, build_run_manifest
from umbrella.artifacts.models import ArtifactCategory, RunStatus
from umbrella.artifacts.run_index import (
    get_latest_run,
    get_run_by_id,
    index_workspace_runs,
)


def _write_run(
    workspace_root: Path,
    run_id: str,
    *,
    status: str = "completed",
    final_agent_id: str | None = "delivery_agent",
    include_events: bool = True,
    include_report: bool = True,
) -> Path:
    run_dir = workspace_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    report_path = None
    if include_report:
        reports_dir = workspace_root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{run_id}_report.md"
        report_path.write_text(f"# Report for {run_id}\n", encoding="utf-8")

    summary_payload = {
        "run_id": run_id,
        "status": status,
        "final_agent_id": final_agent_id,
        "execution_order": ["brief_architect", "delivery_agent"]
        if final_agent_id
        else [],
        "final_answer": "done" if final_agent_id else "",
        "total_tokens": 321,
        "total_time": 12.5,
        "report_path": str(report_path) if report_path else None,
        "idea_path": None,
        "events_path": str(run_dir / "events.jsonl"),
        "notifications_path": str(run_dir / "human_notifications.jsonl"),
        "errors": [] if status == "completed" else ["boom"],
    }
    (run_dir / "result_summary.json").write_text(
        json.dumps(summary_payload, indent=2),
        encoding="utf-8",
    )

    if include_events:
        events = [
            {
                "event_type": "run_start",
                "timestamp": "2026-03-30T12:00:00Z",
                "query": "test",
            },
            {
                "event_type": "agent_start",
                "timestamp": "2026-03-30T12:00:01Z",
                "agent_id": "brief_architect",
            },
            {
                "event_type": "agent_end",
                "timestamp": "2026-03-30T12:00:05Z",
                "agent_id": "brief_architect",
                "is_final": True,
                "tokens_used": 123,
            },
            {
                "event_type": "run_end",
                "timestamp": "2026-03-30T12:00:12Z",
                "success": status == "completed",
                "final_agent_id": final_agent_id,
                "final_answer": "done" if final_agent_id else "",
                "error": "" if status == "completed" else "boom",
            },
        ]
        (run_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )

    return run_dir


def test_index_workspace_runs_finds_and_orders_runs(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _write_run(workspace_root, "20260330T120000Z_old")
    _write_run(workspace_root, "20260330T130000Z_new")

    index = index_workspace_runs(workspace_root, "workspace")

    assert index.total_runs == 2
    assert index.latest_run is not None
    assert index.latest_run.run_id == "20260330T130000Z_new"


def test_get_run_by_id_returns_expected_manifest(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _write_run(workspace_root, "run-123")

    manifest = get_run_by_id(workspace_root, "run-123", "workspace")

    assert manifest is not None
    assert manifest.run_id == "run-123"
    assert manifest.status == RunStatus.COMPLETED


def test_get_latest_run_successful_only_skips_failed_runs(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _write_run(workspace_root, "run-ok", status="completed")
    _write_run(workspace_root, "run-failed", status="failed", final_agent_id=None)

    latest_success = get_latest_run(workspace_root, "workspace", successful_only=True)

    assert latest_success is not None
    assert latest_success.run_id == "run-ok"


def test_build_run_manifest_reads_failed_status_from_summary(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = _write_run(
        workspace_root, "run-failed", status="failed", final_agent_id=None
    )

    manifest = build_run_manifest(run_dir, "workspace")

    assert manifest is not None
    assert manifest.status == RunStatus.FAILED
    assert manifest.report_path is not None


def test_build_artifact_manifest_uses_report_path_from_summary(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = _write_run(workspace_root, "run-artifacts")

    manifest = build_artifact_manifest(run_dir, "workspace")

    assert manifest.main_report is not None
    assert manifest.main_report.category == ArtifactCategory.REPORT
    assert manifest.main_report.path.name == "run-artifacts_report.md"


def test_summarize_run_logs_uses_events_and_stays_clean_on_success(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = _write_run(workspace_root, "run-summary")

    summary = summarize_run_logs(run_dir, "workspace")

    assert summary is not None
    assert summary.final_status == RunStatus.COMPLETED
    assert summary.warning_count == 0
    assert summary.error_count == 0


def test_extract_error_signatures_reports_real_failure_only(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = _write_run(
        workspace_root, "run-errors", status="failed", final_agent_id=None
    )
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]

    signatures = extract_error_signatures(events)

    assert signatures
    assert any(signature.error_type == "RunFailure" for signature in signatures)


def test_tail_log_returns_recent_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "events.log"
    log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert tail_log(log_path, max_lines=2) == "two\nthree"
