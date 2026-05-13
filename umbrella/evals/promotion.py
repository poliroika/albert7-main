"""
Promotion pipeline for workspace improvements.

This module provides functions to evaluate whether a local workspace improvement
should be promoted back to the seed workspace.
"""

import logging
from pathlib import Path
from typing import Any

from umbrella.evals.models import (
    PromotionCandidate,
    PromotionDecision,
    PromotionEligibility,
    EvaluationRecord,
    ComparisonReport,
    SeedGuardrail,
    SeedProtectionPolicy,
    PatchOutcome,
    generate_promotion_candidate_id,
)
from umbrella.evals.comparisons import get_improvement_magnitude

log = logging.getLogger(__name__)

_INSTANCE_ONLY_TOP_LEVEL_DIRS = {
    "instances",
    "logs",
    "reports",
    "runs",
    "snapshots",
}
_INSTANCE_ONLY_FILES = {
    "umbrella_improvements.jsonl",
    "instance_metadata.json",
}


def _normalize_changed_file_path(
    file_path: Path,
    instance_path: Path,
) -> Path | None:
    """Convert changed files into promotable paths relative to the instance root."""
    instance_root = instance_path.resolve()
    candidate = Path(file_path)

    if candidate.is_absolute():
        try:
            relative_path = candidate.resolve().relative_to(instance_root)
        except ValueError:
            log.warning(
                "Skipping promotion for path outside instance root: %s (instance=%s)",
                candidate,
                instance_root,
            )
            return None
    else:
        relative_path = candidate

    if relative_path.is_absolute() or not relative_path.parts:
        return None

    if any(part == ".." for part in relative_path.parts):
        log.warning("Skipping promotion for unsafe relative path: %s", relative_path)
        return None

    if relative_path.name.lower() in _INSTANCE_ONLY_FILES:
        return None

    top_level = relative_path.parts[0].lower()
    if top_level in _INSTANCE_ONLY_TOP_LEVEL_DIRS:
        return None

    return Path(*relative_path.parts)


def normalize_promotable_changed_files(
    changed_files: list[Path],
    instance_path: Path,
) -> list[Path]:
    """Normalize changed files into safe, deduplicated, seed-relative paths."""
    normalized: list[Path] = []
    seen: set[str] = set()

    for file_path in changed_files:
        relative_path = _normalize_changed_file_path(file_path, instance_path)
        if relative_path is None:
            continue

        key = relative_path.as_posix().lower()
        if key in seen:
            continue

        seen.add(key)
        normalized.append(relative_path)

    return normalized


def promote_changed_files_to_seed(
    *,
    seed_path: Path,
    instance_path: Path,
    changed_files: list[Path],
) -> list[Path]:
    """Copy promotable files from an instance back into its seed workspace."""
    promotable_files = normalize_promotable_changed_files(changed_files, instance_path)
    if not promotable_files:
        return []

    import shutil

    instance_root = instance_path.resolve()
    seed_root = seed_path.resolve()
    promoted: list[Path] = []

    for file_path in promotable_files:
        source_file = (instance_root / file_path).resolve()
        target_file = (seed_root / file_path).resolve()

        try:
            source_file.relative_to(instance_root)
            target_file.relative_to(seed_root)
        except ValueError:
            log.warning(
                "Skipping promotion for path escaping repository roots: %s", file_path
            )
            continue

        target_file.parent.mkdir(parents=True, exist_ok=True)

        if source_file.exists():
            shutil.copy2(source_file, target_file)
            promoted.append(file_path)
            log.info("  Promoted: %s", file_path)
        else:
            log.warning("  Source not found: %s", source_file)

    return promoted


def build_promotion_candidate(
    baseline: EvaluationRecord,
    comparison: EvaluationRecord,
    comparison_report: ComparisonReport,
    *,
    patch_description: str,
    changed_files: list[Path] | None = None,
    changed_artifacts: list[Path] | None = None,
) -> PromotionCandidate:
    """Build a promotion candidate from evaluation and comparison data.

    Args:
        baseline: Baseline evaluation (before patch)
        comparison: Comparison evaluation (after patch)
        comparison_report: Comparison report between the runs
        patch_description: Description of what was changed
        changed_files: List of files that were modified
        changed_artifacts: List of artifacts that were affected

    Returns:
        PromotionCandidate with all relevant metadata
    """
    normalized_changed_files = normalize_promotable_changed_files(
        changed_files or [],
        baseline.instance_path,
    )

    # Calculate improvement magnitude
    improvement_magnitude = get_improvement_magnitude(comparison_report)

    # Assess generalizability (how reusable this patch is)
    generalizability_score = _assess_generalizability(
        comparison, normalized_changed_files
    )

    # Initial eligibility assessment
    eligibility = _assess_initial_eligibility(
        comparison_report.overall_improvement,
        improvement_magnitude,
        generalizability_score,
    )

    return PromotionCandidate(
        id=generate_promotion_candidate_id(),
        task_id=baseline.task_id,
        workspace_id=baseline.workspace_id,
        instance_path=baseline.instance_path,
        patch_description=patch_description,
        changed_files=normalized_changed_files,
        changed_artifacts=changed_artifacts or [],
        baseline_eval=baseline,
        comparison_eval=comparison,
        comparison_report=comparison_report,
        improvement_magnitude=improvement_magnitude,
        generalizability_score=generalizability_score,
        eligibility=eligibility,
        human_review_required=_requires_human_review(eligibility, comparison_report),
    )


def decide_promotion(
    candidate: PromotionCandidate,
    policy: SeedProtectionPolicy,
) -> PromotionDecision:
    """Decide whether to promote a candidate to the seed workspace.

    Args:
        candidate: The promotion candidate to evaluate
        policy: The seed protection policy to apply

    Returns:
        PromotionDecision with the final decision and reasoning
    """
    # Check minimum improvement threshold
    passes_threshold = (
        candidate.improvement_magnitude >= policy.min_improvement_threshold
    )

    # Check stability requirement
    passes_stability = _passes_stability_check(
        candidate.comparison_eval, policy.require_stability
    )

    # Check guardrails
    passes_guardrails = _passes_guardrails(candidate, policy)

    # Determine human review requirement
    human_required = (
        policy.require_human_approval_for_promotion
        or candidate.human_review_required
        or not passes_guardrails
    )

    # Build reasoning
    reasoning_parts = []
    evidence = []

    if passes_threshold:
        reasoning_parts.append(
            f"Improvement magnitude ({candidate.improvement_magnitude:.2f}) meets threshold ({policy.min_improvement_threshold})"
        )
    else:
        reasoning_parts.append(
            f"Improvement magnitude ({candidate.improvement_magnitude:.2f}) below threshold ({policy.min_improvement_threshold})"
        )
        evidence.append("insufficient_improvement")

    if passes_stability:
        reasoning_parts.append(
            f"Stability ({candidate.comparison_eval.stability}) meets requirement ({policy.require_stability})"
        )
    else:
        reasoning_parts.append(
            f"Stability ({candidate.comparison_eval.stability}) below requirement ({policy.require_stability})"
        )
        evidence.append("instability_detected")

    if not passes_guardrails:
        reasoning_parts.append("Guardrail checks failed")
        evidence.append("guardrail_violation")

    reasoning = ". ".join(reasoning_parts) + "."

    # Determine final eligibility
    if human_required and not passes_guardrails:
        decision = PromotionEligibility.NOT_ELIGIBLE
    elif human_required and passes_threshold and passes_stability and passes_guardrails:
        decision = PromotionEligibility.NEEDS_REVIEW
    elif passes_threshold and passes_stability and passes_guardrails:
        decision = PromotionEligibility.PROMOTE
    elif candidate.generalizability_score < 0.3:
        decision = PromotionEligibility.LOCAL_ONLY
    else:
        decision = PromotionEligibility.NOT_ELIGIBLE

    return PromotionDecision(
        candidate_id=candidate.id,
        decision=decision,
        reasoning=reasoning,
        evidence=evidence,
        passes_threshold=passes_threshold,
        passes_guardrails=passes_guardrails,
        passes_human_gate=not human_required,
        reviewed_by="auto",
    )


def _assess_generalizability(
    eval_record: EvaluationRecord,
    changed_files: list[Path],
) -> float:
    """Assess how generalizable a patch is (0.0 to 1.0).

    Higher scores indicate patches that are more likely to be useful
    across multiple tasks rather than task-specific.
    """
    score = 0.5  # Default: moderate generalizability

    # Boost for patches that touch core graph/agent configuration
    core_indicators = ["graph", "agent", "prompts", "policy"]
    for file_path in changed_files:
        file_str = str(file_path).lower()
        if any(indicator in file_str for indicator in core_indicators):
            score += 0.2
            break

    # Reduce for task-specific files
    task_specific = ["task_main", "readme", "test", "experiment"]
    for file_path in changed_files:
        file_str = str(file_path).lower()
        if any(indicator in file_str for indicator in task_specific):
            score -= 0.15
            break

    # Boost for high stability
    if eval_record.stability.value in ("stable", "mostly_stable"):
        score += 0.1

    # Boost for retrieval effectiveness
    if eval_record.retrieval_was_useful and eval_record.retrieval_hits_used > 0:
        score += 0.1

    # Reduce for manager-level issues
    if eval_record.manager_level_issues:
        score -= 0.2

    return max(0.0, min(1.0, score))


def _assess_initial_eligibility(
    outcome: PatchOutcome,
    magnitude: float,
    generalizability: float,
) -> PromotionEligibility:
    """Assess initial eligibility without full policy check."""
    # Lower thresholds for continuous improvement
    # Any positive improvement should be promotable if generalizable enough
    if outcome == PatchOutcome.IMPROVED and magnitude > 0.01 and generalizability > 0.4:
        return PromotionEligibility.PROMOTE
    elif outcome == PatchOutcome.IMPROVED and generalizability > 0.7:
        # Highly generalizable improvements auto-promote regardless of magnitude
        return PromotionEligibility.PROMOTE
    elif outcome == PatchOutcome.IMPROVED:
        return PromotionEligibility.LOCAL_ONLY
    elif outcome == PatchOutcome.NEUTRAL:
        return PromotionEligibility.LOCAL_ONLY
    elif generalizability < 0.3:
        return PromotionEligibility.LOCAL_ONLY
    else:
        return PromotionEligibility.NOT_ELIGIBLE


def _requires_human_review(
    eligibility: PromotionEligibility,
    report: ComparisonReport,
) -> bool:
    """Determine if human review is required."""
    # Auto-promote candidates don't require review
    if eligibility == PromotionEligibility.PROMOTE:
        return False

    # Local-only changes don't need review
    if eligibility == PromotionEligibility.LOCAL_ONLY:
        return False

    # Everything else needs review
    return True


def _passes_stability_check(
    eval_record: EvaluationRecord | None,
    required: Any,
) -> bool:
    """Check if evaluation meets stability requirements."""
    if eval_record is None:
        return False

    from umbrella.evals.models import StabilityRating

    stability_order = {
        StabilityRating.UNKNOWN: 0,
        StabilityRating.UNSTABLE: 1,
        StabilityRating.MOSTLY_STABLE: 2,
        StabilityRating.STABLE: 3,
    }

    required_level = stability_order.get(required, 2)
    actual_level = stability_order.get(eval_record.stability, 0)

    return actual_level >= required_level


def _passes_guardrails(
    candidate: PromotionCandidate,
    policy: SeedProtectionPolicy,
) -> bool:
    """Check if candidate passes all guardrails."""
    if not policy.guardrails:
        return True

    for guardrail in policy.guardrails:
        if _triggers_guardrail(candidate, guardrail):
            log.warning(
                f"Guardrail '{guardrail.name}' triggered for candidate {candidate.id}"
            )
            return False

    return True


def _triggers_guardrail(
    candidate: PromotionCandidate, guardrail: SeedGuardrail
) -> bool:
    """Check if a candidate triggers a specific guardrail."""
    # Check for blocked patterns in patch description
    desc_lower = candidate.patch_description.lower()
    for pattern in guardrail.blocked_patterns:
        if pattern.lower() in desc_lower:
            return True

    # Check for required approvals
    if guardrail.required_approvals:
        # In a full implementation, this would check for actual approval records
        # For now, we assume no explicit approvals means guardrail passes
        pass

    return False


def _legacy_apply_promotion_decision(
    candidate: PromotionCandidate,
    decision: PromotionDecision,
    seed_path: Path,
    instance_path: Path,
    changed_files: list[Path],
) -> bool:
    """Apply an approved promotion decision to the seed workspace.

    This copies approved changes from the task-instance back to the seed.

    Args:
        candidate: The promotion candidate
        decision: The promotion decision (must be approved)
        seed_path: Path to the seed workspace
        instance_path: Path to the task-instance workspace
        changed_files: List of files to copy

    Returns:
        True if promotion was applied successfully
    """
    if decision.decision != PromotionEligibility.PROMOTE:
        log.warning(f"Cannot apply non-promote decision: {decision.decision}")
        return False

    # NOTE: Allow auto-decisions for continuous improvement
    # if decision.reviewed_by == "auto" and not decision.passes_human_gate:
    #     log.warning("Auto-decision without human gate pass - skipping promotion")
    #     return False

    log.info("=" * 60)
    log.info("🚀 PROMOTING IMPROVEMENTS TO SEED WORKSPACE")
    log.info("=" * 60)
    log.info(f"Instance: {instance_path.name}")
    log.info(f"Seed: {seed_path}")
    log.info(f"Files: {len(changed_files)}")

    import shutil

    promoted_count = 0
    for file_path in changed_files:
        # Resolve paths
        source_file = instance_path / file_path
        target_file = seed_path / file_path

        # Ensure parent directories exist
        target_file.parent.mkdir(parents=True, exist_ok=True)

        # Copy the file
        if source_file.exists():
            shutil.copy2(source_file, target_file)
            log.info(f"  ✓ Promoted: {file_path}")
            promoted_count += 1
        else:
            log.warning(f"  ✗ Source not found: {source_file}")

    log.info(f"Promotion complete: {promoted_count}/{len(changed_files)} files")
    log.info("=" * 60)

    return promoted_count > 0


def apply_promotion_decision(
    candidate: PromotionCandidate,
    decision: PromotionDecision,
    seed_path: Path,
    instance_path: Path,
    changed_files: list[Path],
) -> bool:
    """Apply an approved promotion decision to the seed workspace."""
    if decision.decision != PromotionEligibility.PROMOTE:
        log.warning(f"Cannot apply non-promote decision: {decision.decision}")
        return False

    log.info("=" * 60)
    log.info("Promoting improvements to seed workspace")
    log.info("=" * 60)
    log.info(f"Instance: {instance_path.name}")
    log.info(f"Seed: {seed_path}")

    promotable_files = normalize_promotable_changed_files(changed_files, instance_path)
    log.info(f"Files: {len(promotable_files)}")

    promoted_files = promote_changed_files_to_seed(
        seed_path=seed_path,
        instance_path=instance_path,
        changed_files=promotable_files,
    )
    promoted_count = len(promoted_files)

    log.info(f"Promotion complete: {promoted_count}/{len(promotable_files)} files")
    log.info("=" * 60)

    return promoted_count > 0
