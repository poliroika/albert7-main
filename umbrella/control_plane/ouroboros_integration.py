"""
Real Ouroboros integration for Umbrella self-improvement.

This module provides functions that actually use the Ouroboros agent
to perform code updates and self-improvement tasks, instead of just
delegating back to Umbrella's own runner.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from umbrella.control_plane.remediation_planner import (
    synthesise_verification_remediation_plan,
)
from umbrella.orchestration.context_overlays import build_context_overlays

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhaseBoundaryEvent:
    repo_root: Path
    workspace_id: str
    task_id: str
    event_type: str
    attempt: int = 0
    max_attempts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def _record_baseline(repo_root: Path) -> str:
    """Record HEAD sha before the candidate run starts."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _collect_changed_files(repo_root: Path, baseline_sha: str = "") -> list[str]:
    """Collect all files changed since *baseline_sha*.

    Covers committed (since baseline), staged, unstaged, and untracked.
    Falls back to an empty list on any error so the main flow is never
    blocked.
    """
    paths: set[str] = set()
    _run_kw = dict(
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    try:
        if baseline_sha:
            r = subprocess.run(
                ["git", "diff", "--name-only", baseline_sha, "HEAD"], **_run_kw
            )
            if r.returncode == 0:
                paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())
        r = subprocess.run(["git", "diff", "--name-only", "--cached"], **_run_kw)
        if r.returncode == 0:
            paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())
        r = subprocess.run(["git", "diff", "--name-only"], **_run_kw)
        if r.returncode == 0:
            paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())
        r = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"], **_run_kw
        )
        if r.returncode == 0:
            paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())
    except Exception:
        log.debug("_collect_changed_files failed (non-fatal)", exc_info=True)
    return sorted(paths)


def _filter_workspace_changes(paths: list[str], workspace_id: str) -> list[str]:
    prefix = f"workspaces/{workspace_id}/"
    return sorted({p for p in paths if isinstance(p, str) and p.startswith(prefix)})


def _truncate_final_message(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[truncated]"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail_text(value: Any, limit: int = 2000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[-limit:]


def _verification_failures(
    verification_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(verification_payload, dict):
        return []
    failures: list[dict[str, Any]] = []
    for item in verification_payload.get("results") or []:
        if not isinstance(item, dict) or item.get("optional"):
            continue
        if str(item.get("status") or "").lower() == "passed":
            continue
        failures.append(
            {
                "name": str(item.get("name") or ""),
                "kind": str(item.get("kind") or ""),
                "status": str(item.get("status") or ""),
                "summary": _tail_text(item.get("summary"), 1200),
                "error": _tail_text(item.get("error"), 1200),
                "stdout": _tail_text(item.get("stdout_tail"), 2000),
                "stderr": _tail_text(item.get("stderr_tail"), 2000),
            }
        )
    return failures


# --- Promotion safety -------------------------------------------------------

# Verification step kinds that ACTUALLY exercise behaviour. Anything
# else (``import_check``, ``file_exists``, ``source_policy``) only
# proves the code parses / files exist / static rules pass — it does
# NOT prove the workspace does its job. Auto-promotion needs at least
# one of these "real" gates.
_BEHAVIOURAL_VERIFICATION_KINDS: frozenset[str] = frozenset(
    {
        "shell",
        "http_boot",
        "behavioral_http",
        "input_sensitivity",
        "pptx_diff",
    }
)


def _verification_spec_is_shallow(
    verification_payload: dict[str, Any] | None,
) -> bool:
    """Return ``True`` when verification only ran static-ish checks.

    Intentionally permissive: we treat the spec as shallow only when
    there is at least one passing required step AND none of them are
    behavioural. A spec with mixed static + behavioural steps is fine.
    """
    if not isinstance(verification_payload, dict):
        return False
    results = verification_payload.get("results") or []
    if not isinstance(results, list):
        return False
    required_kinds: set[str] = set()
    saw_required = False
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("optional"):
            continue
        if str(item.get("status") or "").lower() != "passed":
            continue
        saw_required = True
        kind = str(item.get("kind") or "").strip().lower()
        if kind:
            required_kinds.add(kind)
    if not saw_required:
        return False
    return not (required_kinds & _BEHAVIOURAL_VERIFICATION_KINDS)


def _workspace_allows_shallow_promotion(repo_root: Path, workspace_id: str) -> bool:
    """Read ``workspace.toml`` and check the explicit opt-in flag.

    The lookup is intentionally narrow — only ``[promotion]
    allow_shallow_verification`` matters. We do not infer permission
    from anything else; explicit beats clever.
    """
    if not workspace_id:
        return False
    workspace_path = repo_root / "workspaces" / workspace_id / "workspace.toml"
    if not workspace_path.is_file():
        return False
    try:
        try:  # Python 3.11+
            import tomllib as _toml  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # pragma: no cover
            import tomli as _toml  # type: ignore[no-redef]
        with workspace_path.open("rb") as fh:
            data = _toml.load(fh)
    except Exception:
        return False
    section = data.get("promotion")
    if not isinstance(section, dict):
        return False
    return bool(section.get("allow_shallow_verification", False))


def _log_phase_boundary_event(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    event_type: str,
    attempt: int = 0,
    max_attempts: int = 0,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append a typed boundary event to the run's events.jsonl.

    Used by the timeline UI to bucket round_io.jsonl entries into
    named phases (initial / self_review_N / remediation_N) without
    heuristic guessing — the timestamps cleanly separate the buckets.
    """
    try:
        events_path = (
            _canonical_drive_root(repo_root, workspace_id) / "logs" / "events.jsonl"
        )
        events_path.parent.mkdir(parents=True, exist_ok=True)
        event = PhaseBoundaryEvent(
            repo_root=repo_root,
            workspace_id=workspace_id,
            task_id=task_id,
            event_type=event_type,
            attempt=attempt,
            max_attempts=max_attempts,
            metadata=dict(metadata or {}),
        )
        payload = asdict(event)
        payload.pop("repo_root", None)
        payload["type"] = payload.pop("event_type")
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("phase boundary event log failed", exc_info=True)


def _log_initial_phase_started(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
) -> None:
    _log_phase_boundary_event(
        repo_root=repo_root,
        workspace_id=workspace_id,
        task_id=task_id,
        event_type="initial_started",
        attempt=0,
        max_attempts=0,
    )


def _log_verification_phase_event(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    event_type: str,
    verification_payload: dict[str, Any] | None = None,
) -> None:
    _log_phase_boundary_event(
        repo_root=repo_root,
        workspace_id=workspace_id,
        task_id=task_id,
        event_type=event_type,
        attempt=0,
        max_attempts=0,
    )


_STOP_RESPONSE_PREFIXES: tuple[str, ...] = (
    "Stop requested by dashboard",
    "Stop requested by web ui",
    "Stop requested from the web UI",
)


def _stop_request_paths(repo_root: Path, workspace_id: str) -> list[Path]:
    """Mirrors :meth:`WebBridgeApp._stop_request_paths` so the launcher,
    the integration, and the loop all consult the same set of files.

    Keep these in sync if a fourth location is ever added.
    """
    repo_root = repo_root.resolve()
    paths: list[Path] = [
        repo_root / ".umbrella" / "launcher" / "stop_requested.json",
        repo_root / ".umbrella" / "ouroboros_drive" / "state" / "stop_requested.json",
    ]
    if workspace_id:
        try:
            drive_root = _canonical_drive_root(repo_root, workspace_id)
            paths.append(drive_root / "state" / "stop_requested.json")
        except Exception:
            log.debug("could not resolve workspace drive for stop paths", exc_info=True)
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _stop_request_targets_task(payload: Any, task_id: str) -> bool:
    """Return True iff ``payload`` (a parsed ``stop_requested.json``) targets
    ``task_id`` (or any of its remediation/self-review descendants)."""
    if not isinstance(payload, dict):
        return True  # legacy/empty payload — assume global stop
    current = str(task_id or "").strip()
    requested: set[str] = set()
    for key in ("run_id", "task_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            requested.add(value)
    for key in ("attempt_task_ids", "candidate_run_ids"):
        values = payload.get(key)
        if isinstance(values, list):
            requested.update(
                str(item).strip() for item in values if str(item or "").strip()
            )
    if not requested:
        return True
    if not current:
        return False
    return any(current == r or current.startswith(f"{r}__") for r in requested)


def _read_stop_request_for_task(
    repo_root: Path, workspace_id: str, task_id: str
) -> dict[str, Any] | None:
    """Return the live stop-request payload that targets ``task_id``, or None.

    The integration uses this between remediation iterations to break out
    of the loop the moment the user clicks the dashboard's Stop button —
    otherwise each cycle sees the in-loop stop check fire, exits in <1 s
    with zero LLM rounds, and we just spin until the budget is exhausted
    (the symptom user reported as "куча remediation за пол-минуты").
    """
    for path in _stop_request_paths(repo_root, workspace_id):
        try:
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if _stop_request_targets_task(payload, task_id):
            return payload if isinstance(payload, dict) else {}
    return None


def _final_message_indicates_stop(text: Any) -> bool:
    """Detect the "Stop requested by dashboard: …" reply the loop emits
    when ``_check_stop_requested`` fires. The launcher-side fallback
    ("⚠️ Model returned an empty response.") would also surface here
    if we ever miss the real signal, but the canonical marker is the
    "Stop requested" prefix."""
    snippet = str(text or "").strip()
    if not snippet:
        return False
    head = snippet.splitlines()[0].strip().lower()
    for prefix in _STOP_RESPONSE_PREFIXES:
        if head.startswith(prefix.lower()):
            return True
    return False


def _clear_stop_requests_for_task(
    repo_root: Path, workspace_id: str, task_id: str
) -> None:
    """Remove any stale ``stop_requested.json`` whose payload targets
    ``task_id``. Called at the start of a fresh sync run so the in-loop
    stop checker doesn't immediately abort the new run because of a
    prior session that was never cleaned up.

    We deliberately do NOT wipe unrelated payloads (other run ids):
    those belong to other live runs and clearing them would silently
    cancel the cancellation request.
    """
    for path in _stop_request_paths(repo_root, workspace_id):
        try:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                payload = None
            if _stop_request_targets_task(payload, task_id):
                path.unlink(missing_ok=True)
        except OSError:
            log.debug("failed to remove stale stop file %s", path, exc_info=True)


def _build_cancellation_message(
    *,
    stop_payload: dict[str, Any] | None,
    remediation_attempts_used: int,
    final_message_from_agent: str,
) -> str:
    """User-visible Russian explanation that the run was cancelled."""
    reason = ""
    if isinstance(stop_payload, dict):
        reason = str(stop_payload.get("reason") or "").strip()
    if not reason:
        reason = "запуск остановлен пользователем из веб-UI"
    lines = [
        "# Run остановлен пользователем",
        "",
        f"Причина: {reason}.",
    ]
    if remediation_attempts_used:
        lines.append(
            f"Использованных циклов remediation до остановки: "
            f"`{remediation_attempts_used}`."
        )
    agent_tail = (final_message_from_agent or "").strip()
    if agent_tail and not _final_message_indicates_stop(agent_tail):
        snippet = agent_tail[:1500]
        lines.extend(
            [
                "",
                "## Последнее сообщение агента",
                snippet,
            ]
        )
    return "\n".join(lines).strip()


def _self_review_already_run_in_aggregate(
    aggregate_events: list[dict[str, Any]],
) -> bool:
    """Return True if the aggregate_events list already contains a
    self-review marker — guards against accidental double-fire when
    the loop iterates after an already-completed self-review pass.
    """
    for event in aggregate_events:
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") == "self_review_started":
            return True
    return False


def _render_self_review_remediation_prompt(
    *,
    original_task: str,
    fixlist_body: str,
    attempt: int,
    max_attempts: int,
    limit_chars: int = 12000,
) -> str:
    """Build a remediation prompt seeded by the agent's own self-review.

    Different from :func:`render_verification_remediation_prompt`: there
    is no failing-checks block (verification PASSED), the driver is
    the model's own NEEDS_FIX list. We surface the list verbatim and
    instruct the agent to address each item with the smallest
    possible change.
    """
    cleaned_body = (fixlist_body or "").strip()
    if not cleaned_body:
        cleaned_body = "(self-review reply did not include a numbered fixlist; treat the original task as the gap)"
    lines = [
        "# Self-Review Remediation (SAME RUN)",
        "",
        f"Self-review fix-cycle `{attempt}/{max_attempts}` of the SAME run.",
        "",
        "You yourself flagged these issues after looking at the real run",
        "output. Verification already passed once; this cycle is for the",
        "extra defects you just found.",
        "",
        "## Hard rules",
        "- Address ONLY the items in the fixlist below. No new features.",
        "- Make the smallest change that removes each defect.",
        "- After each fix, re-run the affected entrypoint via",
        "  `run_workspace_command` and confirm the output now looks right.",
        "- Send your final message ONLY when every item in the fixlist",
        "  is resolved. Do NOT re-evaluate; the harness will re-verify.",
        "",
        "## Your Fixlist (your own self-review reply)",
        "```",
        cleaned_body[:6000],
        "```",
        "",
    ]
    excerpt = original_task.strip()
    if len(excerpt) > 2500:
        excerpt = excerpt[:2500].rstrip() + "\n…[truncated]"
    lines.extend(["## Original Task Reference", excerpt, ""])
    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = text[:limit_chars].rstrip()
    return text


def _archive_plan_before_remediation(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    attempt: int,
) -> None:
    """Move the cached ``TaskPlan`` for ``task_id`` aside before re-submitting
    the same task id for verification remediation.

    Without this the loop sees a completed plan, skips the planner +
    subtask phases, and goes straight to ``final_aggregation`` with no
    tools — the model receives the failing checks but cannot call a
    single tool to fix them. The cycle repeats for every remediation
    attempt with 0 ``workspace_write_tools`` writes and 0 tool calls.

    The archived file stays on disk under ``task_plans/<id>.before_remediation_<N>.<ts>.json``
    so post-mortem tooling can still inspect what the previous attempt
    looked like.
    """
    try:
        from ouroboros.task_planner import TaskPlanStore
    except Exception:
        log.debug("TaskPlanStore unavailable; cannot archive plan", exc_info=True)
        return
    try:
        drive_root = _canonical_drive_root(repo_root, workspace_id)
        store = TaskPlanStore(drive_root)
        archived = store.archive(task_id, reason=f"before_remediation_{attempt}")
        if archived is not None:
            log.info(
                "Archived plan for %s before remediation attempt %d -> %s",
                task_id,
                attempt,
                archived,
            )
    except Exception:
        # Never let plan archival errors break the remediation loop. If
        # the file cannot be moved, the worst case is we go back to the
        # previous degenerate behaviour (which was already broken), not
        # something worse.
        log.debug("Failed to archive plan for %s", task_id, exc_info=True)


def _persist_verification_failure_context(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    remediation_attempt: int,
    max_attempts: int,
    verification_payload: dict[str, Any] | None,
    changed_files: list[str],
    sweep_payload: dict[str, Any] | None = None,
    completion_warnings: list[str] | None = None,
    failure_kind: str = "verification",
) -> dict[str, Any]:
    drive_root = _canonical_drive_root(repo_root, workspace_id)
    logs_dir = drive_root / "logs"
    state_dir = drive_root / "state"
    memory_dir = drive_root / "memory"
    logs_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)

    sweep_cleanup_targets = _sweep_cleanup_targets(sweep_payload)
    block = {
        "schema_version": 1,
        "ts": _iso_now(),
        "workspace_id": workspace_id,
        "task_id": task_id,
        "failure_kind": failure_kind,
        "remediation_attempt": remediation_attempt,
        "max_attempts": max_attempts,
        "passed": bool(verification_payload and verification_payload.get("passed")),
        "pass_rate": (verification_payload or {}).get("pass_rate"),
        "summary": _tail_text((verification_payload or {}).get("summary"), 4000),
        "failures": _verification_failures(verification_payload),
        "completion_warnings": list(completion_warnings or []),
        "sweep": _compact_sweep_payload(sweep_payload),
        "cleanup_targets": sweep_cleanup_targets,
        "changed_files": list(changed_files)[:200],
    }
    state_path = state_dir / "verification_failure_context.json"
    log_path = logs_dir / "verification_failures.jsonl"
    md_path = memory_dir / "verification_failure_context.md"

    state_path.write_text(
        json.dumps(block, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(block, ensure_ascii=False, default=str) + "\n")

    lines = [
        "# Verification Failure Context",
        "",
        f"- task_id: `{task_id}`",
        f"- remediation_attempt: `{remediation_attempt}/{max_attempts}`",
        f"- passed: `{block['passed']}`",
        "",
        "## Summary",
        str(block["summary"] or ""),
        "",
    ]
    if block["failures"]:
        lines.append("## Failing Checks")
        for failure in block["failures"]:
            lines.append(
                f"- `{failure['name']}` ({failure['kind']}): {failure['status']}"
            )
            if failure.get("summary"):
                lines.append(f"  {failure['summary']}")
        lines.append("")
    if sweep_cleanup_targets:
        lines.append("## Hygiene / Final Sweep Issues")
        lines.append(
            "Verification may already be green; these cleanup targets are the "
            "actual blocking gate. Use `delete_workspace_file` for removable "
            "workspace noise, or move files into the documented layout when they "
            "are real deliverables."
        )
        for target in sweep_cleanup_targets[:40]:
            reason = target.get("reason") or target.get("category") or "cleanup"
            lines.append(f"- `{target.get('path')}`: {reason}")
        lines.append("")
    if block["completion_warnings"]:
        lines.append("## Completion Warnings")
        for warning in block["completion_warnings"]:
            lines.append(f"- `{warning}`")
    md_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    block["state_path"] = str(state_path)
    block["log_path"] = str(log_path)
    block["memory_path"] = str(md_path)
    return block


def _compact_sweep_payload(sweep_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(sweep_payload, dict):
        return {}
    keep: dict[str, Any] = {}
    for key in (
        "status",
        "passed",
        "summary",
        "missing_required",
        "leftover_noise",
        "removed",
        "blocking_noise",
        "warning_noise",
    ):
        value = sweep_payload.get(key)
        if value:
            keep[key] = value
    return keep


def _sweep_cleanup_targets(
    sweep_payload: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if not isinstance(sweep_payload, dict):
        return []
    targets: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(path: str, *, reason: str, category: str = "") -> None:
        clean = str(path or "").strip()
        if not clean or clean in seen:
            return
        seen.add(clean)
        item = {"path": clean, "reason": reason}
        if category:
            item["category"] = category
        targets.append(item)

    for item in sweep_payload.get("blocking_noise") or []:
        if isinstance(item, dict):
            _add(
                str(item.get("path") or ""),
                reason="blocking final_sweep noise",
                category=str(item.get("category") or ""),
            )
        else:
            _add(str(item), reason="blocking final_sweep noise")
    for path in sweep_payload.get("leftover_noise") or []:
        _add(str(path), reason="leftover final_sweep noise")
    for path in sweep_payload.get("missing_required") or []:
        _add(str(path), reason="required file missing or misplaced")
    return targets


def _verification_has_actionable_failures(
    verification_payload: dict[str, Any] | None,
) -> bool:
    if not isinstance(verification_payload, dict):
        return False
    if verification_payload.get("skipped"):
        return True
    return bool(_verification_failures(verification_payload))


def _status_needs_remediation(status: str) -> bool:
    return status in {"failed_verification", "failed_hygiene"}


def _has_actionable_remediation_context(
    *,
    final_status: str,
    verification_payload: dict[str, Any] | None,
    sweep_payload: dict[str, Any] | None,
) -> bool:
    if final_status == "failed_verification":
        return _verification_has_actionable_failures(verification_payload)
    if final_status == "failed_hygiene":
        return bool(_sweep_cleanup_targets(sweep_payload))
    return False


_HYGIENE_WARNING_REASONS = {
    "sweep_blocking_noise",
    "sweep_leftover_noise",
    "sweep_missing_required",
    "sweep_missing_runtime_optional",
}


def _max_hygiene_remediations() -> int:
    try:
        return max(0, int(os.environ.get("OUROBOROS_MAX_HYGIENE_REMEDIATIONS", "1")))
    except (TypeError, ValueError):
        return 1


def _is_hygiene_only_failure(
    *,
    final_status: str,
    completion_warnings: list[str] | None,
    verification_payload: dict[str, Any] | None,
    sweep_payload: dict[str, Any] | None,
) -> bool:
    if final_status != "failed_hygiene":
        return False
    if _verification_failures(verification_payload):
        return False
    if not _sweep_cleanup_targets(sweep_payload):
        return False
    warnings = {str(item) for item in (completion_warnings or [])}
    return bool(warnings) and warnings.issubset(_HYGIENE_WARNING_REASONS)


def _verification_exhausted_message(
    *,
    attempts_used: int,
    verification_payload: dict[str, Any] | None,
    failure_context_path: str,
) -> str:
    summary = ""
    if isinstance(verification_payload, dict):
        summary = str(verification_payload.get("summary") or "").strip()
    lines = [
        f"Verification still failing after {attempts_used} remediation attempt(s).",
    ]
    if failure_context_path:
        lines.append(f"Latest failure context: `{failure_context_path}`.")
    if summary:
        lines.extend(["", summary])
    return _truncate_final_message("\n".join(lines))


def _self_review_contract_failure(body: str) -> bool:
    text = str(body or "")
    return (
        "Self-review returned an empty response" in text
        or "Self-review did not start with LGTM or NEEDS_FIX" in text
    )


_LLM_EMPTY_RESPONSE_PREFIXES: tuple[str, ...] = (
    "⚠️ Failed to get a response from",
    "⚠️ Model returned an empty response",
)


def _looks_like_llm_empty_response(message: str) -> bool:
    text = (message or "").lstrip()
    return any(text.startswith(prefix) for prefix in _LLM_EMPTY_RESPONSE_PREFIXES)


_STATUS_LABEL_RU: dict[str, str] = {
    "verified": "Готово (верификация пройдена)",
    "complete": "Готово",
    "completed": "Готово",
    "failed_verification": "Не пройдена верификация",
    "failed_hygiene": "Не пройдена финальная чистка workspace",
    "failed_self_review": "Self-review контракт нарушен",
    "phase_impasse": "Phase impasse (зацикливание completion-инструмента)",
    "incomplete_subtasks": "План не выполнен (есть failed required subtasks)",
    "incomplete_discovery": (
        "Не выполнен внешний discovery для активного домена "
        "(github / mcp / gmas / deep_search)"
    ),
    "incomplete": "Незавершено",
    "error": "Ошибка",
    "cancelled": "Остановлено пользователем",
}


def _ru_status_label(final_status: str) -> str:
    return _STATUS_LABEL_RU.get(final_status, f"Статус: {final_status}")


def _extract_failed_checks_ru(
    verification_payload: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(verification_payload, dict):
        return []
    items = verification_payload.get("results") or verification_payload.get("failures")
    if not isinstance(items, list):
        return []
    bullets: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").lower()
        if status == "passed":
            continue
        if item.get("optional"):
            continue
        name = str(item.get("name") or item.get("kind") or "проверка").strip()
        kind = str(item.get("kind") or "").strip()
        summary = str(item.get("summary") or item.get("error") or "").strip()
        if len(summary) > 280:
            summary = summary[:280].rstrip() + "…"
        prefix = f"`{name}`" + (f" ({kind})" if kind else "")
        bullets.append(f"- {prefix}: {summary or 'без деталей'}")
        if len(bullets) >= 8:
            break
    return bullets


def _extract_sweep_failures_ru(sweep_payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(sweep_payload, dict):
        return []
    bullets: list[str] = []
    for target in _sweep_cleanup_targets(sweep_payload)[:8]:
        path = target.get("path") or "unknown"
        reason = target.get("reason") or "final_sweep cleanup"
        bullets.append(f"- `{path}`: {reason}")
    summary = str(sweep_payload.get("summary") or "").strip()
    if summary and not bullets:
        bullets.append(f"- final_sweep: {summary[:360]}")
    return bullets


_HOW_TO_RUN_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?:Usage|How\s+to\s+run|Quickstart|Quick\s+start|Getting\s+started"
    r"|Запуск|Использование|Установка|Install)\b.*?$",
    re.IGNORECASE | re.MULTILINE,
)
_FENCED_CODE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)\n```", re.DOTALL)


def _extract_how_to_run_from_readme(workspace_path: Path) -> str:
    """Pull a 'how to run' block out of the workspace README, if any.

    Strategy: locate the first README.md / README.rst / README.txt,
    find a heading that looks like ``Usage`` / ``Запуск`` / etc., and
    return the first fenced code block within ~2000 chars after it.
    Falls back to a generic ``python <main.py>`` if nothing matches
    but a clear entrypoint exists. Returns ``""`` when nothing
    plausible can be inferred — caller must handle that.
    """
    if not workspace_path.is_dir():
        return ""
    for candidate in (
        "README.md",
        "README.MD",
        "Readme.md",
        "readme.md",
        "README.rst",
        "README.txt",
    ):
        readme = workspace_path / candidate
        if readme.is_file():
            try:
                text = readme.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if not text:
                continue
            match = _HOW_TO_RUN_HEADING_RE.search(text)
            if match:
                window = text[match.end() : match.end() + 2400]
                code = _FENCED_CODE_RE.search(window)
                if code:
                    block = code.group(1).strip()
                    if 5 <= len(block) <= 800:
                        return block
            # Fallback: take the first code block in the README at all.
            code = _FENCED_CODE_RE.search(text)
            if code:
                block = code.group(1).strip()
                if 5 <= len(block) <= 600 and any(
                    keyword in block.lower()
                    for keyword in (
                        "python ",
                        "uv ",
                        "pip ",
                        "npm ",
                        "node ",
                        "cargo ",
                        "go ",
                        "make ",
                    )
                ):
                    return block
            break
    # Last resort: detect a common entrypoint and synthesise a command.
    entrypoints = [
        ("main.py", "python main.py"),
        ("app.py", "python app.py"),
        ("run.py", "python run.py"),
        ("cli.py", "python cli.py"),
    ]
    for fname, command in entrypoints:
        if (workspace_path / fname).is_file():
            return command
    package_json = workspace_path / "package.json"
    if package_json.is_file():
        return "npm install && npm start"
    return ""


def _summarise_solution_idea(
    repo_root: Path,
    workspace_id: str,
    base_task_id: str,
    changes_made: list[str] | None,
) -> str:
    """Return a 1-2 sentence Russian-friendly summary of WHAT was built.

    Pulled, in priority order, from:

    1. The most recent task plan's ``objective_digest`` (truncated).
    2. The titles of completed subtasks, joined into a short narrative.
    3. The list of changed files (very last fallback).
    """
    if not workspace_id or not base_task_id:
        return ""
    payload = _load_best_plan_for_summary(repo_root, workspace_id, base_task_id)
    if isinstance(payload, dict):
        subtasks = payload.get("subtasks") or []
        done_titles: list[str] = []
        if isinstance(subtasks, list):
            for st in subtasks:
                if (
                    isinstance(st, dict)
                    and str(st.get("status") or "").lower() == "done"
                ):
                    title = str(st.get("title") or "").strip()
                    if title and len(title) <= 120:
                        done_titles.append(title)
        if done_titles:
            bullet_titles = [f"  - {t}" for t in done_titles[:8]]
            return "Шаги исходного плана:\n" + "\n".join(bullet_titles)
    if changes_made:
        kinds = {Path(p).suffix.lower() for p in changes_made if p}
        if {".py"} <= kinds:
            return "Реализован Python-проект с набором модулей и точкой входа."
        if {".ts", ".tsx", ".js", ".jsx"} & kinds:
            return "Реализован JS/TS-проект с модулями и сборкой."
        return f"Внесены изменения в {len(changes_made)} файл(ов) workspace."
    return ""


def _load_best_plan_for_summary(
    repo_root: Path,
    workspace_id: str,
    base_task_id: str,
) -> dict[str, Any] | None:
    safe = "".join(ch for ch in base_task_id if ch.isalnum() or ch in "-_") or "default"
    plans_dir = (
        repo_root / "workspaces" / workspace_id / ".memory" / "drive" / "task_plans"
    )
    candidates = [plans_dir / f"{safe}.json"]
    candidates.extend(sorted(plans_dir.glob(f"{safe}.before_remediation_*.json")))
    best: dict[str, Any] | None = None
    best_score = -1
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        subtasks = (
            payload.get("subtasks") if isinstance(payload.get("subtasks"), list) else []
        )
        done_count = sum(
            1
            for st in subtasks
            if isinstance(st, dict) and str(st.get("status") or "").lower() == "done"
        )
        score = len(subtasks) * 10 + done_count
        objective = str(payload.get("objective_digest") or "").lower()
        if "remediation" in objective or "verification" in objective:
            score -= 5
        if score > best_score:
            best = payload
            best_score = score
    return best


def _build_russian_final_summary(
    *,
    final_status: str,
    raw_message: str,
    verification_payload: dict[str, Any] | None,
    sweep_payload: dict[str, Any] | None = None,
    completion_warnings: list[str] | None = None,
    changes_made: list[str] | None = None,
    remediation_attempts_used: int = 0,
    repo_root: Path | None = None,
    workspace_id: str = "",
    base_task_id: str = "",
) -> str:
    """Build a structured Russian summary for the UI.

    Format (always present, even when empty):

        ## <Russian status label>

        ### Что реализовано    (только при verified/complete с записями)
        ...
        ### Идея решения       (только при verified/complete с записями)
        ...
        ### Как запустить      (только при verified/complete с записями)
        ...
        ### Что сделано
        ...
        ### Где проблемы
        ...
        ### Что осталось
        ...
    """
    status_label = _ru_status_label(final_status)
    lines: list[str] = [f"## {status_label}", ""]

    # Cancelled runs short-circuit: the agent didn't fail, the user
    # explicitly stopped it. Surface a clean explanation and the
    # partial work, not the verification post-mortem (it would be
    # misleading — verification was never the deciding gate).
    if final_status == "cancelled":
        cleaned_raw = (raw_message or "").strip()
        if cleaned_raw:
            lines.append(cleaned_raw)
            lines.append("")
        if changes_made:
            lines.append("### Частичные изменения")
            for path in list(changes_made)[:12]:
                lines.append(f"- `{path}`")
            if len(changes_made) > 12:
                lines.append(f"- …и ещё {len(changes_made) - 12} файл(ов)")
            lines.append("")
            lines.append(
                "Эти файлы записаны до остановки. Они НЕ прошли верификацию "
                "и НЕ промоутились — посмотри diff перед использованием."
            )
        else:
            lines.append(
                "Изменений в `workspaces/{ws}/` зафиксировано не было.".format(
                    ws=workspace_id or "workspace",
                )
            )
        if remediation_attempts_used:
            lines.append("")
            lines.append(
                f"До остановки прошло циклов remediation: "
                f"`{remediation_attempts_used}`."
            )
        return _truncate_final_message("\n".join(lines).rstrip() + "\n")

    is_success = final_status in {"verified", "complete", "completed"}
    has_writes = bool(changes_made)

    # Human-friendly preamble: when the run actually shipped something,
    # tell the operator WHAT was built, the IDEA behind it, and HOW to
    # run it. This is what a junior engineer would write in a PR
    # description; the rest of the structured report (Что сделано / Где
    # проблемы / Что осталось) is the engineering audit.
    if is_success and has_writes:
        idea = ""
        run_block = ""
        if repo_root is not None and workspace_id:
            idea = _summarise_solution_idea(
                repo_root,
                workspace_id,
                base_task_id,
                changes_made,
            )
            workspace_path = repo_root / "workspaces" / workspace_id
            run_block = _extract_how_to_run_from_readme(workspace_path)

        lines.append("### Что реализовано")
        if changes_made:
            kinds = sorted(
                {Path(p).suffix.lower() for p in changes_made if Path(p).suffix}
            )
            if any(k in kinds for k in (".py", ".ipynb")):
                kind_label = "Python-проект"
            elif any(k in kinds for k in (".ts", ".tsx", ".js", ".jsx")):
                kind_label = "JS/TS-проект"
            elif ".rs" in kinds:
                kind_label = "Rust-проект"
            elif ".go" in kinds:
                kind_label = "Go-проект"
            else:
                kind_label = "проект"
            lines.append(
                f"Реализован {kind_label} в `workspaces/{workspace_id}/`. "
                f"Записано {len(changes_made)} файл(ов), верификация пройдена."
            )
            if final_status != "verified":
                lines[-1] = (
                    f"Реализован {kind_label} в `workspaces/{workspace_id}/`. "
                    f"Записано {len(changes_made)} файл(ов), автоматическая верификация не пройдена."
                )
        else:
            lines.append("Run завершился успешно, но без изменений в файлах.")
        lines.append("")

        if idea:
            lines.append("### Идея решения")
            lines.append(idea)
            lines.append("")

        if run_block:
            lines.append("### Как запустить")
            lines.append(
                f"Скопируй и выполни в корне workspace (`workspaces/{workspace_id}/`):"
            )
            lines.append("```bash")
            lines.append(run_block)
            lines.append("```")
            lines.append("")

    lines.append("### Что сделано")
    changed_files = list(changes_made or [])
    if changed_files:
        for path in changed_files[:12]:
            lines.append(f"- `{path}`")
        if len(changed_files) > 12:
            lines.append(f"- …и ещё {len(changed_files) - 12} файл(ов)")
    else:
        lines.append("- Изменений в файлах workspace не зафиксировано.")
    if remediation_attempts_used:
        lines.append(
            f"- Циклов self-verify→fix→verify: {remediation_attempts_used} "
            "(в рамках одного run, без рестарта)."
        )
    lines.append("")

    lines.append("### Где проблемы")
    failed_bullets = _extract_failed_checks_ru(verification_payload)
    sweep_bullets = _extract_sweep_failures_ru(sweep_payload)
    skipped = bool(
        isinstance(verification_payload, dict) and verification_payload.get("skipped")
    )
    if final_status == "failed_verification" and not skipped:
        if failed_bullets:
            lines.extend(failed_bullets)
        else:
            summary = ""
            if isinstance(verification_payload, dict):
                summary = str(verification_payload.get("summary") or "").strip()
            lines.append(
                f"- Верификация не прошла. {summary or 'Подробности в verification_failure_context.md.'}"
            )
    elif final_status == "failed_hygiene":
        if sweep_bullets:
            lines.extend(sweep_bullets)
        else:
            lines.append(
                "- Runtime verification прошла, но final_sweep заблокировал run из-за workspace hygiene."
            )
    elif skipped:
        lines.append(
            "- Верификация не запускалась: в `workspace.toml` нет секции "
            "`[verification]`, и автодетект ничего не нашёл. Это не ошибка, "
            "но и не подтверждение, что код работает — добавь хотя бы одну "
            'команду в `[verification]` (например `command = "python -m pytest -q"`), '
            "чтобы harness мог проверить результат."
        )
    elif failed_bullets:
        lines.extend(failed_bullets)
    elif completion_warnings:
        # Filter the cosmetic ``verification_skipped_no_spec`` warning —
        # it is already explained right above when ``skipped=True``.
        meaningful = [
            w for w in completion_warnings if w != "verification_skipped_no_spec"
        ]
        if meaningful:
            for warning in meaningful[:6]:
                lines.append(f"- предупреждение: `{warning}`")
        else:
            lines.append("- Не обнаружено.")
    else:
        lines.append("- Не обнаружено.")
    lines.append("")

    lines.append("### Что осталось")
    if final_status == "failed_verification" and not skipped:
        lines.append(
            "- Доисправить указанные выше проверки. Контекст ошибок: "
            "`drive/memory/verification_failure_context.md`. Запусти "
            "падающую команду локально, обнови файлы, повтори self-verify."
        )
    elif final_status == "failed_hygiene":
        lines.append(
            "- Убрать или переместить указанные final_sweep cleanup targets через "
            "`delete_workspace_file` / нормальную структуру `src/`, `tests/`, `docs/`, "
            "затем повторить verification."
        )
    elif final_status == "incomplete":
        lines.append(
            "- Run завершился без записи в workspace. Дай агенту повторный "
            "запуск или явно укажи, какие файлы создать."
        )
    elif final_status == "incomplete_subtasks":
        lines.append(
            "- В плане остались required-сабтаски в статусе `failed` — "
            "верификация прошла на текущем срезе, но обещанная работа не "
            "сделана. Перезапусти run или вручную закрой подзадачи через "
            "remediation, прежде чем промоутить."
        )
    elif final_status == "phase_impasse":
        lines.append(
            "- Сработал circuit breaker `phase_impasse`: агент повторно "
            "падал на одном и том же completion-инструменте. Смотри "
            "`drive/state/phase_impasse.json` — нужно поменять стратегию "
            "(перепланировать сабтаски / обновить evidence) и перезапустить."
        )
    elif final_status == "failed_self_review":
        lines.append(
            "- Self-review агент сломал контракт ответа (LGTM / NEEDS_FIX). "
            "Без структурного вердикта harness не может запустить ещё один "
            "цикл remediation — нужно вручную пересмотреть результат."
        )
    elif final_status == "incomplete_discovery":
        lines.append(
            "- Workspace помечен активным доменом (например `multi_agent_gmas`), "
            "но агент за весь run ни разу не вызвал `deep_search` / "
            "`github_project_search` / `github_extract_snippets` / "
            "`mcp_discover` / `web_fetch`. Без discovery такие задачи "
            "приниматься не должны — перезапусти и заставь агента провести "
            "поиск перед началом реализации."
        )
    elif skipped:
        lines.append(
            "- Запусти результат руками (открой получившиеся файлы, "
            "выполни нужный сценарий) — автоматического gate'а нет."
        )
        lines.append(
            "- Когда выберешь критерий приёмки, добавь его в "
            "`[verification]` секцию `workspace.toml`, тогда следующий "
            "run сможет сам подтвердить готовность."
        )
    elif completion_warnings and any(
        w != "verification_skipped_no_spec" for w in completion_warnings
    ):
        lines.append(
            "- Верификация пройдена, но есть предупреждения "
            "(см. раздел выше) — стоит проверить вручную перед промоутом."
        )
    else:
        lines.append("- Ничего критичного. Можно отправлять в production / promote.")
    lines.append("")

    cleaned_raw = (raw_message or "").strip()
    if cleaned_raw and not _looks_like_llm_empty_response(cleaned_raw):
        excerpt = cleaned_raw
        if len(excerpt) > 1200:
            excerpt = excerpt[:1200].rstrip() + "\n…[обрезано]"
        lines.extend(["### Краткое сообщение агента", excerpt, ""])

    return _truncate_final_message("\n".join(lines).rstrip() + "\n")


def _normalize_final_message_for_status(
    *,
    final_status: str,
    final_message: str,
    verification_payload: dict[str, Any] | None,
    sweep_payload: dict[str, Any] | None = None,
    completion_warnings: list[str] | None = None,
    changes_made: list[str] | None = None,
    remediation_attempts_used: int = 0,
    repo_root: Path | None = None,
    workspace_id: str = "",
    base_task_id: str = "",
) -> str:
    """Always return a structured Russian summary so the UI shows a
    truthful, consistent report regardless of what the last LLM turn
    happened to print."""
    return _build_russian_final_summary(
        final_status=final_status,
        raw_message=final_message,
        verification_payload=verification_payload,
        sweep_payload=sweep_payload,
        completion_warnings=completion_warnings,
        changes_made=changes_made,
        remediation_attempts_used=remediation_attempts_used,
        repo_root=repo_root,
        workspace_id=workspace_id,
        base_task_id=base_task_id,
    )


def _resolve_final_status(
    *,
    verification_payload: dict[str, Any] | None,
    critic_payload: dict[str, Any] | None = None,
    failed_required_subtask: bool,
    no_writes: bool,
    sweep_payload: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Resolve terminal status. Verification + final sweep are the hard gates."""
    del critic_payload
    warnings: list[str] = []
    if verification_payload is None:
        return ("incomplete" if no_writes else "complete"), warnings
    # ``skipped=True`` means the workspace has no verification spec
    # declared (no ``[verification]`` section in ``workspace.toml`` and
    # nothing auto-detectable). In delivery mode that is a fixable gate
    # failure: the remediation loop should add a real smoke step rather
    # than letting a run finish green without proof.
    if verification_payload.get("skipped"):
        warnings.append("verification_skipped_no_spec")
        if no_writes:
            return "incomplete", warnings
        return "failed_verification", warnings
    if not verification_payload.get("passed"):
        return "failed_verification", warnings

    if isinstance(sweep_payload, dict):
        if sweep_payload.get("missing_required"):
            try:
                from umbrella.verification.final_sweep import (
                    is_runtime_optional_required_path,
                )
            except Exception:
                is_runtime_optional_required_path = lambda _: False  # type: ignore[assignment]
            missing_required = [
                str(p) for p in (sweep_payload.get("missing_required") or [])
            ]
            hard_missing = [
                p for p in missing_required if not is_runtime_optional_required_path(p)
            ]
            soft_missing = [
                p for p in missing_required if is_runtime_optional_required_path(p)
            ]
            if soft_missing:
                warnings.append("sweep_missing_runtime_optional")
            if hard_missing:
                warnings.append("sweep_missing_required")
                return "failed_hygiene", warnings
        if sweep_payload.get("leftover_noise"):
            warnings.append("sweep_leftover_noise")
        if sweep_payload.get("removed"):
            warnings.append("sweep_auto_cleaned")
        # Tier 4.1 — block-level noise (ad-hoc scripts / extraction
        # artifacts / handoff docs at the workspace root) is now a hard
        # gate. Auto-clean removes the files but the agent still
        # "passes" verification, so the only way to fix the source
        # behaviour is to fail the run and force remediation. Severity
        # comes from the sweep itself; no per-workspace tuning here.
        blocking_noise = sweep_payload.get("blocking_noise")
        if isinstance(blocking_noise, list) and blocking_noise:
            warnings.append("sweep_blocking_noise")
            return "failed_hygiene", warnings

    if failed_required_subtask:
        warnings.append("failed_required_subtask")
        return "incomplete_subtasks", warnings
    return "verified", warnings


_DELIVERY_CONTRACT_FAIL_STATUSES: frozenset[str] = frozenset(
    {
        "phase_impasse",
        "failed_self_review",
        "incomplete_subtasks",
        "incomplete_discovery",
    }
)


def _phase_impasse_active(drive_root: Path, task_id: str) -> dict[str, Any] | None:
    """Return the persisted ``phase_impasse.json`` payload for ``task_id``.

    The loop writes ``state/phase_impasse.json`` whenever a completion tool
    keeps failing with the same control-plane error. The control plane
    treats that artifact as a hard delivery-contract failure: there is no
    point in re-running the same remediation cycle, the agent must
    re-plan or escalate. ``None`` means no impasse for this run.
    """

    path = drive_root / "state" / "phase_impasse.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    impasse_task_id = str(payload.get("task_id") or "")
    if impasse_task_id and task_id and impasse_task_id != task_id:
        return None
    return payload


def _has_required_external_discovery(
    *,
    quality_telemetry: dict[str, Any] | None,
) -> bool:
    """The agent must consult external sources when active domains apply.

    For multi-agent / GMAS / similar product-class tasks Umbrella stamps
    the active domain into ``quality_telemetry``. The delivery gate then
    requires at least one external discovery call (``deep_search`` /
    ``github_project_search`` / ``github_extract_snippets`` /
    ``mcp_discover`` / ``web_fetch``) before the run is allowed to claim
    ``verified``. This closes the "agent didn't search anything" gap that
    otherwise lets thin scaffolds pass shallow verification.
    """

    if not isinstance(quality_telemetry, dict):
        return True
    active_domains = quality_telemetry.get("active_domains") or []
    if not isinstance(active_domains, list) or not active_domains:
        return True
    if not bool(quality_telemetry.get("missing_external_discovery_warning")):
        return True
    counts = quality_telemetry.get("external_discovery_tool_calls") or {}
    if isinstance(counts, dict):
        try:
            return any(int(value) > 0 for value in counts.values())
        except (TypeError, ValueError):
            return True
    return True


def _apply_delivery_contract_gate(
    *,
    final_status: str,
    completion_warnings: list[str],
    quality_telemetry: dict[str, Any] | None,
    drive_root: Path,
    task_id: str,
) -> tuple[str, list[str]]:
    """Downgrade ``verified`` to a delivery-contract failure when needed.

    ``_resolve_final_status`` only inspects verification + final_sweep
    payloads. That is not enough for a deep agent: the run can be
    structurally invalid even when those gates are green — for example
    when the planner failed a required subtask, the self-review contract
    broke, the completion-tool circuit breaker tripped (``phase_impasse``
    artifact), or no external discovery was attempted on a product-class
    task. Each of those signals is recorded elsewhere; this gate ties
    them back to the terminal status so the web UI and promotion logic
    see the truth instead of a misleading ``verified``.
    """

    warnings = list(completion_warnings or [])
    if final_status != "verified":
        return final_status, warnings

    impasse = _phase_impasse_active(drive_root, task_id)
    if impasse is not None:
        if "phase_impasse" not in warnings:
            warnings.append("phase_impasse")
        return "phase_impasse", warnings

    if "self_review_contract_failed" in warnings:
        return "failed_self_review", warnings

    if not _has_required_external_discovery(quality_telemetry=quality_telemetry):
        if "missing_external_discovery" not in warnings:
            warnings.append("missing_external_discovery")
        return "incomplete_discovery", warnings

    return final_status, warnings


def _canonical_drive_root(repo_root: Path, workspace_id: str | None = None) -> Path:
    from umbrella.integration.ouroboros_launcher import resolve_drive_root

    drive_root = resolve_drive_root(repo_root.resolve(), workspace_id)
    drive_root.mkdir(parents=True, exist_ok=True)
    return drive_root


def _submit_launcher_task(repo_root: Path, task: dict[str, Any]) -> tuple[str, Any]:
    from umbrella.integration.ouroboros_launcher import get_launcher

    launcher = get_launcher(repo_root.resolve())
    launcher.start()
    task_id = launcher.submit_task(task)
    return task_id, launcher


def create_ouroboros_self_improvement_task(
    *,
    repo_root: Path,
    issue_description: str,
    context: str,
    workspace_id: str,
    max_iterations: int = 3,
) -> dict[str, Any]:
    """Create a self-improvement task for Ouroboros to work on.

    This is different from regular code updates - it asks Ouroboros to analyze
    the issue, explore solutions, and make improvements to the workspace or
    even to Umbrella itself.

    Args:
        repo_root: Repository root
        issue_description: What needs to be improved
        context: Additional context about the issue
        workspace_id: Relevant workspace ID
        max_iterations: Maximum iterations for Ouroboros to try

    Returns:
        Dict with task_id and status
    """
    try:
        from ouroboros.utils import utc_now_iso, append_jsonl
    except ImportError:
        log.warning("Ouroboros utils not available")
        return {"status": "unavailable", "error": "ouroboros_utils_not_importable"}

    task_id = f"self_improve_{uuid.uuid4().hex[:8]}"

    repo_root = repo_root.resolve()
    drive_root = _canonical_drive_root(repo_root, workspace_id)

    task_input = f"""# Self-Improvement Task

**Task ID**: {task_id}
**Workspace**: {workspace_id}
**Created**: {utc_now_iso()}

## Issue

{issue_description}

## Context

{context}

## Instructions

1. Use repo_read to examine the relevant workspace files
2. Identify the root cause of the issue
3. Propose and implement a fix
4. Use repo_write_commit or commit_workspace_changes to persist local commits only
5. Test the fix if possible

**Constraints**:
- Make minimal, targeted changes
- Preserve existing functionality
- Create backups before modifying
- Document your changes

**Max iterations**: {max_iterations}
"""

    task_data = {
        "id": task_id,
        "type": "self_improvement",
        "input": task_input,
        "workspace_id": workspace_id,
        "max_iterations": max_iterations,
        "depth": 1,  # Top-level task
        "created_at": time.time(),
    }

    try:
        task_id, _launcher = _submit_launcher_task(repo_root, task_data)
    except ImportError:
        log.warning("Ouroboros launcher not available for self-improvement delegation")
        return {"status": "unavailable", "error": "ouroboros_launcher_not_importable"}

    # Log the task creation
    append_jsonl(
        drive_root / "logs" / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "self_improvement_task_created",
            "task_id": task_id,
            "workspace_id": workspace_id,
            "issue": issue_description[:200],
        },
    )

    log.info("Queued Ouroboros self-improvement task via launcher: %s", task_id)

    return {
        "status": "delegated",
        "task_id": task_id,
        "task_file": None,
        "transport": "launcher",
        "workspace_id": workspace_id,
    }


def _try_create_instance(
    repo_root: Path, workspace_id: str, task_id: str
) -> Path | None:
    """Create a task instance from a seed workspace, logging failures loudly."""
    try:
        from umbrella.workspace_registry.discovery import load_seed_profile
        from umbrella.workspace_runtime.instances import create_task_instance
        from umbrella.workspace_registry.models import TaskBrief

        seed_path = repo_root / "workspaces" / workspace_id
        seed_profile = load_seed_profile(seed_path)
        if seed_profile is None:
            log.warning(
                "Instance creation unavailable for workspace=%s: missing seed_profile.toml at %s",
                workspace_id,
                seed_path,
            )
            return None

        task_brief = TaskBrief(
            task_id=task_id,
            description=f"Ouroboros sync improvement task {task_id}",
            task_class="improvement",
            domains=[],
        )

        instance = create_task_instance(seed_profile, task_brief, task_id=task_id)
        log.info("Created task instance %s at %s", instance.instance_id, instance.path)
        return instance.path
    except Exception as exc:
        log.warning(
            "Instance creation failed for workspace=%s task_id=%s: %s",
            workspace_id,
            task_id,
            exc,
            exc_info=True,
        )
        return None


def _safe_task_segment(task_id: str) -> str:
    return (
        "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)[:120]
        or uuid.uuid4().hex
    )


def _prepare_candidate_workspace(
    repo_root: Path, workspace_id: str, task_id: str
) -> Path | None:
    """Copy a seed workspace into a harness-private candidate workspace."""
    source = (repo_root / "workspaces" / workspace_id).resolve()
    if not source.exists() or not source.is_dir():
        return None
    root = (repo_root / ".umbrella" / "meta_harness" / "workspaces").resolve()
    target = (root / _safe_task_segment(task_id)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"unsafe candidate workspace path: {target}")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".umbrella_scratch",
            ".stdout.txt",
            ".stderr.txt",
        ),
    )
    return target


def _iter_workspace_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    if not root.exists():
        return files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = set(path.relative_to(root).parts)
        if "__pycache__" in parts or ".umbrella_scratch" in parts:
            continue
        if rel.endswith(".pyc") or rel in {".stdout.txt", ".stderr.txt"}:
            continue
        # Runtime logs are shared visibility noise, not candidate deliverables.
        if rel.startswith(".memory/drive/logs/"):
            continue
        files[rel] = path
    return files


def _same_file(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        import hashlib

        def digest(path: Path) -> str:
            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()

        return digest(left) == digest(right)
    except OSError:
        return False


def _collect_candidate_workspace_changes(
    repo_root: Path,
    workspace_id: str,
    candidate_workspace_path: Path | None,
) -> list[str]:
    """Return live-workspace paths changed in a harness candidate copy."""
    if candidate_workspace_path is None:
        return []
    seed = (repo_root / "workspaces" / workspace_id).resolve()
    candidate = candidate_workspace_path.resolve()
    seed_files = _iter_workspace_files(seed)
    candidate_files = _iter_workspace_files(candidate)
    changed: list[str] = []
    for rel, candidate_file in sorted(candidate_files.items()):
        seed_file = seed_files.get(rel)
        if seed_file is None or not _same_file(seed_file, candidate_file):
            changed.append(f"workspaces/{workspace_id}/{rel}")
    for rel in sorted(set(seed_files) - set(candidate_files)):
        changed.append(f"workspaces/{workspace_id}/{rel}")
    return changed


def _record_competency_signals(
    result: dict[str, Any],
    workspace_id: str,
    task_id: str,
) -> None:
    """Record competency signals from the Ouroboros run result."""
    try:
        from umbrella.memory.competency import record_competency_signal
        from umbrella.memory.store import MemoryStore
        from umbrella.memory.models import SignalCategory

        store = MemoryStore()

        status = result.get("status", "unknown")
        tool_calls = int(result.get("llm_tool_invocations", 0))
        write_calls = int(result.get("workspace_write_tool_calls", 0))
        verification_passed = bool(result.get("verification_passed"))
        verification_skipped = bool(result.get("verification_skipped"))
        verification_report_present = (
            "verification_passed" in result
            or "verification_skipped" in result
            or isinstance(result.get("verification_report"), dict)
        )
        promotion_blocked_reason = str(result.get("promotion_blocked_reason") or "")

        if status == "verified":
            record_competency_signal(
                store=store,
                category=SignalCategory.NO_PROGRESS_ITERATIONS,
                capability_area="workspace_improvement",
                strength=0.7,
                evidence_summary=(
                    f"Verified run: {write_calls} write calls, {tool_calls} tool calls, "
                    "runtime verification passed"
                ),
                task_id=task_id,
                workspace_id=workspace_id,
            )
        elif (
            status == "failed_verification"
            or (
                status == "complete"
                and verification_report_present
                and (verification_skipped or not verification_passed)
            )
            or promotion_blocked_reason == "verification_failed"
        ):
            record_competency_signal(
                store=store,
                category=SignalCategory.REPEATED_FAILURE_MODE,
                capability_area="workspace_improvement",
                strength=-0.6,
                evidence_summary=(
                    f"Self-reported complete but verification did not pass: "
                    f"{write_calls} write calls, {tool_calls} tool calls"
                ),
                task_id=task_id,
                workspace_id=workspace_id,
            )
        elif status == "complete" and write_calls > 0:
            record_competency_signal(
                store=store,
                category=SignalCategory.NO_PROGRESS_ITERATIONS,
                capability_area="workspace_improvement",
                strength=0.5,
                evidence_summary=f"Successful run: {write_calls} write calls, {tool_calls} total tool calls",
                task_id=task_id,
                workspace_id=workspace_id,
            )
        elif status == "incomplete":
            record_competency_signal(
                store=store,
                category=SignalCategory.NO_PROGRESS_ITERATIONS,
                capability_area="workspace_improvement",
                strength=-0.4,
                evidence_summary="Incomplete: no tool invocations despite task submission",
                task_id=task_id,
                workspace_id=workspace_id,
            )
        elif status == "error":
            error_text = str(result.get("error", ""))[:200]
            record_competency_signal(
                store=store,
                category=SignalCategory.REPEATED_FAILURE_MODE,
                capability_area="agent_stability",
                strength=-0.6,
                evidence_summary=f"Error during run: {error_text}",
                task_id=task_id,
                workspace_id=workspace_id,
            )

        if (
            status in ("complete", "verified", "failed_verification")
            and tool_calls > 0
            and write_calls == 0
        ):
            record_competency_signal(
                store=store,
                category=SignalCategory.HIGH_COST_NO_GAIN,
                capability_area="task_execution",
                strength=-0.3,
                evidence_summary=f"{tool_calls} tool calls but 0 workspace writes",
                task_id=task_id,
                workspace_id=workspace_id,
            )
    except Exception:
        log.debug("Competency signal recording failed (non-fatal)", exc_info=True)


def _capture_candidate_safe(
    *,
    repo_root: Path,
    task_id: str,
    workspace_id: str,
    task_description: str = "",
    instance_path: Path | None = None,
    events: list[dict[str, Any]] | None = None,
    changes_made: list[str] | None = None,
    promoted_files: list[str] | None = None,
    run_status: str = "",
    error: str = "",
    llm_tool_invocations: int = 0,
    workspace_write_tool_calls: int = 0,
    final_message: str = "",
    result_dict: dict[str, Any] | None = None,
    experiment_id: str = "",
    candidate_diff: str = "",
    baseline_sha: str = "",
    verification_report: dict[str, Any] | None = None,
) -> None:
    """Best-effort candidate capture; never blocks the main flow."""
    try:
        from umbrella.meta_harness.capture import capture_ouroboros_candidate

        candidate = capture_ouroboros_candidate(
            repo_root=repo_root,
            task_id=task_id,
            workspace_id=workspace_id,
            task_description=task_description,
            instance_path=instance_path,
            events=events,
            changes_made=changes_made,
            promoted_files=promoted_files,
            llm_tool_invocations=llm_tool_invocations,
            workspace_write_tool_calls=workspace_write_tool_calls,
            final_message=final_message,
            run_status=run_status,
            error=error,
            experiment_id=experiment_id,
            candidate_diff=candidate_diff,
            baseline_sha=baseline_sha,
            verification_report=verification_report,
        )
        if result_dict is not None:
            result_dict["candidate_id"] = candidate.candidate_id
            result_dict["candidate_manifest_path"] = str(
                repo_root
                / ".umbrella"
                / "meta_harness"
                / "experiments"
                / (candidate.experiment_id or "_default")
                / "candidates"
                / candidate.candidate_id
                / "manifest.json"
            )
    except Exception:
        log.debug("Meta-harness candidate capture failed (non-fatal)", exc_info=True)


def _run_workspace_verification(
    repo_root: Path,
    workspace_id: str,
    instance_path: Path | None,
    *,
    overall_timeout_seconds: int | None = None,
    changed_files: list[str] | None = None,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """Run runtime verification against the workspace or its task instance.

    Returns a ``VerificationReport.to_dict()`` serialisation, or ``None`` if
    verification was skipped (no spec resolvable, no workspace on disk).
    """
    try:
        from umbrella.verification import (
            VerificationSpecError,
            load_verification_spec,
            run_verification,
        )
    except Exception:  # noqa: BLE001 - never block main flow
        log.debug("Verification module unavailable (non-fatal)", exc_info=True)
        return None

    target = instance_path or (repo_root / "workspaces" / workspace_id)
    if not target.exists():
        log.debug("Verification skipped: target path %s does not exist", target)
        return None

    try:
        steps = load_verification_spec(target)
    except VerificationSpecError as exc:
        message = str(exc)
        return {
            "workspace_id": workspace_id,
            "workspace_path": str(target),
            "passed": False,
            "pass_rate": 0.0,
            "results": [
                {
                    "name": "verification_spec:parse",
                    "kind": "source_policy",
                    "status": "failed",
                    "exit_code": None,
                    "duration_seconds": 0.0,
                    "summary": message,
                    "stdout_tail": "",
                    "stderr_tail": "",
                    "error": "verification_spec_error",
                    "optional": False,
                    "request_payload_count": 0,
                }
            ],
            "summary": (
                "Verification spec is invalid and must be repaired before checks can run.\n"
                f"{message}\n\n"
                "Fix `workspace.toml` / `verification.toml` syntax first. On Windows, "
                "use forward slashes (`C:/path/...`) or escaped backslashes (`C:\\\\path`) "
                "inside TOML double-quoted strings."
            ),
            "error": "verification_spec_error",
            "spec_error": exc.to_payload(),
            "repairable": True,
        }
    except Exception:  # noqa: BLE001
        log.debug("load_verification_spec failed (non-fatal)", exc_info=True)
        return None

    if not steps:
        log.debug("No verification steps for %s", target)
        return {
            "workspace_id": workspace_id,
            "workspace_path": str(target),
            "passed": False,
            "pass_rate": 0.0,
            "results": [],
            "summary": "No verification steps declared or auto-detected.",
            "skipped": True,
        }

    try:
        report = run_verification(
            target,
            steps,
            workspace_id=workspace_id,
            overall_timeout_seconds=overall_timeout_seconds or 900,
            changed_files=changed_files,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Verification run raised: %s", exc, exc_info=True)
        return {
            "workspace_id": workspace_id,
            "workspace_path": str(target),
            "passed": False,
            "pass_rate": 0.0,
            "results": [],
            "summary": f"Verification runner crashed: {exc}",
            "error": str(exc),
        }

    payload = report.to_dict()
    payload["summary"] = report.render_summary()
    # NOTE: the synthetic ``skill_compliance:multi_agent_gmas_context_before_write``
    # gate that used to be appended here was removed. Forcing the agent to
    # call ``get_gmas_context`` before any workspace write created a
    # cargo-cult loop where the agent added fake GMAS calls just to pass
    # the gate even when the task wasn't multi-agent. GMAS retrieval is
    # now optional — the tools (``get_gmas_context``,
    # ``search_gmas_knowledge``) remain available for the agent to use
    # voluntarily, but verification no longer requires them.
    return payload


def _read_tool_history(drive_root: Path, *, task_id: str | None = None) -> list[str]:
    """Return ordered tool names from ``tools.jsonl``.

    When ``task_id`` is provided, only events that belong to the current
    attempt are kept. This is critical for retry isolation: each web-bridge
    retry uses a fresh ``task_id`` (``run_id`` / ``run_id__a2`` / ...), and
    mixing them would let writes from a botched attempt 1 poison the
    "context-before-first-write" check of attempt N where the agent is
    actually compliant.
    """
    tools_path = drive_root / "logs" / "tools.jsonl"
    tools: list[str] = []
    try:
        for line in tools_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if task_id:
                event_task = str(event.get("task_id") or "")
                if event_task and event_task != task_id:
                    continue
            name = str(event.get("tool") or "").strip()
            if name:
                tools.append(name)
    except OSError:
        return []
    return tools


def _append_required_result(
    payload: dict[str, Any],
    *,
    name: str,
    kind: str,
    passed: bool,
    summary: str,
    error: str = "",
) -> None:
    result = {
        "name": name,
        "kind": kind,
        "status": "passed" if passed else "failed",
        "exit_code": None,
        "duration_seconds": 0.0,
        "summary": summary,
        "stdout_tail": "",
        "stderr_tail": "",
        "error": error,
        "optional": False,
        "request_payload_count": 0,
    }
    results = payload.setdefault("results", [])
    if isinstance(results, list):
        results.append(result)
    _recompute_verification_payload(payload)


def _recompute_verification_payload(payload: dict[str, Any]) -> None:
    results = [r for r in payload.get("results") or [] if isinstance(r, dict)]
    required = [r for r in results if not r.get("optional")]
    passed_count = sum(1 for r in required if r.get("status") == "passed")
    payload["passed"] = bool(required) and passed_count == len(required)
    payload["pass_rate"] = round(passed_count / len(required), 3) if required else 0.0
    status_label = "PASS" if payload["passed"] else "FAIL"
    lines = [
        f"Verification: **{status_label}** ({passed_count}/{len(required)} required steps passed)"
    ]
    for result in results:
        tag = "[optional]" if result.get("optional") else "[required]"
        icon = (
            "ok" if result.get("status") == "passed" else result.get("status", "failed")
        )
        lines.append(f"- {tag} `{result.get('name')}` ({result.get('kind')}) -> {icon}")
        if result.get("summary"):
            lines.append(f"  {result.get('summary')}")
        if result.get("error") and result.get("status") != "passed":
            lines.append(f"  error: {result.get('error')}")
    payload["summary"] = "\n".join(lines)[:4000]


def _collect_run_quality_telemetry(
    *,
    repo_root: Path,
    workspace_id: str,
    critic_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del critic_payload
    drive_root = _canonical_drive_root(repo_root, workspace_id)
    events = _read_jsonl(drive_root / "logs" / "events.jsonl")
    active_domains = sorted(_load_active_domains(repo_root, workspace_id))
    models = sorted(
        {
            str(event.get("model"))
            for event in events
            if event.get("type") == "llm_round" and event.get("model")
        }
    )
    phases = [
        str(event.get("phase"))
        for event in events
        if event.get("type") == "llm_round" and event.get("phase")
    ]
    repair_events = [
        event for event in events if event.get("type") == "tool_args_repair"
    ]
    repaired_count = sum(1 for event in repair_events if event.get("repaired") is True)
    unrepairable_count = sum(
        1 for event in repair_events if event.get("repaired") is False
    )
    tool_names = [
        str(event.get("tool") or event.get("tool_name") or "")
        for event in events
        if event.get("type") == "tool_call"
    ]
    external_discovery_tools = {
        "deep_search",
        "github_project_search",
        "github_extract_snippets",
        "mcp_discover",
        "web_fetch",
    }
    external_discovery_counts = {
        name: sum(1 for tool in tool_names if tool == name)
        for name in sorted(external_discovery_tools)
    }
    external_discovery_total = sum(external_discovery_counts.values())
    llm_rounds = sum(1 for event in events if event.get("type") == "llm_round")
    workspace_write_events = [
        event for event in events if event.get("type") == "workspace_write_tools"
    ]
    workspace_write_count = sum(
        int(event.get("count") or 0) for event in workspace_write_events
    )
    gmas_context_path = drive_root / "memory" / "knowledge" / "gmas_active_context.md"
    nontrivial_run = llm_rounds >= 20 or workspace_write_count > 0
    telemetry = {
        "active_domains": active_domains,
        "gmas_context_present": gmas_context_path.exists(),
        "models": models,
        "phases": list(dict.fromkeys(phases)),
        "llm_rounds": llm_rounds,
        "workspace_write_tool_calls": workspace_write_count,
        "external_discovery_tool_calls": external_discovery_counts,
        "external_discovery_total": external_discovery_total,
        "missing_external_discovery_warning": bool(
            nontrivial_run and external_discovery_total == 0
        ),
        "tool_arg_repairs": repaired_count,
        "tool_arg_unrepairable": unrepairable_count,
        "degraded_tool_call_quality": unrepairable_count > 0,
    }
    return telemetry


def _load_active_domains(repo_root: Path, workspace_id: str) -> set[str]:
    try:
        from umbrella.orchestration.ouroboros_task import load_detected_domains

        return load_detected_domains(repo_root, workspace_id)
    except Exception:
        return set()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    except OSError:
        pass
    return records


def _persist_run_quality_telemetry(drive_root: Path, telemetry: dict[str, Any]) -> None:
    try:
        path = drive_root / "task_results" / "run_quality.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(telemetry, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        log.debug("Failed to persist run quality telemetry", exc_info=True)


def _persist_final_gate_report(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    final_status: str,
    verification_payload: dict[str, Any] | None,
    critic_payload: dict[str, Any] | None = None,
    completion_warnings: list[str],
    sweep_payload: dict[str, Any] | None = None,
) -> str:
    del critic_payload
    drive_root = _canonical_drive_root(repo_root, workspace_id)
    out_dir = drive_root / "task_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{task_id}.verification.md"
    verification_summary = ""
    if isinstance(verification_payload, dict):
        verification_summary = str(verification_payload.get("summary") or "").strip()
    lines = [
        "# Verification Report",
        "",
        f"- task_id: `{task_id}`",
        f"- workspace_id: `{workspace_id}`",
        f"- final_status: `{final_status}`",
        f"- verification_passed: `{bool(verification_payload and verification_payload.get('passed'))}`",
        "",
    ]
    if completion_warnings:
        lines.append("## Completion Warnings")
        for item in completion_warnings:
            lines.append(f"- {item}")
        lines.append("")
    if verification_summary:
        lines.append("## Verification Summary")
        lines.append(verification_summary)
        lines.append("")
    sweep_summary = ""
    if isinstance(sweep_payload, dict):
        sweep_summary = str(sweep_payload.get("summary") or "").strip()
    if sweep_summary or _sweep_cleanup_targets(sweep_payload):
        lines.append("## Final Sweep")
        if sweep_summary:
            lines.append(sweep_summary)
            lines.append("")
        for target in _sweep_cleanup_targets(sweep_payload)[:40]:
            lines.append(f"- `{target.get('path')}`: {target.get('reason')}")
        lines.append("")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return str(path)


def _persist_canonical_task_result(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    result: dict[str, Any],
) -> str:
    """Overwrite the launcher task_result with the integration's final verdict.

    The raw Ouroboros agent writes ``task_results/<id>.json`` as soon as the
    LLM loop emits a final text response. In long remediation runs that text
    can be a malformed pseudo-tool-call or an intermediate summary, while the
    integration later has the real final status, verification report, and
    normalized final message. Persist the canonical integration result last so
    the dashboard, task-result list, and web_runs all agree.
    """
    if not task_id:
        return ""
    try:
        out_dir = _canonical_drive_root(repo_root, workspace_id) / "task_results"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{task_id}.json"
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}

        canonical_text = _truncate_final_message(
            result.get("final_message")
            or result.get("error")
            or result.get("status")
            or ""
        )
        previous_raw = str(existing.get("result") or "").strip()
        payload = dict(existing)
        payload.update(
            {
                "task_id": task_id,
                "status": str(
                    result.get("status") or existing.get("status") or "unknown"
                ),
                "workspace_id": workspace_id,
                "result": canonical_text,
                "final_message": canonical_text,
                "result_preview": _truncate_final_message(canonical_text, 600),
                "verification_passed": bool(result.get("verification_passed")),
                "verification_skipped": bool(result.get("verification_skipped")),
                "verification_remediation_attempts_used": result.get(
                    "verification_remediation_attempts_used"
                ),
                "verification_remediation_max": result.get(
                    "verification_remediation_max"
                ),
                "promotion_blocked_reason": result.get("promotion_blocked_reason"),
                "ts": _iso_now(),
            }
        )
        if previous_raw and previous_raw != canonical_text:
            payload["agent_raw_result"] = previous_raw[:4000]
        for key in (
            "changes_made",
            "promoted_files",
            "completion_warnings",
            "quality_telemetry",
            "final_gate_report_path",
            "verification_failure_context_path",
            "internal_task_ids",
        ):
            if key in result:
                payload[key] = result.get(key)
        if isinstance(result.get("verification_report"), dict):
            report = result["verification_report"]
            payload["verification"] = {
                "passed": report.get("passed"),
                "pass_rate": report.get("pass_rate"),
                "skipped": report.get("skipped"),
                "summary": report.get("summary"),
            }

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)
        return str(path)
    except Exception:
        log.debug("Failed to persist canonical task result", exc_info=True)
        return ""


def _plan_has_failed_required_subtask(drive_root: Path, task_id: str) -> bool:
    safe = (
        "".join(ch for ch in (task_id or "default") if ch.isalnum() or ch in "-_")
        or "default"
    )
    path = drive_root / "task_plans" / f"{safe}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    for subtask in payload.get("subtasks") or []:
        if not isinstance(subtask, dict):
            continue
        if str(subtask.get("status") or "").lower() == "failed":
            return True
    return False


def _try_promote_changes(
    repo_root: Path,
    workspace_id: str,
    changes_made: list[str],
    instance_path: Path | None,
) -> list[str]:
    """Best-effort promotion of changes back to the seed workspace."""
    if not changes_made or instance_path is None:
        return []
    try:
        from umbrella.evals.promotion import promote_changed_files_to_seed

        seed_path = repo_root / "workspaces" / workspace_id
        promoted = promote_changed_files_to_seed(
            seed_path=seed_path,
            instance_path=instance_path,
            changed_files=[Path(f) for f in changes_made],
        )
        if promoted:
            log.info("Promoted %d file(s) from instance to seed", len(promoted))
        return [str(p) for p in promoted]
    except Exception:
        log.debug("Promotion failed (non-fatal)", exc_info=True)
        return []


def _run_launcher_task_once(
    *,
    repo_root: Path,
    task: dict[str, Any],
    timeout_seconds: float | None,
) -> tuple[str, dict[str, Any]]:
    task_id, launcher = _submit_launcher_task(repo_root, task)
    wait_timeout = None
    if timeout_seconds is not None:
        wait_timeout = float(timeout_seconds) + max(
            15.0, min(120.0, float(timeout_seconds) * 0.25)
        )
    result = launcher.wait_for_result(task_id, timeout=wait_timeout)
    if result is None:
        try:
            launcher.stop()
        except Exception:
            log.debug("Failed to stop Ouroboros launcher after timeout", exc_info=True)
        return task_id, {
            "status": "error",
            "error": "timeout waiting for Ouroboros launcher result",
            "task_id": task_id,
        }
    if result.get("status") == "error":
        return task_id, {
            "status": "error",
            "error": result.get("error", "unknown launcher error"),
            "task_id": task_id,
        }
    return task_id, result


def _build_initial_ouroboros_task(
    *,
    repo_root: Path,
    task_id: str,
    task_description: str,
    workspace_id: str,
    use_live_llm: bool,
    timeout_seconds: float | None,
    candidate_isolation: bool,
    candidate_workspace_path: Path | None,
    instance_path: Path | None,
) -> dict[str, Any]:
    task = _build_initial_ouroboros_task(
        repo_root=repo_root,
        task_id=task_id,
        task_description=task_description,
        workspace_id=workspace_id,
        use_live_llm=use_live_llm,
        timeout_seconds=timeout_seconds,
        candidate_isolation=candidate_isolation,
        candidate_workspace_path=candidate_workspace_path,
        instance_path=instance_path,
    )
    return task


def _maybe_run_final_sweep(
    *,
    repo_root: Path,
    workspace_id: str,
    instance_path: Path | None,
    verification_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if (
        not verification_payload
        or not verification_payload.get("passed")
        or verification_payload.get("skipped")
    ):
        return None
    try:
        from umbrella.verification.final_sweep import run_workspace_sweep

        sweep_target = instance_path or (repo_root / "workspaces" / workspace_id)
        if not sweep_target.exists():
            return None
        sweep_payload = run_workspace_sweep(sweep_target, auto_clean=False).to_dict()
        if sweep_payload.get("removed"):
            log.info(
                "Final sweep removed %d noise file(s) in %s: %s",
                len(sweep_payload["removed"]),
                sweep_target,
                ", ".join(sweep_payload["removed"]),
            )
        blocking_noise = sweep_payload.get("blocking_noise") or []
        if blocking_noise:
            log.warning(
                "Final sweep found blocking noise in %s: %s",
                sweep_target,
                ", ".join(str(item.get("path") or item) for item in blocking_noise),
            )
        if sweep_payload.get("missing_required"):
            log.warning(
                "Final sweep: missing required files in %s: %s",
                sweep_target,
                ", ".join(sweep_payload["missing_required"]),
            )
        return sweep_payload
    except Exception:
        log.debug("Final sweep failed", exc_info=True)
        return None


def _build_incomplete_no_tools_result(
    *,
    base_task_id: str,
    workspace_id: str,
    final_message: str,
    aggregate_events: list[dict[str, Any]],
    baseline_sha: str,
    internal_task_ids: list[str],
) -> dict[str, Any]:
    return {
        "status": "incomplete",
        "error": "Ouroboros completed without invoking tools; no workspace work was verified",
        "task_id": base_task_id,
        "changes_made": [],
        "candidate_diff": "",
        "workspace_write_tool_calls": 0,
        "llm_tool_invocations": 0,
        "final_message": _truncate_final_message(final_message),
        "events_count": len(aggregate_events),
        "transport": "launcher",
        "baseline_sha": baseline_sha,
        "workspace_id": workspace_id,
        "internal_task_ids": list(internal_task_ids),
    }


def _prepare_self_review_task(
    *,
    base_task: dict[str, Any],
    base_task_id: str,
    review_prompt: str,
    self_review_attempt: int,
) -> dict[str, Any]:
    return {
        **base_task,
        "id": base_task_id,
        "input": review_prompt,
        "parent_task_id": base_task_id,
        "self_review_attempt": self_review_attempt,
        "max_rounds": 30,  # tight cap — review is short by design
    }


def _prepare_self_review_remediation_task(
    *,
    base_task: dict[str, Any],
    base_task_id: str,
    fix_prompt: str,
    remediation_attempt: int,
    remediation_rounds: int | None,
    prebuilt_plan_id: str | None = None,
) -> dict[str, Any]:
    current_task = {
        **base_task,
        "id": base_task_id,
        "input": fix_prompt,
        "parent_task_id": base_task_id,
        "verification_remediation_attempt": remediation_attempt,
        "self_review_origin": True,
    }
    if prebuilt_plan_id:
        current_task["prebuilt_plan_id"] = prebuilt_plan_id
    if remediation_rounds is not None:
        current_task["max_rounds"] = int(remediation_rounds)
    return current_task


def _prepare_verification_remediation_task(
    *,
    base_task: dict[str, Any],
    base_task_id: str,
    remediation_prompt: str,
    remediation_attempt: int,
    remediation_rounds: int | None,
    prebuilt_plan_id: str | None = None,
) -> dict[str, Any]:
    current_task = {
        **base_task,
        "id": base_task_id,
        "input": remediation_prompt,
        "parent_task_id": base_task_id,
        "verification_remediation_attempt": remediation_attempt,
    }
    if prebuilt_plan_id:
        current_task["prebuilt_plan_id"] = prebuilt_plan_id
    if remediation_rounds is not None:
        current_task["max_rounds"] = int(remediation_rounds)
    return current_task


def _agent_loop_error_from_events(
    events: list[dict[str, Any]], final_message: str = ""
) -> str:
    for event in reversed(events):
        if str(event.get("type") or "") == "task_error":
            err = str(event.get("error") or final_message or "").strip()
            tb = str(event.get("traceback") or "").strip()
            return "\n".join(part for part in [err, tb] if part)[:6000]
    if "Error during processing:" in str(final_message or ""):
        return str(final_message)[:2000]
    return ""


def _prepare_control_plane_recovery_task(
    *,
    base_task: dict[str, Any],
    base_task_id: str,
    error_context: str,
    remediation_attempt: int,
    remediation_rounds: int | None = None,
) -> dict[str, Any]:
    current_task = {
        **base_task,
        "id": base_task_id,
        "type": "self_improvement",
        "input": (
            "Umbrella/Ouroboros control-plane recovery is required. The previous "
            "workspace run crashed inside the agent loop before normal "
            "verification/remediation could proceed. Fix the harness/loop bug "
            "autonomously in this repo, preserve the current workspace work, "
            "then validate with focused tests and rerun the original flow.\n\n"
            "Crash context:\n"
            f"{error_context}"
        ),
        "parent_task_id": base_task_id,
        "verification_remediation_attempt": remediation_attempt,
        "control_plane_recovery_attempt": remediation_attempt,
    }
    if remediation_rounds is not None:
        current_task["max_rounds"] = int(remediation_rounds)
    return current_task


def run_ouroboros_improvement_sync(
    *,
    repo_root: Path,
    task_description: str,
    workspace_id: str,
    use_live_llm: bool = True,
    timeout_seconds: float | None = 300.0,
    promote: bool = False,
    experiment_id: str = "",
    candidate_isolation: bool = False,
    verify: bool = True,
    verification_timeout_seconds: int | None = None,
    require_instance: bool = True,
    task_id: str | None = None,
    verification_remediation_attempts: int = 0,
    verification_remediation_rounds: int | None = None,
    task_input_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Ouroboros synchronously for immediate improvements.

    Lifecycle:
    1. Record baseline SHA.
    2. Optionally create a task instance from the seed workspace.
    3. Submit the task to the Ouroboros launcher (with isolation if requested).
    4. Wait for completion.
    5. If isolated, use launcher-captured diff/changed_files (taken before
       sandbox rollback).  Otherwise fall back to live git inspection.
    6. If ``promote=True``, attempt auto-promotion of changes back to the seed.

    Args:
        repo_root: Repository root
        task_description: What to improve
        workspace_id: Target workspace
        use_live_llm: Whether to use live LLM
        timeout_seconds: Seconds to wait for the launcher result; None waits indefinitely.
        promote: When True, auto-promote changed files to the seed workspace.
            Callers using Meta-Harness gated promotion should leave this False.
        experiment_id: Meta-Harness experiment to associate the candidate with.
        candidate_isolation: When True, force ``git_branch`` sandbox so the
            candidate never touches the live worktree.  The captured diff is
            returned in the result for later ``apply_candidate_patch``.
        verify: Run runtime verification (pytest / HTTP boot / import checks)
            after the agent returns.  When True, the final ``status`` is one of
            ``verified`` / ``failed_verification`` / ``incomplete`` / ``error``.
            ``promote`` is only honored when status is ``verified``.
        verification_timeout_seconds: Hard ceiling for the full verification
            pass.  ``None`` uses the runner default.
        verification_remediation_attempts: Extra focused remediation passes to
            run inside this same sync invocation when runtime verification fails.
            These are continuations of the same web/CLI run, not fresh user-visible
            retries.
        verification_remediation_rounds: Optional per-remediation LLM round cap.
        task_id: Optional caller-provided id, useful for UI polling.

    Returns:
        Dict with status, changes_made, candidate_diff, verification_report,
        and other result details.
    """
    repo_root = repo_root.resolve()
    _canonical_drive_root(repo_root, workspace_id)

    baseline_sha = _record_baseline(repo_root)

    task_id = task_id or f"sync_improve_{uuid.uuid4().hex[:8]}"
    task_input_metadata = dict(task_input_metadata or {})

    instance_path = (
        None
        if candidate_isolation
        else _try_create_instance(repo_root, workspace_id, task_id)
    )
    candidate_workspace_path: Path | None = None
    if candidate_isolation:
        candidate_workspace_path = _prepare_candidate_workspace(
            repo_root, workspace_id, task_id
        )
        if candidate_workspace_path is not None:
            # Harness candidates must not mutate the live seed workspace before
            # a winner is selected.  Reuse the instance_path channel so
            # verification and candidate capture also point at the isolated copy.
            instance_path = candidate_workspace_path
    seed_profile_path = repo_root / "workspaces" / workspace_id / "seed_profile.toml"
    seed_profile_declared = seed_profile_path.exists()
    if require_instance and instance_path is None and seed_profile_declared:
        error_result = {
            "status": "error",
            "error": "instance_create_failed: task instance is required for this profiled workspace; rerun with --allow-seed-writes to operate directly on the seed workspace",
            "task_id": task_id,
            "workspace_id": workspace_id,
            "changes_made": [],
            "candidate_diff": "",
            "workspace_write_tool_calls": 0,
            "llm_tool_invocations": 0,
            "final_message": "",
            "events_count": 0,
            "transport": "launcher",
            "baseline_sha": baseline_sha,
        }
        _record_competency_signals(error_result, workspace_id, task_id)
        return error_result
    if require_instance and instance_path is None and not seed_profile_declared:
        log.warning(
            "Workspace %s has no seed_profile.toml; allowing legacy seed execution despite require_instance=True",
            workspace_id,
        )

    task: dict[str, Any] = {
        "id": task_id,
        "type": "sync_improvement",
        "input": task_description,
        "workspace_id": workspace_id,
        "depth": 1,
        "_is_direct_chat": False,
        "use_live_llm": use_live_llm,
        "context_overlays": build_context_overlays(repo_root),
    }
    if candidate_isolation:
        task["candidate_isolation"] = True
    if candidate_workspace_path is not None:
        task["workspace_root_overrides"] = {
            workspace_id: str(candidate_workspace_path),
        }
        task["candidate_workspace_path"] = str(candidate_workspace_path)
    if instance_path is not None:
        task["instance_path"] = str(instance_path)
    if timeout_seconds is not None:
        task["max_runtime_seconds"] = timeout_seconds

    try:
        from umbrella.orchestration.ouroboros_task import (
            render_verification_remediation_prompt,
            render_self_review_prompt,
            parse_self_review_response,
        )

        base_task_id = task_id
        max_remediation_attempts = max(0, int(verification_remediation_attempts or 0))
        # Self-review piggybacks on the remediation budget: it counts
        # as one cycle, so a workspace with ``max_attempts=3`` can have
        # at most 1 self-review + 2 remediations OR 2 self-reviews +
        # 1 remediation, etc. We hard-cap at 1 self-review so a happy
        # run doesn't spin forever rejecting its own LGTMs.
        max_self_review_attempts = 1 if max_remediation_attempts > 0 else 0
        aggregate_events: list[dict[str, Any]] = []
        internal_task_ids: list[str] = []
        result: dict[str, Any] = {}
        verification_payload: dict[str, Any] | None = None
        sweep_payload: dict[str, Any] | None = None
        completion_warnings: list[str] = []
        final_status = "error"
        final_message = ""
        candidate_diff = ""
        changes_made: list[str] = []
        failure_context_path = ""
        remediation_attempts_used = 0
        self_review_attempts_used = 0
        cancelled_payload: dict[str, Any] | None = None
        # The post-loop block reads these even when we ``break`` out of
        # the loop early (cancellation path) before the body sets them.
        # Initialise to safe defaults so we don't trip ``UnboundLocalError``
        # in the cancellation branch.
        no_writes = True
        workspace_write_tool_calls = 0
        remediation_write_tool_calls = 0
        llm_tool_invocations = 0
        failed_required_subtask = False
        consecutive_hygiene_remediations = 0

        # Drop any stale stop-request file that targets THIS new task id
        # (wouldn't normally happen with random ids, but a previous crash
        # could leave one behind and instantly cancel the new run before
        # it does any work).
        _clear_stop_requests_for_task(repo_root, workspace_id, base_task_id)

        current_task = dict(task)
        current_task_id = base_task_id
        _log_initial_phase_started(
            repo_root=repo_root,
            workspace_id=workspace_id,
            task_id=base_task_id,
        )

        while True:
            # Fast-fail if the user clicked Stop in the dashboard while we
            # were preparing the next iteration. Without this every
            # remediation/self-review cycle in flight would still get
            # submitted, hit ``_check_stop_requested`` inside the loop,
            # exit in <1s with 0 rounds, and silently churn through the
            # remediation budget — exactly the "5+ instant remediations
            # after pressing Stop" symptom we observed in the wild.
            stop_payload = _read_stop_request_for_task(
                repo_root, workspace_id, base_task_id
            )
            if stop_payload is not None:
                cancelled_payload = stop_payload
                final_status = "cancelled"
                final_message = _build_cancellation_message(
                    stop_payload=stop_payload,
                    remediation_attempts_used=remediation_attempts_used,
                    final_message_from_agent=final_message,
                )
                break

            current_task["id"] = current_task_id
            submitted_task_id, result = _run_launcher_task_once(
                repo_root=repo_root,
                task=current_task,
                timeout_seconds=timeout_seconds,
            )
            internal_task_ids.append(submitted_task_id)
            if result.get("status") == "error":
                result["status"] = "control_plane_error"
                result["error_kind"] = "agent_loop_crash"
                return result

            events = list(result.get("events") or [])
            aggregate_events.extend(events)
            candidate_diff = str(result.get("candidate_diff") or candidate_diff or "")
            launcher_changed: list[str] = list(
                result.get("candidate_changed_files") or []
            )
            if candidate_workspace_path is not None:
                changes_made = _collect_candidate_workspace_changes(
                    repo_root,
                    workspace_id,
                    candidate_workspace_path,
                )
            elif launcher_changed:
                changes_made = _filter_workspace_changes(launcher_changed, workspace_id)
            else:
                changes_made = _filter_workspace_changes(
                    _collect_changed_files(repo_root, baseline_sha),
                    workspace_id,
                )

            final_message = next(
                (
                    str(event.get("text") or "")
                    for event in reversed(aggregate_events)
                    if event.get("type") == "send_message"
                ),
                "",
            ) or str(result.get("result") or final_message or "")

            # Second cancellation gate: the agent itself can surface the
            # stop request via its final message ("Stop requested by
            # dashboard: …"). Treat that as an authoritative cancel even
            # if the stop file was deleted between submission and now —
            # otherwise we would then run verification, find no writes,
            # and start a remediation cycle for a run the user already
            # killed.
            if _final_message_indicates_stop(final_message):
                cancelled_payload = _read_stop_request_for_task(
                    repo_root, workspace_id, base_task_id
                ) or {"reason": "stop reported by agent"}
                final_status = "cancelled"
                final_message = _build_cancellation_message(
                    stop_payload=cancelled_payload,
                    remediation_attempts_used=remediation_attempts_used,
                    final_message_from_agent=final_message,
                )
                break

            write_events = [
                e for e in aggregate_events if e.get("type") == "workspace_write_tools"
            ]
            workspace_write_tool_calls = sum(
                int(e.get("count") or 0) for e in write_events
            )
            remediation_write_tool_calls = sum(
                1
                for event in aggregate_events
                if str(event.get("phase") or "").startswith("remediation")
                and str(event.get("type") or "") == "tool_call"
                and str(event.get("tool") or event.get("tool_name") or "")
                in {
                    "update_workspace_seed",
                    "update_workspace_from_instance",
                    "commit_workspace_changes",
                    "delete_workspace_file",
                }
            )
            effective_write_tool_calls = max(
                workspace_write_tool_calls, remediation_write_tool_calls
            )
            llm_tool_invocations = sum(
                int(event.get("tool_calls") or 0)
                for event in aggregate_events
                if event.get("type") == "task_metrics"
            )

            agent_error = _agent_loop_error_from_events(aggregate_events, final_message)
            if (
                agent_error
                and max_remediation_attempts > 0
                and remediation_attempts_used < max_remediation_attempts
            ):
                remediation_attempts_used += 1
                _log_phase_boundary_event(
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    task_id=base_task_id,
                    event_type="control_plane_recovery_started",
                    attempt=remediation_attempts_used,
                    max_attempts=max_remediation_attempts,
                )
                current_task = _prepare_control_plane_recovery_task(
                    base_task=task,
                    base_task_id=base_task_id,
                    error_context=agent_error,
                    remediation_attempt=remediation_attempts_used,
                    remediation_rounds=verification_remediation_rounds,
                )
                current_task_id = base_task_id
                continue

            if llm_tool_invocations == 0 and effective_write_tool_calls == 0:
                incomplete_result = _build_incomplete_no_tools_result(
                    base_task_id=base_task_id,
                    workspace_id=workspace_id,
                    final_message=final_message,
                    aggregate_events=aggregate_events,
                    baseline_sha=baseline_sha,
                    internal_task_ids=internal_task_ids,
                )
                _capture_candidate_safe(
                    repo_root=repo_root,
                    task_id=base_task_id,
                    workspace_id=workspace_id,
                    task_description=task_description,
                    instance_path=instance_path,
                    events=aggregate_events,
                    changes_made=[],
                    run_status="incomplete",
                    llm_tool_invocations=0,
                    workspace_write_tool_calls=0,
                    final_message=final_message,
                    result_dict=incomplete_result,
                    experiment_id=experiment_id,
                    candidate_diff=candidate_diff,
                    baseline_sha=baseline_sha,
                )
                _record_competency_signals(
                    incomplete_result, workspace_id, base_task_id
                )
                incomplete_result["canonical_task_result_path"] = (
                    _persist_canonical_task_result(
                        repo_root=repo_root,
                        workspace_id=workspace_id,
                        task_id=base_task_id,
                        result=incomplete_result,
                    )
                )
                return incomplete_result

            verification_payload = None
            if verify:
                _log_verification_phase_event(
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    task_id=base_task_id,
                    event_type="verification_started",
                )
                verification_payload = _run_workspace_verification(
                    repo_root,
                    workspace_id,
                    instance_path,
                    overall_timeout_seconds=verification_timeout_seconds,
                    changed_files=changes_made,
                    task_id=submitted_task_id,
                )
                _log_verification_phase_event(
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    task_id=base_task_id,
                    event_type="verification_completed",
                    verification_payload=verification_payload,
                )

            failed_required_subtask = _plan_has_failed_required_subtask(
                _canonical_drive_root(repo_root, workspace_id),
                submitted_task_id,
            )

            sweep_payload = _maybe_run_final_sweep(
                repo_root=repo_root,
                workspace_id=workspace_id,
                instance_path=instance_path,
                verification_payload=verification_payload,
            )

            no_writes = effective_write_tool_calls == 0
            final_status, completion_warnings = _resolve_final_status(
                verification_payload=verification_payload,
                failed_required_subtask=failed_required_subtask,
                no_writes=no_writes,
                sweep_payload=sweep_payload,
            )

            # Self-review hook: when verification passed AND we still
            # have budget AND haven't done it yet, ask the model to
            # look at the actual run output and decide whether to
            # accept or queue improvements. The verdict is parsed
            # from the LLM's last text reply (LGTM / NEEDS_FIX). A
            # NEEDS_FIX verdict turns into a remediation cycle with
            # the agent's own fixlist as the prompt body — same run,
            # same task_id.
            self_review_pending = (
                final_status == "verified"
                and self_review_attempts_used < max_self_review_attempts
                and remediation_attempts_used < max_remediation_attempts
                and not _self_review_already_run_in_aggregate(aggregate_events)
            )
            if self_review_pending:
                self_review_attempts_used += 1
                review_prompt = render_self_review_prompt(
                    original_task=task_description,
                    verification_report=verification_payload or {},
                    attempt=self_review_attempts_used,
                    max_attempts=max_self_review_attempts,
                )
                _archive_plan_before_remediation(
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    task_id=base_task_id,
                    attempt=remediation_attempts_used,
                )
                _log_phase_boundary_event(
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    task_id=base_task_id,
                    event_type="self_review_started",
                    attempt=self_review_attempts_used,
                    max_attempts=max_self_review_attempts,
                )
                current_task = _prepare_self_review_task(
                    base_task=task,
                    base_task_id=base_task_id,
                    review_prompt=review_prompt,
                    self_review_attempt=self_review_attempts_used,
                )
                continue

            # If the previous loop iteration WAS a self-review, the
            # final_message contains the agent's verdict. Parse it,
            # and if NEEDS_FIX, transform into a remediation prompt.
            if (
                self_review_attempts_used > 0
                and final_status == "verified"
                and current_task.get("self_review_attempt")
            ):
                verdict, body = parse_self_review_response(final_message)
                if verdict == "needs_fix" and _self_review_contract_failure(body):
                    completion_warnings.append("self_review_contract_failed")
                    log.warning(
                        "Self-review contract failed after green verification; "
                        "not consuming another remediation cycle. Body: %s",
                        body[:500],
                    )
                    break
                if (
                    verdict == "needs_fix"
                    and remediation_attempts_used < max_remediation_attempts
                ):
                    remediation_attempts_used += 1
                    fix_prompt = _render_self_review_remediation_prompt(
                        original_task=task_description,
                        fixlist_body=body,
                        attempt=remediation_attempts_used,
                        max_attempts=max_remediation_attempts,
                    )
                    _archive_plan_before_remediation(
                        repo_root=repo_root,
                        workspace_id=workspace_id,
                        task_id=base_task_id,
                        attempt=remediation_attempts_used,
                    )
                    _log_phase_boundary_event(
                        repo_root=repo_root,
                        workspace_id=workspace_id,
                        task_id=base_task_id,
                        event_type="remediation_started",
                        attempt=remediation_attempts_used,
                        max_attempts=max_remediation_attempts,
                    )
                    prebuilt_plan_id = synthesise_verification_remediation_plan(
                        drive_root=_canonical_drive_root(repo_root, workspace_id),
                        task_id=base_task_id,
                        workspace_id=workspace_id,
                        remediation_attempt=remediation_attempts_used,
                    )
                    current_task = _prepare_self_review_remediation_task(
                        base_task=task,
                        base_task_id=base_task_id,
                        fix_prompt=fix_prompt,
                        remediation_attempt=remediation_attempts_used,
                        remediation_rounds=verification_remediation_rounds,
                        prebuilt_plan_id=prebuilt_plan_id,
                    )
                    continue

            if (
                not _status_needs_remediation(final_status)
                or remediation_attempts_used >= max_remediation_attempts
            ):
                if _status_needs_remediation(final_status) and max_remediation_attempts:
                    final_message = _verification_exhausted_message(
                        attempts_used=remediation_attempts_used,
                        verification_payload=verification_payload,
                        failure_context_path=failure_context_path,
                    )
                break

            if not _has_actionable_remediation_context(
                final_status=final_status,
                verification_payload=verification_payload,
                sweep_payload=sweep_payload,
            ):
                completion_warnings.append("no_actionable_remediation_context")
                log.warning(
                    "Not starting remediation for %s: verification failures and "
                    "sweep cleanup targets are both empty.",
                    final_status,
                )
                break

            hygiene_only_failure = _is_hygiene_only_failure(
                final_status=final_status,
                completion_warnings=completion_warnings,
                verification_payload=verification_payload,
                sweep_payload=sweep_payload,
            )
            if (
                hygiene_only_failure
                and consecutive_hygiene_remediations >= _max_hygiene_remediations()
            ):
                completion_warnings.append("hygiene_remediation_skipped")
                _log_phase_boundary_event(
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    task_id=base_task_id,
                    event_type="hygiene_remediation_skipped",
                    metadata={
                        "cleanup_targets": _sweep_cleanup_targets(sweep_payload),
                        "max_hygiene_remediations": _max_hygiene_remediations(),
                    },
                )
                final_status = "verified_with_blocking_noise"
                break
            if hygiene_only_failure:
                consecutive_hygiene_remediations += 1
            else:
                consecutive_hygiene_remediations = 0

            remediation_attempts_used += 1
            failure_context = _persist_verification_failure_context(
                repo_root=repo_root,
                workspace_id=workspace_id,
                task_id=submitted_task_id,
                remediation_attempt=remediation_attempts_used,
                max_attempts=max_remediation_attempts,
                verification_payload=verification_payload,
                sweep_payload=sweep_payload,
                completion_warnings=completion_warnings,
                failure_kind=(
                    "hygiene" if final_status == "failed_hygiene" else "verification"
                ),
                changed_files=changes_made,
            )
            failure_context_path = str(failure_context.get("state_path") or "")
            # Backend-side recall_memory: pull lessons from past
            # remediation cycles in this workspace and inject them into
            # the prompt. Saves the agent from having to remember to
            # call ``recall_memory`` voluntarily (which it almost never
            # does — empirically 0 ``recall_memory`` calls per multi-
            # hour run while ``record_idea`` was called dozens of
            # times). Lessons live in ``<workspace>/.memory/ideas.jsonl``.
            recalled_lessons: list[dict[str, str]] = []
            try:
                from umbrella.orchestration.ouroboros_task import (
                    _recall_relevant_lessons_for_failures,
                )

                workspace_memory_root = (
                    repo_root / "workspaces" / workspace_id / ".memory"
                )
                failing_for_recall = [
                    item
                    for item in (verification_payload or {}).get("results") or []
                    if isinstance(item, dict)
                    and not item.get("optional")
                    and str(item.get("status") or "").lower() != "passed"
                ]
                if not failing_for_recall and final_status == "failed_hygiene":
                    failing_for_recall = [
                        {
                            "name": "final_sweep:hygiene",
                            "kind": "final_sweep",
                            "status": "failed",
                            "summary": (
                                (sweep_payload or {}).get("summary")
                                or "final_sweep found cleanup targets"
                            ),
                        }
                    ]
                recalled_lessons = _recall_relevant_lessons_for_failures(
                    workspace_memory_root=workspace_memory_root,
                    failing=failing_for_recall,
                )
            except Exception:
                log.debug("recall lessons failed", exc_info=True)
            remediation_prompt = render_verification_remediation_prompt(
                original_task=task_description,
                verification_report=verification_payload or {},
                attempt=remediation_attempts_used,
                max_attempts=max_remediation_attempts,
                previous_final_message=final_message,
                failure_context_path=failure_context_path,
                recalled_lessons=recalled_lessons,
            )
            # Keep the SAME ``task_id`` across remediation iterations so the
            # whole loop appears as a single continuous run in the UI and
            # in ``task_results/<id>.json``. The user sees ONE row, ONE
            # logical task, and the agent receives the failure context as
            # a continuation prompt — not a fresh run with a reset run id.
            #
            # CRITICAL: archive the cached plan first. Otherwise
            # ``plan_store.load(task_id)`` finds the previous attempt's
            # plan with every subtask marked ``done`` →
            # ``plan.is_complete()`` short-circuits the subtask phase →
            # the loop drops straight into ``final_aggregation`` with
            # ``tool_schemas=[]`` and the model gets the failure context
            # but cannot call a single tool to fix it. The user sees 8
            # remediation cycles each with ``tool_calls=0`` and 0
            # ``workspace_write_tools``. We archive the plan (kept on
            # disk for audit) so the planner runs again on the new
            # remediation prompt with full tool access.
            _archive_plan_before_remediation(
                repo_root=repo_root,
                workspace_id=workspace_id,
                task_id=base_task_id,
                attempt=remediation_attempts_used,
            )
            _log_phase_boundary_event(
                repo_root=repo_root,
                workspace_id=workspace_id,
                task_id=base_task_id,
                event_type="remediation_started",
                attempt=remediation_attempts_used,
                max_attempts=max_remediation_attempts,
            )
            prebuilt_plan_id = synthesise_verification_remediation_plan(
                drive_root=_canonical_drive_root(repo_root, workspace_id),
                task_id=base_task_id,
                workspace_id=workspace_id,
                remediation_attempt=remediation_attempts_used,
                failure_kind=(
                    "hygiene" if final_status == "failed_hygiene" else "verification"
                ),
            )
            current_task_id = base_task_id
            current_task = _prepare_verification_remediation_task(
                base_task=task,
                base_task_id=base_task_id,
                remediation_prompt=remediation_prompt,
                remediation_attempt=remediation_attempts_used,
                remediation_rounds=verification_remediation_rounds,
                prebuilt_plan_id=prebuilt_plan_id,
            )

        quality_telemetry = _collect_run_quality_telemetry(
            repo_root=repo_root,
            workspace_id=workspace_id,
        )

        final_status, completion_warnings = _apply_delivery_contract_gate(
            final_status=final_status,
            completion_warnings=completion_warnings,
            quality_telemetry=quality_telemetry,
            drive_root=_canonical_drive_root(repo_root, workspace_id),
            task_id=base_task_id,
        )

        no_write_error: str = ""
        if no_writes and final_status == "incomplete":
            no_write_error = (
                "Ouroboros finished without writing any workspace files "
                f"(executed {llm_tool_invocations} tool calls, all read-only). "
                "The task requires producing deliverables in "
                f"`workspaces/{workspace_id}/` via tools like "
                "`update_workspace_seed` / `update_workspace_from_instance` / "
                "`commit_workspace_changes`."
            )

        # Promotion safety: ``verified`` is necessary but not sufficient.
        # If the verification spec is *shallow* (e.g. only ``import_check``
        # and ``file_exists`` steps with zero ``shell`` / ``http_boot`` /
        # ``behavioral_http`` coverage), a green run can still mean the
        # code merely imports and one file exists — it does not prove the
        # workspace actually does its job. We block automatic promotion
        # in that case unless the user explicitly opts in via
        # ``[promotion] allow_shallow_verification = true`` in
        # ``workspace.toml``. The change is recoverable: the user can
        # promote manually once they review the diff.
        promotion_blocked_reason = ""
        # Cancelled runs are NEVER promoted: the user explicitly said stop,
        # and we may have only partial writes that pass shallow checks by
        # coincidence. Force a manual review.
        if final_status == "cancelled":
            completion_warnings.append("cancelled_by_user")
            promotion_blocked_reason = "cancelled_by_user"
        should_promote = (
            promote and final_status == "verified" and not completion_warnings
        )
        if should_promote:
            shallow = _verification_spec_is_shallow(verification_payload)
            allow_shallow = _workspace_allows_shallow_promotion(
                repo_root,
                workspace_id,
            )
            if shallow and not allow_shallow:
                should_promote = False
                promotion_blocked_reason = "shallow_verification_spec"
                log.warning(
                    "Promotion blocked for %s: verification spec is shallow "
                    "(import_check / file_exists only). Set "
                    "``[promotion] allow_shallow_verification = true`` in "
                    "workspace.toml to opt in.",
                    workspace_id,
                )
                completion_warnings.append("promotion_blocked_shallow_verification")
        promoted = (
            _try_promote_changes(repo_root, workspace_id, changes_made, instance_path)
            if should_promote
            else []
        )
        promotion_blocked_report_reason = promotion_blocked_reason
        if promote and not promotion_blocked_report_reason:
            if final_status == "failed_hygiene":
                promotion_blocked_report_reason = "hygiene_failed"
            elif final_status in _DELIVERY_CONTRACT_FAIL_STATUSES:
                promotion_blocked_report_reason = final_status
            elif final_status != "verified":
                promotion_blocked_report_reason = "verification_failed"
            elif completion_warnings:
                promotion_blocked_report_reason = "post_verify_warnings"

        complete_result: dict[str, Any] = {
            "status": final_status,
            "task_id": base_task_id,
            "changes_made": changes_made,
            "candidate_diff": candidate_diff,
            "promoted_files": promoted,
            "promotion_blocked_reason": promotion_blocked_report_reason,
            "workspace_write_tool_calls": workspace_write_tool_calls,
            "remediation_write_tool_calls": remediation_write_tool_calls,
            "llm_tool_invocations": llm_tool_invocations,
            "final_message": _normalize_final_message_for_status(
                final_status=final_status,
                final_message=final_message,
                verification_payload=verification_payload,
                sweep_payload=sweep_payload,
                completion_warnings=completion_warnings,
                changes_made=changes_made,
                remediation_attempts_used=remediation_attempts_used,
                repo_root=repo_root,
                workspace_id=workspace_id,
                base_task_id=base_task_id,
            ),
            "events_count": len(aggregate_events),
            "transport": "launcher",
            "instance_path": str(instance_path) if instance_path else None,
            "candidate_workspace_path": str(candidate_workspace_path)
            if candidate_workspace_path
            else "",
            "baseline_sha": baseline_sha,
            "workspace_id": workspace_id,
            "verification_report": verification_payload,
            "verification_passed": bool(
                verification_payload and verification_payload.get("passed")
            ),
            "failed_required_subtask": failed_required_subtask,
            "completion_warnings": completion_warnings,
            "sweep_report": sweep_payload,
            "verification_skipped": bool(
                verification_payload and verification_payload.get("skipped")
            ),
            "quality_telemetry": quality_telemetry,
            "verification_remediation_attempts_used": remediation_attempts_used,
            "verification_remediation_max": max_remediation_attempts,
            "verification_failure_context_path": failure_context_path,
            "internal_task_ids": list(internal_task_ids),
            "task_source": task_input_metadata.get("task_source", ""),
            "task_hash": task_input_metadata.get("task_hash", ""),
            "task_missing": bool(task_input_metadata.get("task_missing", False)),
        }
        complete_result["final_gate_report_path"] = _persist_final_gate_report(
            repo_root=repo_root,
            workspace_id=workspace_id,
            task_id=base_task_id,
            final_status=final_status,
            verification_payload=verification_payload,
            completion_warnings=completion_warnings,
            sweep_payload=sweep_payload,
        )
        if no_write_error:
            complete_result["error"] = no_write_error
        complete_result["canonical_task_result_path"] = _persist_canonical_task_result(
            repo_root=repo_root,
            workspace_id=workspace_id,
            task_id=base_task_id,
            result=complete_result,
        )
        _capture_candidate_safe(
            repo_root=repo_root,
            task_id=base_task_id,
            workspace_id=workspace_id,
            task_description=task_description,
            instance_path=instance_path,
            events=aggregate_events,
            changes_made=changes_made,
            promoted_files=promoted,
            run_status=final_status,
            llm_tool_invocations=llm_tool_invocations,
            workspace_write_tool_calls=workspace_write_tool_calls,
            final_message=final_message,
            result_dict=complete_result,
            experiment_id=experiment_id,
            candidate_diff=candidate_diff,
            baseline_sha=baseline_sha,
            verification_report=verification_payload,
        )
        _record_competency_signals(complete_result, workspace_id, base_task_id)
        return complete_result

    except Exception as e:
        log.error(f"Ouroboros sync improvement failed: {e}", exc_info=True)
        error_result: dict[str, Any] = {
            "status": "error",
            "error": str(e),
            "task_id": task_id,
        }
        _capture_candidate_safe(
            repo_root=repo_root,
            task_id=task_id,
            workspace_id=workspace_id,
            task_description=task_description,
            instance_path=instance_path,
            events=[],
            changes_made=[],
            run_status="error",
            error=str(e),
            result_dict=error_result,
            experiment_id=experiment_id,
        )
        _record_competency_signals(error_result, workspace_id, task_id)
        error_result["canonical_task_result_path"] = _persist_canonical_task_result(
            repo_root=repo_root,
            workspace_id=workspace_id,
            task_id=task_id,
            result=error_result,
        )
        return error_result
