"""
Workspace run comparison utilities.

This module provides functions to compare workspace runs and determine
whether patches improved, regressed, or had neutral effects.
"""

import logging

from umbrella.evals.models import (
    ComparisonReport,
    EvaluationRecord,
    PatchOutcome,
    TaskSuccessRating,
    StabilityRating,
    generate_comparison_id,
)

log = logging.getLogger(__name__)


def compare_runs(
    baseline: EvaluationRecord,
    comparison: EvaluationRecord,
    evidence: list[str] | None = None,
) -> ComparisonReport:
    """Compare two workspace runs to determine patch outcome.

    Args:
        baseline: The baseline evaluation record (before patch)
        comparison: The comparison evaluation record (after patch)
        evidence: Optional additional evidence to include

    Returns:
        ComparisonReport with detailed comparison metrics
    """
    # Validate that runs are comparable
    if baseline.task_id != comparison.task_id:
        log.warning(
            f"Comparing runs from different tasks: {baseline.task_id} vs {comparison.task_id}"
        )

    if baseline.workspace_id != comparison.workspace_id:
        log.warning(
            f"Comparing runs from different workspaces: {baseline.workspace_id} vs {comparison.workspace_id}"
        )

    # Calculate score delta
    score_delta = comparison.overall_score - baseline.overall_score

    # Compare task success
    better_outcome = _compare_success_ratings(
        baseline.task_success, comparison.task_success
    )

    # Compare stability
    more_stable = _compare_stability_ratings(baseline.stability, comparison.stability)

    # Compare cost effectiveness
    cost_delta_usd = comparison.total_cost_usd - baseline.total_cost_usd
    more_cost_effective = (
        comparison.total_cost_usd < baseline.total_cost_usd
        if baseline.task_success == comparison.task_success
        else False
    )

    # Determine overall patch outcome
    overall_improvement = _determine_patch_outcome(
        score_delta=score_delta,
        better_outcome=better_outcome,
        more_stable=more_stable,
        more_cost_effective=more_cost_effective,
        baseline_success=baseline.task_success,
        comparison_success=comparison.task_success,
    )

    # Collect evidence
    comparison_evidence = _collect_comparison_evidence(
        baseline, comparison, score_delta, cost_delta_usd
    )
    if evidence:
        comparison_evidence.extend(evidence)

    return ComparisonReport(
        id=generate_comparison_id(),
        task_id=baseline.task_id,
        workspace_id=baseline.workspace_id,
        baseline_run_id=baseline.run_id,
        comparison_run_id=comparison.run_id,
        baseline_score=baseline.overall_score,
        comparison_score=comparison.overall_score,
        score_delta=score_delta,
        baseline_task_success=baseline.task_success,
        comparison_task_success=comparison.task_success,
        baseline_output_quality=baseline.output_quality,
        comparison_output_quality=comparison.output_quality,
        baseline_cost_usd=baseline.total_cost_usd,
        comparison_cost_usd=comparison.total_cost_usd,
        cost_delta_usd=cost_delta_usd,
        baseline_stability=baseline.stability,
        comparison_stability=comparison.stability,
        better_outcome=better_outcome,
        more_stable=more_stable,
        more_cost_effective=more_cost_effective,
        overall_improvement=overall_improvement,
        evidence=comparison_evidence,
    )


def _compare_success_ratings(
    baseline: TaskSuccessRating, comparison: TaskSuccessRating
) -> bool:
    """Determine if the comparison run has better success rating."""
    success_order = {
        TaskSuccessRating.UNKNOWN: 0,
        TaskSuccessRating.FAILED: 1,
        TaskSuccessRating.PARTIAL: 2,
        TaskSuccessRating.COMPLETE: 3,
    }
    return success_order.get(comparison, 0) > success_order.get(baseline, 0)


def _compare_stability_ratings(
    baseline: StabilityRating, comparison: StabilityRating
) -> bool:
    """Determine if the comparison run is more stable."""
    stability_order = {
        StabilityRating.UNKNOWN: 0,
        StabilityRating.UNSTABLE: 1,
        StabilityRating.MOSTLY_STABLE: 2,
        StabilityRating.STABLE: 3,
    }
    return stability_order.get(comparison, 0) > stability_order.get(baseline, 0)


def _determine_patch_outcome(
    *,
    score_delta: float,
    better_outcome: bool,
    more_stable: bool,
    more_cost_effective: bool,
    baseline_success: TaskSuccessRating,
    comparison_success: TaskSuccessRating,
) -> PatchOutcome:
    """Determine the overall patch outcome based on comparison metrics."""
    # Clear improvement: significant score gain or better outcome
    if better_outcome and score_delta > 0.1:
        return PatchOutcome.IMPROVED
    if score_delta > 0.2:
        return PatchOutcome.IMPROVED

    # Clear regression: significant score loss or worse outcome
    if _compare_success_ratings(comparison_success, baseline_success):
        return PatchOutcome.REGRESSED
    if score_delta < -0.2:
        return PatchOutcome.REGRESSED

    # Neutral with improvements in other dimensions
    if abs(score_delta) < 0.1 and (more_stable or more_cost_effective):
        return PatchOutcome.IMPROVED

    # Neutral: small changes without clear benefit
    if abs(score_delta) < 0.1:
        return PatchOutcome.NEUTRAL

    # Inconclusive: mixed signals
    return PatchOutcome.INCONCLUSIVE


def _collect_comparison_evidence(
    baseline: EvaluationRecord,
    comparison: EvaluationRecord,
    score_delta: float,
    cost_delta_usd: float,
) -> list[str]:
    """Collect evidence strings for the comparison."""
    evidence = []

    # Score change
    evidence.append(
        f"Score change: {score_delta:+.2f} ({baseline.overall_score:.2f} → {comparison.overall_score:.2f})"
    )

    # Success change
    if baseline.task_success != comparison.task_success:
        evidence.append(
            f"Task success: {baseline.task_success} → {comparison.task_success}"
        )

    # Quality change
    if baseline.output_quality != comparison.output_quality:
        evidence.append(
            f"Output quality: {baseline.output_quality} → {comparison.output_quality}"
        )

    # Cost change
    if abs(cost_delta_usd) > 0.01:
        evidence.append(f"Cost change: ${cost_delta_usd:+.4f}")

    # Iteration change
    if baseline.iterations_to_completion and comparison.iterations_to_completion:
        iter_delta = (
            comparison.iterations_to_completion - baseline.iterations_to_completion
        )
        if iter_delta != 0:
            evidence.append(
                f"Iterations: {iter_delta:+d} ({baseline.iterations_to_completion} → {comparison.iterations_to_completion})"
            )

    # Token change
    if baseline.total_tokens and comparison.total_tokens:
        token_delta = comparison.total_tokens - baseline.total_tokens
        if abs(token_delta) > 100:
            evidence.append(f"Tokens: {token_delta:+d}")

    # Stability info
    if comparison.stability != StabilityRating.UNKNOWN:
        evidence.append(f"Stability: {comparison.stability}")

    # Retrieval info
    if baseline.retrieval_was_useful != comparison.retrieval_was_useful:
        evidence.append(
            f"Retrieval usefulness changed: {comparison.retrieval_was_useful}"
        )

    # Manager issues
    if comparison.manager_level_issues:
        evidence.append(f"Manager issues: {', '.join(comparison.manager_level_issues)}")

    return evidence


def classify_patch_outcome_from_reports(
    reports: list[ComparisonReport],
) -> PatchOutcome:
    """Classify overall patch outcome from multiple comparison reports.

    Useful for aggregating results across multiple runs.

    Args:
        reports: List of comparison reports

    Returns:
        Overall PatchOutcome classification
    """
    if not reports:
        return PatchOutcome.INCONCLUSIVE

    # Count outcomes
    outcome_counts: dict[PatchOutcome, int] = {
        PatchOutcome.IMPROVED: 0,
        PatchOutcome.REGRESSED: 0,
        PatchOutcome.NEUTRAL: 0,
        PatchOutcome.INCONCLUSIVE: 0,
    }

    for report in reports:
        outcome_counts[report.overall_improvement] += 1

    total = len(reports)

    # Clear majority for improvement or regression
    if outcome_counts[PatchOutcome.IMPROVED] / total >= 0.6:
        return PatchOutcome.IMPROVED
    if outcome_counts[PatchOutcome.REGRESSED] / total >= 0.6:
        return PatchOutcome.REGRESSED

    # Mostly neutral
    if outcome_counts[PatchOutcome.NEUTRAL] / total >= 0.6:
        return PatchOutcome.NEUTRAL

    # Mixed or unclear
    return PatchOutcome.INCONCLUSIVE


def get_improvement_magnitude(report: ComparisonReport) -> float:
    """Calculate the magnitude of improvement (-1.0 to 1.0).

    Returns:
        Float from -1.0 (significant regression) to 1.0 (significant improvement)
        with 0.0 being neutral.
    """
    # Normalize score delta to roughly -1 to 1 range
    # Assuming typical scores are in 0-1 range
    normalized_delta = max(-1.0, min(1.0, report.score_delta * 2))

    # Boost for categorical improvements
    multiplier = 1.0
    if report.overall_improvement == PatchOutcome.IMPROVED:
        multiplier = 1.2
    elif report.overall_improvement == PatchOutcome.REGRESSED:
        multiplier = 1.2

    return normalized_delta * multiplier
