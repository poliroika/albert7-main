import pytest

from umbrella.contracts.models import ContractDelta
from umbrella.contracts.contract_paths import (
    InvalidContractPath,
    is_canonical_contract_path,
    normalize_contract_path,
    suggest_contract_path,
    validate_delta_path,
)


def test_contract_path_normalizes_pytest_targets_alias() -> None:
    assert normalize_contract_path("proof.pytest_targets").path == (
        "proof.scope.pytest_targets"
    )
    assert normalize_contract_path("pytest_targets").path == (
        "proof.scope.pytest_targets"
    )
    assert normalize_contract_path("changed_files_expected").path == (
        "proof.scope.changed_files_expected"
    )


def test_contract_path_rejects_indexed_path_with_suggestion() -> None:
    with pytest.raises(InvalidContractPath) as exc:
        normalize_contract_path("proof.pytest_targets[0]")

    assert exc.value.suggestion == "proof.scope.pytest_targets"


def test_contract_path_rejects_unknown_path() -> None:
    with pytest.raises(InvalidContractPath):
        normalize_contract_path("proof.not_a_real_field")


def test_contract_path_accepts_scope_pytest_targets() -> None:
    assert is_canonical_contract_path("proof.scope.pytest_targets")
    assert normalize_contract_path("proof.scope.pytest_targets").path == (
        "proof.scope.pytest_targets"
    )


def test_validate_delta_path_returns_normalized_copy() -> None:
    raw = {
        "op": "replace",
        "path": "proof.required_properties",
        "values": ["distinct_inputs_distinct_outputs"],
    }

    normalized = validate_delta_path(raw)

    assert normalized is not raw
    assert normalized["path"] == "proof.oracle.required_properties"
    assert suggest_contract_path("proof.required_properties") == (
        "proof.oracle.required_properties"
    )


def test_contract_delta_from_mapping_is_canonical() -> None:
    delta = ContractDelta.from_mapping(
        {
            "op": "remove",
            "path": "proof.required_properties",
            "values": ["distinct_inputs_distinct_outputs"],
            "target_subtask_id": "logic",
            "source_issue_code": "bad_generated_oracle",
        }
    )

    assert delta.path == "proof.oracle.required_properties"
    assert delta.to_payload()["target_subtask_id"] == "logic"
    assert delta.to_payload()["source_issue_code"] == "bad_generated_oracle"
