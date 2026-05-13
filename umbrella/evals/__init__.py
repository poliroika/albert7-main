"""
Evaluation system - workspace run assessment and promotion pipeline.

This package provides:
- Evaluation of workspace runs against success criteria
- Comparison of iterations to determine patch effectiveness
- Classification of patch outcomes
- Promotion candidate identification and eligibility assessment
- Seed workspace protection policies
"""

from umbrella.evals.models import (
    # Enums
    TaskSuccessRating,
    OutputQualityRating,
    StabilityRating,
    PatchOutcome,
    PromotionEligibility,
    # Core models
    EvaluationRecord,
    ComparisonReport,
    PromotionCandidate,
    PromotionDecision,
    # Seed protection
    SeedGuardrail,
    SeedProtectionPolicy,
    # Factory functions
    generate_evaluation_id,
    generate_comparison_id,
    generate_promotion_candidate_id,
)

from umbrella.evals.runner import evaluate_run
from umbrella.evals.comparisons import (
    compare_runs,
    classify_patch_outcome_from_reports,
    get_improvement_magnitude,
)
from umbrella.evals.promotion import (
    build_promotion_candidate,
    decide_promotion,
    apply_promotion_decision,
)
from umbrella.evals.seed_guardrails import (
    create_default_policy,
    load_policy_from_file,
    save_policy_to_file,
    check_promotion_eligibility,
    create_guardrail,
    add_guardrail_to_policy,
    DEFAULT_SEED_GUARDRAILS,
)

__all__ = [
    # Enums
    "TaskSuccessRating",
    "OutputQualityRating",
    "StabilityRating",
    "PatchOutcome",
    "PromotionEligibility",
    # Core models
    "EvaluationRecord",
    "ComparisonReport",
    "PromotionCandidate",
    "PromotionDecision",
    # Seed protection
    "SeedGuardrail",
    "SeedProtectionPolicy",
    # Factory functions
    "generate_evaluation_id",
    "generate_comparison_id",
    "generate_promotion_candidate_id",
    # Runner
    "evaluate_run",
    # Comparisons
    "compare_runs",
    "classify_patch_outcome_from_reports",
    "get_improvement_magnitude",
    # Promotion
    "build_promotion_candidate",
    "decide_promotion",
    "apply_promotion_decision",
    # Seed guardrails
    "create_default_policy",
    "load_policy_from_file",
    "save_policy_to_file",
    "check_promotion_eligibility",
    "create_guardrail",
    "add_guardrail_to_policy",
    "DEFAULT_SEED_GUARDRAILS",
]
