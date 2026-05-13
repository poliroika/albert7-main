from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from umbrella.artifacts.error_signatures import extract_error_signatures
from umbrella.artifacts.log_access import read_events_jsonl
from umbrella.artifacts.models import LogSummary, RunStatus, StageTransition


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def extract_stage_transitions(events: list[dict[str, Any]]) -> list[StageTransition]:
    out: list[StageTransition] = []
    for event in events:
        ev_type = str(event.get("event_type", ""))
        if ev_type not in {"agent_start", "agent_end"}:
            continue
        out.append(
            StageTransition(
                stage=str(event.get("agent_id") or "unknown"),
                timestamp=_parse_ts(event.get("timestamp"))
                or datetime.now(timezone.utc),
                agent_id=event.get("agent_id"),
                status="started" if ev_type == "agent_start" else "completed",
            )
        )
    return out


def count_errors_and_warnings(events: list[dict[str, Any]]) -> tuple[int, int]:
    signatures = extract_error_signatures(events)
    errors = sum(1 for s in signatures if s.severity.value in {"error", "critical"})
    warnings = sum(1 for s in signatures if s.severity.value == "warning")
    return errors, warnings


def summarize_run_logs(run_dir: Path, workspace_id: str) -> LogSummary:
    events = read_events_jsonl(run_dir / "events.jsonl")
    transitions = extract_stage_transitions(events)
    signatures = extract_error_signatures(events)
    warnings = [s for s in signatures if s.severity.value == "warning"]
    errors = [s for s in signatures if s.severity.value != "warning"]
    run_end = next(
        (e for e in reversed(events) if e.get("event_type") == "run_end"), {}
    )
    success = bool(run_end.get("success")) if run_end else False
    status = (
        RunStatus.COMPLETED
        if success
        else (RunStatus.FAILED if run_end else RunStatus.UNKNOWN)
    )
    start_time = _parse_ts(events[0].get("timestamp")) if events else None
    end_time = _parse_ts(run_end.get("timestamp")) if run_end else None
    duration = 0.0
    if start_time and end_time:
        duration = max(0.0, (end_time - start_time).total_seconds())
    total_tokens = sum(int(e.get("tokens_used", 0) or 0) for e in events)
    return LogSummary(
        run_id=run_dir.name,
        workspace_id=workspace_id,
        total_events=len(events),
        agent_executions=sum(1 for e in events if e.get("event_type") == "agent_end"),
        stages=transitions,
        errors=errors,
        warnings=warnings,
        final_status=status,
        final_agent_id=run_end.get("final_agent_id"),
        final_message=str(run_end.get("final_answer") or run_end.get("error") or ""),
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration,
        total_tokens=total_tokens,
    )
