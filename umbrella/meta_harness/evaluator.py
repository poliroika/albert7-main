"""Workspace candidate gate for Meta-Harness.

Evaluates a candidate against a search set using a **workspace_candidate_gate**
strategy: for each task matching the candidate's workspace, scores the
candidate's manifest data (write calls, changed files, run status, etc.)
without re-executing the task.  Tasks from other workspaces are skipped.

A full ``search_set_gate`` that re-runs each task is intentionally separate
and optional (not yet implemented).

Delegates to the existing ``umbrella.evals.runner`` for per-task evaluation
and adds Meta-Harness-specific metrics.

Score formula v2 (with runtime verification gate):
    0.35 * task_success
    0.20 * artifact_quality
    0.15 * validation_pass
    0.15 * runtime_verification
    0.10 * stability
    0.00 * cost_efficiency (neutralised to fold into runtime gate)
    0.05 * observability_quality

When ``runtime_verification`` is failing, the candidate cannot reach
``MetaPromotionEligibility.PROMOTE`` regardless of the aggregate score.
"""

import logging
import statistics
import subprocess
from pathlib import Path
from typing import Any

from umbrella.meta_harness.models import (
    CandidateEval,
    CandidateManifest,
    CandidateStatus,
    SearchSet,
    SearchTask,
    TaskEvalResult,
)
from umbrella.meta_harness.store import MetaHarnessStore, get_default_store

log = logging.getLogger(__name__)

_VALIDATION_TIMEOUT = 120
_VERIFICATION_TIMEOUT = 900


# ---------------------------------------------------------------------------
# Score components
# ---------------------------------------------------------------------------


def _score_task_success(status: str) -> float:
    return {
        "verified": 1.0,
        "complete": 0.8,
        "partial": 0.5,
        "incomplete": 0.2,
        "failed_verification": 0.2,
        "failed_hygiene": 0.2,
        "error": 0.0,
    }.get(status, 0.3)


def _score_runtime_verification(
    repo_root: Path,
    task: SearchTask,
    *,
    instance_path: str | Path | None = None,
    cached_report: dict[str, Any] | None = None,
) -> tuple[float, bool, bool, str]:
    """Run the workspace's verification spec and return a score tuple.

    Returns ``(score, passed, skipped, summary)``.

    Resolution order for the verification target:

    1. ``cached_report`` — if the caller already ran ``run_verification`` for
       this candidate (e.g. via ``run_ouroboros_improvement_sync``), reuse
       the report instead of re-running. This keeps Meta-Harness consistent
       with the Ouroboros post-gate and avoids double-spending time.
    2. ``instance_path`` — the task-instance copy produced by the run. This
       is the correct target because promotion is gated on the instance's
       content, not the pristine seed.
    3. ``repo_root/workspaces/<workspace_id>`` — fallback to the seed when
       no instance path is available (e.g. legacy candidates).
    """
    if cached_report:
        return _score_from_cached_report(cached_report)

    try:
        from umbrella.verification import load_verification_spec, run_verification
    except Exception:  # noqa: BLE001
        log.debug("Verification module unavailable", exc_info=True)
        return 0.5, False, True, "verification module unavailable"

    candidate_root: Path | None = None
    if instance_path:
        candidate_root = Path(instance_path).resolve()
        if not candidate_root.exists():
            log.debug(
                "Candidate instance_path missing, falling back to seed: %s",
                candidate_root,
            )
            candidate_root = None
    if candidate_root is None:
        candidate_root = repo_root / "workspaces" / task.workspace_id

    if not candidate_root.exists():
        return 0.0, False, True, f"workspace path missing: {candidate_root}"

    try:
        steps = load_verification_spec(candidate_root)
    except Exception:  # noqa: BLE001
        log.debug(
            "load_verification_spec crashed for %s", task.workspace_id, exc_info=True
        )
        return 0.5, False, True, "verification spec load failed"

    if not steps:
        return 0.5, False, True, "no verification steps declared or auto-detected"

    try:
        report = run_verification(
            candidate_root,
            steps,
            workspace_id=task.workspace_id,
            overall_timeout_seconds=_VERIFICATION_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("run_verification crashed: %s", exc, exc_info=True)
        return 0.0, False, False, f"verification crashed: {exc}"

    return (
        report.pass_rate,
        report.passed,
        False,
        report.render_summary(limit_chars=800),
    )


def _score_from_cached_report(
    report: dict[str, Any],
) -> tuple[float, bool, bool, str]:
    """Derive a score tuple from a serialized ``VerificationReport`` dict."""

    passed = bool(report.get("passed"))
    skipped = bool(report.get("skipped"))
    try:
        pass_rate = float(report.get("pass_rate") or 0.0)
    except (TypeError, ValueError):
        pass_rate = 1.0 if passed else 0.0
    summary = str(report.get("summary") or "")
    if skipped:
        return 0.5, False, True, summary or "verification skipped"
    return max(0.0, min(1.0, pass_rate)), passed, False, summary


def _score_artifact_quality(result: dict[str, Any]) -> float:
    write_calls = int(result.get("workspace_write_tool_calls", 0) or 0)
    changes = len(result.get("changes_made", []))
    if changes >= 3 and write_calls >= 5:
        return 1.0
    if changes >= 1 and write_calls >= 2:
        return 0.7
    if write_calls >= 1:
        return 0.4
    return 0.1


def _score_validation(
    task: SearchTask,
    repo_root: Path,
) -> float:
    """Run validation commands and return pass rate."""
    if not task.validation_commands:
        return 0.5  # neutral when no validation defined

    passed = 0
    total = len(task.validation_commands)

    for cmd in task.validation_commands:
        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_root / "workspaces" / task.workspace_id),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_VALIDATION_TIMEOUT,
            )
            if result.returncode == 0:
                passed += 1
        except Exception:
            pass

    return passed / total if total > 0 else 0.5


def _score_cost_efficiency(cost_usd: float, tokens: int, status: str) -> float:
    if status in ("error", "incomplete"):
        return 0.0
    if cost_usd <= 0.5:
        return 1.0
    if cost_usd <= 2.0:
        return 0.7
    if cost_usd <= 5.0:
        return 0.4
    return 0.2


def _score_observability(result: dict[str, Any]) -> float:
    score = 0.0
    if result.get("final_message"):
        score += 0.4
    if result.get("events_count", 0) > 0:
        score += 0.3
    if result.get("changes_made"):
        score += 0.3
    return min(1.0, score)


def compute_weighted_score(
    task_success: float,
    artifact_quality: float,
    validation_pass: float,
    stability: float,
    cost_efficiency: float,
    observability: float,
    runtime_verification: float = 0.5,
) -> float:
    return (
        0.35 * task_success
        + 0.20 * artifact_quality
        + 0.15 * validation_pass
        + 0.15 * runtime_verification
        + 0.10 * stability
        + 0.05 * observability
    )


# ---------------------------------------------------------------------------
# Single-task evaluation
# ---------------------------------------------------------------------------


def evaluate_candidate_task(
    repo_root: Path,
    candidate: CandidateManifest,
    task: SearchTask,
) -> TaskEvalResult:
    """Evaluate a candidate on a single search-set task.

    Only produces a real score when the candidate's workspace matches the
    task's workspace.  For non-matching tasks the only signal is validation
    commands (cheap subprocess calls); manifest-level metrics are not
    projected onto unrelated workspaces.
    """
    workspace_match = candidate.workspace_id == task.workspace_id

    if not workspace_match:
        validation_pass = _score_validation(task, repo_root)
        return TaskEvalResult(
            task_id=task.task_id,
            workspace_id=task.workspace_id,
            status="skipped",
            score=validation_pass,
            task_success=0.0,
            artifact_quality=0.0,
            validation_pass=validation_pass,
            notes="not evaluated - different workspace",
        )

    result_data: dict[str, Any] = {
        "status": candidate.run_status,
        "workspace_write_tool_calls": candidate.write_calls,
        "changes_made": candidate.changed_files,
        "final_message": candidate.final_message,
        "events_count": candidate.events_count,
    }

    task_success = _score_task_success(candidate.run_status)
    artifact_quality = _score_artifact_quality(result_data)
    validation_pass = _score_validation(task, repo_root)
    cost_efficiency = _score_cost_efficiency(
        candidate.cost_usd, candidate.total_tokens, candidate.run_status
    )
    observability = _score_observability(result_data)

    cached_report = None
    if isinstance(candidate.metadata, dict):
        raw_report = candidate.metadata.get("verification_report")
        if isinstance(raw_report, dict):
            cached_report = raw_report

    runtime_score, runtime_passed, runtime_skipped, runtime_summary = (
        _score_runtime_verification(
            repo_root,
            task,
            instance_path=candidate.instance_path or None,
            cached_report=cached_report,
        )
    )

    score = compute_weighted_score(
        task_success=task_success,
        artifact_quality=artifact_quality,
        validation_pass=validation_pass,
        stability=0.5,
        cost_efficiency=cost_efficiency,
        observability=observability,
        runtime_verification=runtime_score,
    )

    return TaskEvalResult(
        task_id=task.task_id,
        workspace_id=task.workspace_id,
        status=candidate.run_status,
        score=score,
        task_success=task_success,
        artifact_quality=artifact_quality,
        validation_pass=validation_pass,
        runtime_verification=runtime_score,
        runtime_verification_passed=runtime_passed,
        runtime_verification_skipped=runtime_skipped,
        verification_summary=runtime_summary,
        cost_usd=candidate.cost_usd,
        tokens=candidate.total_tokens,
        duration_seconds=candidate.duration_seconds,
        errors=[candidate.error] if candidate.error else [],
    )


# ---------------------------------------------------------------------------
# Full search-set evaluation
# ---------------------------------------------------------------------------


def evaluate_candidate_on_search_set(
    repo_root: Path,
    candidate_id: str,
    search_set: SearchSet,
    *,
    store: MetaHarnessStore | None = None,
) -> CandidateEval:
    """Evaluate a candidate against all tasks in a search set."""
    if store is None:
        store = get_default_store(repo_root)

    candidate = store.find_candidate(candidate_id)
    if candidate is None:
        return CandidateEval(
            candidate_id=candidate_id,
            notes=f"Candidate {candidate_id} not found",
        )

    task_results: list[TaskEvalResult] = []
    for task in search_set.tasks:
        try:
            result = evaluate_candidate_task(repo_root, candidate, task)
            task_results.append(result)
        except Exception as exc:
            log.warning("Eval failed for task %s: %s", task.task_id, exc)
            task_results.append(
                TaskEvalResult(
                    task_id=task.task_id,
                    workspace_id=task.workspace_id,
                    status="error",
                    errors=[str(exc)],
                )
            )

    evaluated = [r for r in task_results if r.status != "skipped"]
    scores = (
        [r.score for r in evaluated] if evaluated else [r.score for r in task_results]
    )
    complete_count = sum(1 for r in task_results if r.status == "complete")
    partial_count = sum(1 for r in task_results if r.status == "partial")
    failed_count = sum(
        1 for r in task_results if r.status in ("error", "failed", "incomplete")
    )

    avg_score = statistics.mean(scores) if scores else 0.0
    median_score = statistics.median(scores) if scores else 0.0

    evaluation = CandidateEval(
        candidate_id=candidate_id,
        experiment_id=candidate.experiment_id,
        search_set_id=search_set.id,
        task_results=task_results,
        tasks_total=len(task_results),
        tasks_complete=complete_count,
        tasks_partial=partial_count,
        tasks_failed=failed_count,
        avg_score=avg_score,
        median_score=median_score,
        weighted_score=avg_score,
        total_cost_usd=sum(r.cost_usd for r in task_results),
        total_tokens=sum(r.tokens for r in task_results),
        total_duration_seconds=sum(r.duration_seconds for r in task_results),
        write_calls=candidate.write_calls,
        tool_calls=candidate.tool_calls,
        raw_trace_paths=[str(store.find_candidate_dir(candidate_id) or "")],
    )

    # Update candidate status
    candidate.status = CandidateStatus.EVALUATED
    store.save_candidate(candidate)
    store.save_eval(evaluation)

    return evaluation


# ---------------------------------------------------------------------------
# Retrieval usefulness (extended)
# ---------------------------------------------------------------------------


def assess_retrieval_usefulness_extended(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assess retrieval usefulness from raw execution events."""
    memory_calls = 0
    gmas_calls = 0
    search_calls = 0

    for event in events:
        tool_name = event.get("tool", "") or event.get("type", "")
        if "get_umbrella_memory" in tool_name:
            memory_calls += 1
        elif "get_gmas_context" in tool_name:
            gmas_calls += 1
        elif "search_gmas_knowledge" in tool_name:
            search_calls += 1

    total = memory_calls + gmas_calls + search_calls
    return {
        "retrieval_useful": total > 0,
        "memory_calls": memory_calls,
        "gmas_calls": gmas_calls,
        "search_calls": search_calls,
        "total_retrieval_calls": total,
    }
