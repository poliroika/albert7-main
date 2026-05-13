"""
Evaluation system for workspace runs and patches.

This module defines the schema for evaluating workspace iterations,
comparing runs, and determining promotion eligibility.
"""

import time
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# =============================================================================
# Enums
# =============================================================================


class TaskSuccessRating(StrEnum):
    """How well did the workspace achieve its task?"""

    COMPLETE = "complete"  # Task fully achieved
    PARTIAL = "partial"  # Some progress made but incomplete
    FAILED = "failed"  # No meaningful progress
    UNKNOWN = "unknown"  # Unable to determine


class OutputQualityRating(StrEnum):
    """Quality assessment of produced artifacts."""

    EXCELLENT = "excellent"  # Production-ready, high quality
    GOOD = "good"  # Usable with minor issues
    FAIR = "fair"  # Has issues but usable
    POOR = "poor"  # Significant issues, barely usable
    UNUSABLE = "unusable"  # Output cannot be used
    UNKNOWN = "unknown"  # Unable to assess


class StabilityRating(StrEnum):
    """Repeatability assessment of the workspace."""

    STABLE = "stable"  # Consistent results across runs
    MOSTLY_STABLE = "mostly_stable"  # Minor variations
    UNSTABLE = "unstable"  # Inconsistent results
    UNKNOWN = "unknown"  # Insufficient data


class PatchOutcome(StrEnum):
    """Did the patch improve the workspace?"""

    IMPROVED = "improved"
    REGRESSED = "regressed"
    NEUTRAL = "neutral"
    INCONCLUSIVE = "inconclusive"


class PromotionEligibility(StrEnum):
    """Is this improvement ready for the seed workspace?"""

    PROMOTE = "promote"  # Clear improvement, seed-eligible
    LOCAL_ONLY = "local_only"  # Task-specific, not for seed
    NEEDS_REVIEW = "needs_review"  # Requires human evaluation
    NOT_ELIGIBLE = "not_eligible"  # Not good enough
    UNKNOWN = "unknown"


# =============================================================================
# Evaluation Models
# =============================================================================


class EvaluationRecord(BaseModel):
    """Record of a single workspace run evaluation."""

    id: str = Field(default_factory=lambda: f"eval_{int(time.time())}")
    task_id: str
    workspace_id: str
    run_id: str
    instance_path: Path

    # Task success assessment
    task_success: TaskSuccessRating
    output_quality: OutputQualityRating
    stability: StabilityRating

    # Cost metrics
    total_tokens: int
    total_duration_seconds: float
    total_cost_usd: float = 0.0

    # Iteration metrics
    iterations_to_completion: int | None = None
    iterations_limit_reached: bool = False

    # Retrieval assessment
    retrieval_was_useful: bool = True
    retrieval_hits_used: int = 0
    raw_log_inspection_required: bool = False

    # Patch effectiveness
    patches_applied: int = 0
    patch_success_rate: float = 0.0  # Portion of patches that helped

    # Observability quality
    structured_summary_sufficient: bool = True
    artifact_count: int = 0

    # Manager signals
    manager_level_issues: list[str] = Field(default_factory=list)

    # Overall score (0.0 to 1.0)
    overall_score: float = 0.0

    # Evidence and notes
    evidence: list[str] = Field(default_factory=list)
    evaluator_notes: str = ""

    # Timestamps
    created_at: float = Field(default_factory=time.time)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ComparisonReport(BaseModel):
    """Comparison between two workspace runs."""

    id: str = Field(default_factory=lambda: f"compare_{int(time.time())}")
    task_id: str
    workspace_id: str

    # Runs being compared
    baseline_run_id: str
    comparison_run_id: str

    # Score comparison
    baseline_score: float
    comparison_score: float
    score_delta: float

    # Outcome comparison
    baseline_task_success: TaskSuccessRating
    comparison_task_success: TaskSuccessRating
    baseline_output_quality: OutputQualityRating
    comparison_output_quality: OutputQualityRating

    # Cost comparison
    baseline_cost_usd: float
    comparison_cost_usd: float
    cost_delta_usd: float

    # Stability comparison
    baseline_stability: StabilityRating
    comparison_stability: StabilityRating

    # Verdict
    better_outcome: bool = False
    more_stable: bool = False
    more_cost_effective: bool = False
    overall_improvement: PatchOutcome = PatchOutcome.NEUTRAL

    # Evidence
    evidence: list[str] = Field(default_factory=list)

    # Timestamp
    created_at: float = Field(default_factory=time.time)


# =============================================================================
# Promotion Models
# =============================================================================


class PromotionCandidate(BaseModel):
    """A workspace improvement that might be promoted to seed."""

    id: str = Field(default_factory=lambda: f"promo_{int(time.time())}")
    task_id: str
    workspace_id: str
    instance_path: Path

    # What changed
    patch_description: str
    changed_files: list[Path] = Field(default_factory=list)
    changed_artifacts: list[Path] = Field(default_factory=list)

    # Evidence
    baseline_eval: EvaluationRecord | None = None
    comparison_eval: EvaluationRecord | None = None
    comparison_report: ComparisonReport | None = None

    # Metrics
    improvement_magnitude: float = 0.0  # How much better (-1.0 to 1.0)
    generalizability_score: float = 0.0  # How reusable (0.0 to 1.0)

    # Classification
    eligibility: PromotionEligibility = PromotionEligibility.UNKNOWN

    # Required for promotion
    human_review_required: bool = False
    human_review_status: Literal["pending", "approved", "rejected"] | None = None

    # Promotion metadata
    promoted_to_seed: bool = False
    promoted_at: float | None = None
    promoted_by: str | None = None  # "human" or "auto"

    # Timestamp
    created_at: float = Field(default_factory=time.time)


class PromotionDecision(BaseModel):
    """Decision on whether to promote an improvement."""

    candidate_id: str
    decision: PromotionEligibility

    reasoning: str
    evidence: list[str] = Field(default_factory=list)

    # Conditions checked
    passes_threshold: bool = False
    passes_guardrails: bool = False
    passes_human_gate: bool = True  # Default true unless human required

    # Review info
    reviewed_by: Literal["auto", "human"] = "auto"
    reviewed_at: float = Field(default_factory=time.time)

    # Human review (if required)
    human_reviewer: str | None = None
    human_comments: str | None = None


# =============================================================================
# Seed Guardrails
# =============================================================================


class SeedGuardrail(BaseModel):
    """Protection rule for seed workspace modifications."""

    id: str
    name: str
    description: str

    # Rule definition
    blocked_patterns: list[str] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)

    # Metrics
    times_triggered: int = 0
    last_triggered_at: float | None = None


class SeedProtectionPolicy(BaseModel):
    """Policy governing what can be promoted to seed workspaces."""

    enabled: bool = True
    require_human_approval_for_promotion: bool = False
    min_improvement_threshold: float = 0.1  # Minimum improvement to consider promotion
    min_runs_for_promotion: int = 2  # Minimum successful runs before promotion
    require_stability: StabilityRating = StabilityRating.MOSTLY_STABLE

    # Guardrails
    guardrails: list[SeedGuardrail] = Field(default_factory=list)


# =============================================================================
# Factory Functions
# =============================================================================


def generate_evaluation_id() -> str:
    """Generate a unique evaluation ID."""
    import uuid

    return f"eval_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_comparison_id() -> str:
    """Generate a unique comparison ID."""
    import uuid

    return f"compare_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_promotion_candidate_id() -> str:
    """Generate a unique promotion candidate ID."""
    import uuid

    return f"promo_{int(time.time())}_{uuid.uuid4().hex[:8]}"
