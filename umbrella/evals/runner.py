"""
Workspace run evaluation - assess run success, quality, and cost.

Quality assessment is artifact-based: it reads actual report files,
counts words/sections, and checks stage completeness rather than
relying on keyword heuristics in ``final_answer``.

Can use LLM-based evaluation for more intelligent assessment.
"""

import json
import logging
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from umbrella.evals.models import (
    TaskSuccessRating,
    OutputQualityRating,
    StabilityRating,
    EvaluationRecord,
    generate_evaluation_id,
)
from umbrella.workspace_runtime.models import WorkspaceRunResult, WorkspaceRunStatus

# Flag to enable LLM-based evaluation globally
USE_LLM_EVALUATION = True

log = logging.getLogger(__name__)

_DEFAULT_MIN_ARTICLE_WORDS = 1500
_DEFAULT_REQUIRED_ARTIFACT_TYPES: list[str] = ["report"]


def _run_result_payload(run_result: WorkspaceRunResult) -> dict[str, Any]:
    """Return a JSON-safe payload for evaluators that expect a plain dict."""
    if hasattr(run_result, "model_dump"):
        payload = run_result.model_dump(mode="json")
    elif is_dataclass(run_result):
        payload = asdict(run_result)
    else:
        payload = dict(vars(run_result))
    return json.loads(json.dumps(payload, default=str))


def _effective_min_word_count(
    default_min_word_count: int,
    *,
    task_input: str | None,
) -> int:
    """Relax article-length expectations for explicitly summary-style tasks."""
    normalized = (task_input or "").strip().lower()
    summary_markers = (
        "brief summary",
        "summary of",
        "summarize",
        "overview",
        "workspace summary",
        "explain the workspace",
    )
    if any(marker in normalized for marker in summary_markers):
        return min(default_min_word_count, 300)
    return default_min_word_count


def evaluate_run(
    run_result: WorkspaceRunResult,
    instance_path: Path,
    *,
    task_class: Any | None = None,
    previous_evals: list[EvaluationRecord] | None = None,
    repo_root: Path | None = None,
    min_article_word_count: int = _DEFAULT_MIN_ARTICLE_WORDS,
    required_artifact_types: list[str] | None = None,
    task_input: str | None = None,
    use_llm: bool | None = None,
) -> EvaluationRecord:
    """Evaluate a workspace run and produce a standardized evaluation record.

    Args:
        run_result: Result from workspace execution
        instance_path: Path to the workspace instance
        task_class: Optional task class for context
        previous_evals: Previous evaluations for stability assessment
        repo_root: Repository root for file operations
        task_input: Original task input (required for LLM evaluation)
        use_llm: Use LLM-based evaluation (defaults to USE_LLM_EVALUATION)

    Returns:
        EvaluationRecord with all metrics and ratings
    """
    instance_path = Path(instance_path)
    effective_artifact_types = (
        required_artifact_types or _DEFAULT_REQUIRED_ARTIFACT_TYPES
    )
    effective_min_word_count = _effective_min_word_count(
        min_article_word_count,
        task_input=task_input,
    )

    # Determine whether to use LLM evaluation
    should_use_llm = use_llm if use_llm is not None else USE_LLM_EVALUATION

    # Use LLM-based evaluation if enabled and task_input is provided
    if should_use_llm and task_input:
        try:
            from umbrella.evals.llm_evaluator import evaluate_run_with_llm

            log.info("Using LLM-based evaluation for run %s", run_result.run_id)
            return evaluate_run_with_llm(
                _run_result_payload(run_result),
                instance_path,
                repo_root or Path.cwd(),
                task_input,
                task_class=str(task_class) if task_class else None,
            )
        except ImportError:
            log.warning(
                "LLM evaluator not available, falling back to formula-based evaluation"
            )
        except Exception as e:
            log.warning(
                f"LLM evaluation failed: {e}, falling back to formula-based evaluation"
            )

    # Extract basic info from run result
    task_id = run_result.task_id
    workspace_id = run_result.workspace_id
    run_id = run_result.run_id

    # Assess task success (artifact-based)
    task_success = _assess_task_success(
        run_result,
        instance_path,
        min_word_count=effective_min_word_count,
        required_artifact_types=effective_artifact_types,
    )

    # Assess output quality (artifact-based)
    output_quality = _assess_output_quality(
        run_result,
        instance_path,
        min_word_count=effective_min_word_count,
    )

    # Assess stability (based on previous evals if available)
    stability = _assess_stability(run_result, previous_evals)

    # Cost metrics
    total_tokens = run_result.total_tokens or 0
    total_duration = run_result.duration_seconds or 0.0
    total_cost_usd = _estimate_cost_usd(total_tokens, total_duration)

    # Iteration metrics
    iterations_to_completion = _extract_iterations(run_result)
    iterations_limit_reached = run_result.status == WorkspaceRunStatus.PAUSED

    # Retrieval assessment
    retrieval_was_useful = _assess_retrieval_usefulness(run_result)
    retrieval_hits_used = _count_retrieval_hits(run_result)
    raw_log_inspection_required = _required_raw_log_inspection(run_result)

    # Patch effectiveness
    patches_applied = _count_patches_applied(run_result)
    patch_success_rate = _calculate_patch_success_rate(run_result)

    # Observability quality
    structured_summary_sufficient = _check_summary_sufficiency(run_result)
    artifact_count = _count_artifacts(run_result, instance_path)

    # Manager signals
    manager_level_issues = _extract_manager_issues(run_result)

    # Evidence collection
    evidence = _collect_evidence(run_result)

    # Calculate overall score
    overall_score = _calculate_overall_score(
        task_success=task_success,
        output_quality=output_quality,
        stability=stability,
        cost_effectiveness=_assess_cost_effectiveness(total_cost_usd, task_success),
        retrieval_useful=retrieval_was_useful,
    )

    return EvaluationRecord(
        id=generate_evaluation_id(),
        task_id=task_id,
        workspace_id=workspace_id,
        run_id=run_id,
        instance_path=instance_path,
        task_success=task_success,
        output_quality=output_quality,
        stability=stability,
        total_tokens=total_tokens,
        total_duration_seconds=total_duration,
        total_cost_usd=total_cost_usd,
        iterations_to_completion=iterations_to_completion,
        iterations_limit_reached=iterations_limit_reached,
        retrieval_was_useful=retrieval_was_useful,
        retrieval_hits_used=retrieval_hits_used,
        raw_log_inspection_required=raw_log_inspection_required,
        patches_applied=patches_applied,
        patch_success_rate=patch_success_rate,
        structured_summary_sufficient=structured_summary_sufficient,
        artifact_count=artifact_count,
        manager_level_issues=manager_level_issues,
        overall_score=overall_score,
        evidence=evidence,
        evaluator_notes=f"Evaluated run {run_id} for task {task_id}",
    )


def _assess_task_success(
    run_result: WorkspaceRunResult,
    instance_path: Path,
    *,
    min_word_count: int = _DEFAULT_MIN_ARTICLE_WORDS,
    required_artifact_types: list[str] | None = None,
) -> TaskSuccessRating:
    """Assess task success by inspecting actual artifacts, not keywords."""
    if run_result.status == WorkspaceRunStatus.FAILED:
        return TaskSuccessRating.FAILED
    if run_result.status not in (
        WorkspaceRunStatus.COMPLETED,
        WorkspaceRunStatus.PAUSED,
    ):
        return TaskSuccessRating.UNKNOWN

    if run_result.errors:
        return TaskSuccessRating.FAILED

    effective_types = required_artifact_types or _DEFAULT_REQUIRED_ARTIFACT_TYPES

    # Require expected artifact types to be present
    for artifact_type in effective_types:
        if not any(
            a.artifact_type.value == artifact_type for a in run_result.artifacts
        ):
            return TaskSuccessRating.PARTIAL

    # Check report content meets minimum depth
    report_paths = _collect_primary_report_paths(run_result, instance_path)
    if not report_paths:
        return TaskSuccessRating.PARTIAL

    for path in report_paths:
        word_count = _count_words(path)
        if word_count < min_word_count:
            log.info(
                "Report %s has %d words (need %d); task_success=PARTIAL",
                path.name,
                word_count,
                min_word_count,
            )
            return TaskSuccessRating.PARTIAL

    # Check pipeline stage completeness
    stages_info = _scan_stage_notes(instance_path)
    if stages_info.get("total", 0) > 0 and stages_info.get("missing"):
        return TaskSuccessRating.PARTIAL

    return TaskSuccessRating.COMPLETE


def _assess_output_quality(
    run_result: WorkspaceRunResult,
    instance_path: Path,
    *,
    min_word_count: int = _DEFAULT_MIN_ARTICLE_WORDS,
) -> OutputQualityRating:
    """Assess quality by reading actual report content."""
    report_paths = _collect_report_paths(run_result, instance_path)

    if not report_paths:
        return OutputQualityRating.UNUSABLE

    best_rating = OutputQualityRating.UNUSABLE

    for path in report_paths:
        word_count = _count_words(path)
        section_count = _count_sections(path)
        has_citations = _has_citations(path)

        if word_count >= 3000 and section_count >= 5 and has_citations:
            rating = OutputQualityRating.EXCELLENT
        elif word_count >= min_word_count and section_count >= 3:
            rating = OutputQualityRating.GOOD
        elif word_count >= 500 and section_count >= 2:
            rating = OutputQualityRating.FAIR
        elif word_count > 50:
            rating = OutputQualityRating.POOR
        else:
            rating = OutputQualityRating.UNUSABLE

        if _quality_rank(rating) > _quality_rank(best_rating):
            best_rating = rating

    return best_rating


# ── Artifact helpers ────────────────────────────────────────────────────


def _collect_report_paths(
    run_result: WorkspaceRunResult, instance_path: Path
) -> list[Path]:
    """Gather all readable report file paths."""
    paths: list[Path] = []

    for artifact in run_result.artifacts:
        if artifact.artifact_type.value == "report" and artifact.path.exists():
            paths.append(artifact.path)

    # Fallback: scan well-known directories
    if not paths:
        for candidate_dir in (instance_path / "reports", instance_path / "output"):
            if candidate_dir.is_dir():
                for md_file in candidate_dir.glob("*.md"):
                    if md_file.stat().st_size > 0:
                        paths.append(md_file)

    return paths


def _collect_primary_report_paths(
    run_result: WorkspaceRunResult, instance_path: Path
) -> list[Path]:
    """Return the main deliverable reports, excluding auxiliary idea notes when possible."""
    report_paths = _collect_report_paths(run_result, instance_path)
    primary_paths = [path for path in report_paths if "idea" not in path.name.lower()]
    return primary_paths or report_paths


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _count_words(path: Path) -> int:
    return len(_read_text_safe(path).split())


def _count_sections(path: Path) -> int:
    """Count markdown ## headings."""
    return len(re.findall(r"^#{1,3}\s+", _read_text_safe(path), re.MULTILINE))


def _has_citations(path: Path) -> bool:
    text = _read_text_safe(path).lower()
    return bool(
        re.search(r"(references|citations|sources|bibliography)", text)
        or re.search(r"\[[\d]+\]", text)  # [1], [2] style
        or text.count("http") >= 3
    )


def _quality_rank(rating: OutputQualityRating) -> int:
    return {
        OutputQualityRating.UNUSABLE: 0,
        OutputQualityRating.POOR: 1,
        OutputQualityRating.FAIR: 2,
        OutputQualityRating.UNKNOWN: 2,
        OutputQualityRating.GOOD: 3,
        OutputQualityRating.EXCELLENT: 4,
    }.get(rating, 0)


def _scan_stage_notes(instance_path: Path) -> dict[str, Any]:
    """Scan instance for pipeline stage completion notes."""
    notes_dir = instance_path / "stage_notes"
    if not notes_dir.is_dir():
        notes_dir = instance_path / "notes"
    if not notes_dir.is_dir():
        return {"total": 0, "completed": [], "missing": []}

    completed = [
        f.stem for f in notes_dir.iterdir() if f.is_file() and f.stat().st_size > 0
    ]

    expected_final_stages = [
        "final_idea",
        "final_article_structure",
        "article_writer_draft",
    ]
    missing = [s for s in expected_final_stages if s not in completed]

    return {
        "total": len(completed),
        "completed": completed,
        "missing": missing,
    }


def _assess_stability(
    run_result: WorkspaceRunResult,
    previous_evals: list[EvaluationRecord] | None,
) -> StabilityRating:
    """Assess stability based on historical comparisons."""
    if not previous_evals or len(previous_evals) < 2:
        return StabilityRating.UNKNOWN

    # Compare with recent evals of the same workspace
    recent_scores = [e.overall_score for e in previous_evals[-3:]]
    if not recent_scores:
        return StabilityRating.UNKNOWN

    current_score = 0.5  # Placeholder - would extract from run_result
    variance = max(abs(s - current_score) for s in recent_scores)

    if variance < 0.1:
        return StabilityRating.STABLE
    elif variance < 0.3:
        return StabilityRating.MOSTLY_STABLE
    else:
        return StabilityRating.UNSTABLE


def _estimate_cost_usd(tokens: int, duration_seconds: float) -> float:
    """Estimate cost in USD based on tokens and duration."""
    # Simple cost model
    # $0.003 per 1K tokens (input+output)
    token_cost = (tokens / 1000) * 0.003
    # $0.0001 per second of compute
    compute_cost = duration_seconds * 0.0001
    return token_cost + compute_cost


def _extract_iterations(run_result: WorkspaceRunResult) -> int | None:
    """Extract iteration count from run result."""
    # This would be populated by the runtime
    # For now, return None if not explicitly tracked
    return None


def _assess_retrieval_usefulness(run_result: WorkspaceRunResult) -> bool:
    """Assess whether retrieval was useful for this run."""
    if "retrieval_context_injected" in run_result.metrics:
        if bool(run_result.metrics.get("retrieval_context_injected")):
            return True

    retrieval_tool_names = {
        "get_umbrella_memory",
        "search_gmas_knowledge",
        "get_gmas_context",
    }
    tool_calls = run_result.metrics.get("tool_calls", [])
    if isinstance(tool_calls, list):
        for call in tool_calls:
            name = call.get("tool", "") if isinstance(call, dict) else str(call)
            if name in retrieval_tool_names:
                return True

    for key in ("get_umbrella_memory_calls", "gmas_context_calls", "search_gmas_calls"):
        if int(run_result.metrics.get(key, 0) or 0) > 0:
            return True

    return False


def _count_retrieval_hits(run_result: WorkspaceRunResult) -> int:
    """Count how many retrieval hits were used."""
    return int(run_result.metrics.get("retrieval_hits_used", 0) or 0)


def _required_raw_log_inspection(run_result: WorkspaceRunResult) -> bool:
    """Determine if raw logs had to be inspected."""
    # Check if structured summaries were sufficient
    # This is a placeholder - in production would track whether
    # the inspector had to fall back to raw log reading
    return False


def _count_patches_applied(run_result: WorkspaceRunResult) -> int:
    """Count how many patches were applied during this run."""
    return int(run_result.metrics.get("manager_patch_count", 0) or 0)


def _calculate_patch_success_rate(run_result: WorkspaceRunResult) -> float:
    """Calculate the proportion of patches that helped."""
    patches = _count_patches_applied(run_result)
    if patches == 0:
        return 0.0
    return 1.0 if run_result.status == WorkspaceRunStatus.COMPLETED else 0.0


def _check_summary_sufficiency(run_result: WorkspaceRunResult) -> bool:
    """Check if structured summaries were sufficient."""
    return bool(run_result.final_answer or run_result.errors)


def _count_artifacts(run_result: WorkspaceRunResult, instance_path: Path) -> int:
    """Count produced artifacts."""
    if run_result.artifacts:
        return len(run_result.artifacts)
    output_dir = instance_path / "output"
    if not output_dir.exists():
        return 0
    return len(list(output_dir.glob("*")))


def _extract_manager_issues(run_result: WorkspaceRunResult) -> list[str]:
    """Extract manager-level problem indicators."""
    issues = []

    if run_result.errors:
        for error in run_result.errors:
            error_lower = error.lower()
            # Look for manager-level patterns
            if "policy" in error_lower:
                issues.append("policy_violation")
            if "retrieval" in error_lower:
                issues.append("retrieval_failure")
            if "context" in error_lower and "exceed" in error_lower:
                issues.append("context_limit")

    return issues


def _collect_evidence(run_result: WorkspaceRunResult) -> list[str]:
    """Collect evidence for the evaluation."""
    evidence = []

    if run_result.status:
        evidence.append(f"Status: {run_result.status.value}")

    if run_result.final_answer:
        evidence.append(f"Final answer length: {len(run_result.final_answer)} chars")

    if run_result.errors:
        evidence.append(f"Errors: {', '.join(run_result.errors[:3])}")

    if run_result.warnings:
        evidence.append(f"Warnings: {len(run_result.warnings)}")

    if run_result.total_tokens:
        evidence.append(f"Tokens used: {run_result.total_tokens}")

    raw_trace_refs = run_result.metrics.get("raw_trace_paths", [])
    if isinstance(raw_trace_refs, list):
        for ref in raw_trace_refs[:5]:
            evidence.append(f"Trace: {ref}")

    candidate_id = run_result.metrics.get("candidate_id")
    if candidate_id:
        evidence.append(f"Candidate: {candidate_id}")

    return evidence


def _calculate_overall_score(
    *,
    task_success: TaskSuccessRating,
    output_quality: OutputQualityRating,
    stability: StabilityRating,
    cost_effectiveness: float,
    retrieval_useful: bool,
) -> float:
    """Calculate an overall success score (0.0 to 1.0)."""
    # Weight different components
    scores = {
        TaskSuccessRating.COMPLETE: 1.0,
        TaskSuccessRating.PARTIAL: 0.5,
        TaskSuccessRating.FAILED: 0.0,
        TaskSuccessRating.UNKNOWN: 0.3,
    }[task_success]

    quality_scores = {
        OutputQualityRating.EXCELLENT: 1.0,
        OutputQualityRating.GOOD: 0.8,
        OutputQualityRating.FAIR: 0.6,
        OutputQualityRating.POOR: 0.3,
        OutputQualityRating.UNUSABLE: 0.0,
        OutputQualityRating.UNKNOWN: 0.5,
    }[output_quality]

    stability_scores = {
        StabilityRating.STABLE: 1.0,
        StabilityRating.MOSTLY_STABLE: 0.8,
        StabilityRating.UNSTABLE: 0.3,
        StabilityRating.UNKNOWN: 0.5,
    }[stability]

    # Weighted combination
    overall = (
        scores * 0.5
        + quality_scores * 0.3
        + stability_scores * 0.1
        + cost_effectiveness * 0.05
        + (0.1 if retrieval_useful else 0.0)
    )

    return min(1.0, max(0.0, overall))


def _assess_cost_effectiveness(
    cost_usd: float, task_success: TaskSuccessRating
) -> float:
    """Assess whether the cost was justified by results."""
    if task_success == TaskSuccessRating.COMPLETE:
        # Normalize cost - assume $10 is acceptable for a complete task
        return max(0.0, 1.0 - (cost_usd / 10.0))
    elif task_success == TaskSuccessRating.PARTIAL:
        return max(0.0, 1.0 - (cost_usd / 5.0))
    else:
        return 0.0
