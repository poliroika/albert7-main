from umbrella.contracts.oracle_validator import (
    contract_issues_payload,
    extract_failed_pytest_node_ids,
    generated_oracle_contract_issues,
)


def test_oracle_detector_flags_same_input_accept_and_reject() -> None:
    contract = {
        "interface_model": {
            "events": [
                {
                    "name": "on_digit",
                    "valid_values": list(range(10)),
                    "invalid_values": ["<0", ">9"],
                }
            ]
        },
        "oracle_claims": [
            {
                "claim_id": "invalid_on_digit_10_rejected",
                "source": "interface_model",
                "subject": "on_digit",
                "input_value": 10,
                "accepted": False,
                "expected_display": "0",
                "test_refs": [
                    "tests/test_gui_behavior.py::test_invalid_digit_rejected_out_of_range"
                ],
            },
            {
                "claim_id": "on_digit_10_accepts_operand",
                "source": "interface_model",
                "subject": "on_digit",
                "input_value": 10,
                "accepted": True,
                "expected_display": "10",
                "test_refs": [
                    "tests/test_gui_behavior.py::test_multiple_operations_in_sequence"
                ],
            },
        ],
    }

    issues = generated_oracle_contract_issues(contract, subtask_id="gui")
    payload = contract_issues_payload(issues)

    assert payload[0]["code"] == "bad_generated_oracle"
    assert payload[0]["target_subtask_id"] == "gui"
    assert payload[0]["contract_path"] == "proof.generated_test_contract.oracle_claims"
    assert payload[0]["required_deltas"] == [
        {
            "op": "remove",
            "path": "proof.generated_test_contract.oracle_claims",
            "values": [
                "invalid_on_digit_10_rejected",
                "on_digit_10_accepts_operand",
            ],
        }
    ]


def test_oracle_detector_no_issue_for_multi_digit_sequence_on_digit_1_then_0() -> None:
    contract = {
        "interface_model": {
            "events": [
                {
                    "name": "on_digit",
                    "valid_values": list(range(10)),
                    "invalid_values": ["<0", ">9"],
                }
            ]
        },
        "oracle_claims": [
            {
                "claim_id": "invalid_on_digit_10_rejected",
                "source": "interface_model",
                "subject": "on_digit",
                "input_value": 10,
                "accepted": False,
                "expected_display": "0",
            },
            {
                "claim_id": "number_10_entered_as_digit_sequence",
                "source": "interface_model",
                "subject": "number_entry",
                "input_sequence": ["on_digit(1)", "on_digit(0)"],
                "accepted": True,
                "expected_display": "10",
            },
        ],
    }

    issues = generated_oracle_contract_issues(contract, subtask_id="gui")

    assert not issues


def test_oracle_detector_flags_same_input_two_outputs() -> None:
    contract = {
        "oracle_claims": [
            {
                "claim_id": "first_status",
                "source": "task_requirement",
                "api": "POST /users",
                "input": {"email": ""},
                "accepted": True,
                "expected_status": 201,
            },
            {
                "claim_id": "second_status",
                "source": "task_requirement",
                "api": "POST /users",
                "input": {"email": ""},
                "accepted": True,
                "expected_status": 400,
            },
        ]
    }

    issues = generated_oracle_contract_issues(contract, subtask_id="api")

    assert issues[0].code == "contradictory_required_behavior"


def test_failed_pytest_nodes_are_extracted_from_summary() -> None:
    output = """
FAILED tests/test_gui_behavior.py::TestGUI::test_invalid - AssertionError
FAILED tests\\test_api.py::test_create - assert 500 == 201
"""

    assert extract_failed_pytest_node_ids(output) == (
        "tests/test_gui_behavior.py::TestGUI::test_invalid",
        "tests/test_api.py::test_create",
    )


def test_oracle_detector_enforces_default_generated_test_budget() -> None:
    contract = {
        "oracle_claims": [
            {
                "claim_id": f"claim_{index}",
                "source": "task_requirement",
                "subject": "api",
                "input_value": index,
                "accepted": True,
            }
            for index in range(7)
        ]
    }

    issues = generated_oracle_contract_issues(contract, subtask_id="simple")

    assert issues[0].code == "invalid_generated_test_contract"
    assert "proof budget" in issues[0].message


def test_oracle_detector_allows_typed_budget_override() -> None:
    contract = {
        "proof_budget": {
            "max_generated_tests_per_subtask": 6,
            "allow_expanded_generated_tests": True,
            "override_reason": "complex_workspace_policy",
        },
        "oracle_claims": [
            {
                "claim_id": f"claim_{index}",
                "source": "task_requirement",
                "subject": "api",
                "input_value": index,
                "accepted": True,
            }
            for index in range(7)
        ],
    }

    issues = generated_oracle_contract_issues(contract, subtask_id="complex")

    assert not issues


def test_ungrounded_distinct_outputs_property_routes_to_bad_generated_oracle() -> None:
    contract = {
        "required_properties": ["distinct_inputs_distinct_outputs"],
        "oracle_claims": [
            {
                "claim_id": "golden_case",
                "source": "task_requirement",
                "subject": "calculate",
                "input": ["5+4"],
                "accepted": True,
                "expected_output": "9",
            }
        ],
    }

    issues = generated_oracle_contract_issues(contract, subtask_id="logic")
    payload = contract_issues_payload(issues)

    issue = next(item for item in payload if item["code"] == "bad_generated_oracle")
    assert issue["contract_path"] == "proof.oracle.required_properties"
    assert issue["required_deltas"] == [
        {
            "op": "remove",
            "path": "proof.oracle.required_properties",
            "values": ["distinct_inputs_distinct_outputs"],
        }
    ]


def test_grounded_distinct_outputs_property_is_allowed() -> None:
    contract = {
        "required_properties": ["distinct_inputs_distinct_outputs"],
        "required_property_sources": {
            "distinct_inputs_distinct_outputs": ["task_requirement"]
        },
        "oracle_claims": [
            {
                "claim_id": "unique_output_domain",
                "source": "task_requirement",
                "subject": "slugify",
                "input": "a",
                "accepted": True,
                "expected_output": "a",
            }
        ],
    }

    issues = generated_oracle_contract_issues(contract, subtask_id="logic")

    assert not [issue for issue in issues if issue.code == "bad_generated_oracle"]


def test_counterexample_5plus4_6plus3_removes_distinct_outputs_without_calculator_hardcode() -> None:
    contract = {
        "required_properties": ["distinct_inputs_distinct_outputs"],
        "oracle_claims": [
            {
                "claim_id": "case_a",
                "source": "task_requirement",
                "subject": "binary_operation",
                "input_values": [[5, 4]],
                "accepted": True,
                "expected_output": 9,
                "test_refs": ["tests/test_model.py::test_distinct_outputs"],
            },
            {
                "claim_id": "case_b",
                "source": "task_requirement",
                "subject": "binary_operation",
                "input_values": [[6, 3]],
                "accepted": True,
                "expected_output": 9,
                "test_refs": ["tests/test_model.py::test_distinct_outputs"],
            },
        ],
    }

    issues = generated_oracle_contract_issues(
        contract,
        subtask_id="model",
        failed_node_ids=("tests/test_model.py::test_distinct_outputs",),
    )
    payload = contract_issues_payload(issues)

    bad_oracle = next(item for item in payload if item["code"] == "bad_generated_oracle")
    assert bad_oracle["required_deltas"] == [
        {
            "op": "remove",
            "path": "proof.oracle.required_properties",
            "values": ["distinct_inputs_distinct_outputs"],
        }
    ]
