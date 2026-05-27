"""Tests for review coverage checklist enforcement."""

from umbrella.contracts.models import ReviewContract
from umbrella.contracts.schemas import FULL_REVIEW_COVERAGE
from umbrella.contracts.validators import validate_review_contract


def test_review_revise_requires_full_coverage() -> None:
    review = ReviewContract.from_mapping(
        {
            "verdict": "revise",
            "issues": [
                {
                    "code": "missing_proof",
                    "severity": "blocking",
                    "message": "missing proof",
                }
            ],
        }
    )
    issues = validate_review_contract(review, phase="plan_review")
    assert any(item.code == "review_coverage_incomplete" for item in issues)


def test_review_batch_revise_with_coverage_passes_shape() -> None:
    review = ReviewContract.from_mapping(
        {
            "verdict": "revise",
            "coverage": FULL_REVIEW_COVERAGE,
            "issues": [
                {"code": "missing_proof", "severity": "blocking", "message": "a"},
                {"code": "weak_proof", "severity": "blocking", "message": "b"},
                {"code": "scope_mismatch", "severity": "error", "message": "c"},
            ],
            "required_plan_changes": ["fix s1", "fix s2"],
        }
    )
    issues = validate_review_contract(review, phase="plan_review")
    assert not issues


def test_review_ok_with_full_coverage_and_no_blockers() -> None:
    review = ReviewContract.from_mapping(
        {
            "verdict": "ok",
            "coverage": FULL_REVIEW_COVERAGE,
            "issues": [],
        }
    )
    issues = validate_review_contract(review, phase="plan_review")
    assert not issues
