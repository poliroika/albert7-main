from umbrella.contracts.models import ReviewContract, ReviewCoverageChecklist, ReviewIssue
from umbrella.contracts.validators import validate_review_contract


def test_review_schema_accepts_bad_generated_oracle_with_required_deltas() -> None:
    review = ReviewContract(
        verdict="revise",
        coverage=ReviewCoverageChecklist(
            policy_conflicts=True,
            oracle_compatibility=True,
            proof_strength=True,
            scope_validity=True,
            runtime_capabilities=True,
            test_validity=True,
        ),
        issues=(
            ReviewIssue(
                code="bad_generated_oracle",
                severity="blocking",
                target_subtask_id="gui-calculation-behavior",
                contract_path="proof.generated_test_contract.oracle_claims",
                invalid_values=("invalid_on_digit_10_rejected",),
                required_deltas=(
                    {
                        "op": "remove",
                        "path": "proof.generated_test_contract.oracle_claims",
                        "values": ["invalid_on_digit_10_rejected"],
                    },
                ),
            ),
        ),
    )

    issues = validate_review_contract(review, phase="subtask_review")

    assert not issues


def test_review_schema_rejects_bad_generated_oracle_without_delta() -> None:
    review = ReviewContract(
        verdict="revise",
        coverage=ReviewCoverageChecklist(
            policy_conflicts=True,
            oracle_compatibility=True,
            proof_strength=True,
            scope_validity=True,
            runtime_capabilities=True,
            test_validity=True,
        ),
        issues=(
            ReviewIssue(
                code="bad_generated_oracle",
                severity="blocking",
                target_subtask_id="gui-calculation-behavior",
                contract_path="proof.generated_test_contract.oracle_claims",
                invalid_values=("invalid_on_digit_10_rejected",),
            ),
        ),
    )

    issues = validate_review_contract(review, phase="subtask_review")

    assert any(
        issue.code == "bad_oracle_issue_missing_required_deltas"
        for issue in issues
    )


def test_notes_do_not_create_bad_oracle_route() -> None:
    review = ReviewContract(
        verdict="revise",
        coverage=ReviewCoverageChecklist(
            policy_conflicts=True,
            oracle_compatibility=True,
            proof_strength=True,
            scope_validity=True,
            runtime_capabilities=True,
            test_validity=True,
        ),
        notes="The generated test contract is internally contradictory.",
    )

    issues = validate_review_contract(review, phase="subtask_review")

    assert not any(issue.code == "bad_generated_oracle" for issue in issues)
