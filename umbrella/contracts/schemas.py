"""JSON-schema fragments for typed Umbrella contract tools."""

EVIDENCE_REF_SCHEMA = {
    "type": "object",
    "required": ["ref_type", "ref_id", "produced_by"],
    "properties": {
        "ref_type": {
            "type": "string",
            "enum": [
                "ledger_event",
                "verification_report",
                "test_run",
                "artifact",
                "diff",
                "memory_node",
                "harness_candidate",
                "mutation_report",
                "input_sensitivity_report",
            ],
        },
        "ref_id": {"type": "string"},
        "hash": {"type": "string"},
        "produced_by": {
            "type": "string",
            "enum": ["agent", "supervisor", "verifier", "watcher", "harness"],
        },
        "phase": {"type": "string"},
        "subtask_id": {"type": "string"},
        "created_after_event": {"type": "string"},
    },
}

LEDGER_EVIDENCE_REF_SCHEMA = {
    **EVIDENCE_REF_SCHEMA,
    "properties": {
        **EVIDENCE_REF_SCHEMA["properties"],
        "ref_type": {
            "type": "string",
            "enum": [
                "ledger_event",
                "verification_report",
                "test_run",
                "mutation_report",
                "input_sensitivity_report",
            ],
        },
    },
}

VALID_REVIEW_CODES = (
    "missing_proof",
    "weak_proof",
    "manual_proof",
    "unavailable_proof_target",
    "test_tampering_risk",
    "scope_mismatch",
    "policy_violation",
    "insufficient_research_evidence",
    "requires_human_checkpoint",
    "stale_proof_ref",
    "fake_evidence_ref",
    "invalid_evidence_ref",
    "invalid_python_c_proof",
    "non_ledger_evidence_ref",
    "shell_operator_in_argv",
    "proof_after_patch_missing",
    "proof_scope_mismatch",
    "claim_without_proof",
    "test_tampering_detected",
    "verifier_mutation_attempt",
    "memory_without_verified_evidence",
    "legacy_contract_used",
    "llm_judge_only_evidence",
    "greenfield_python_src_layout_policy",
    "unknown_harness_profile",
    "missing_capability_declaration",
    "capability_probe_failed",
    "bad_generated_oracle",
    "plan_contract_issue",
    "inconsistent_generated_oracle",
    "oracle_domain_mismatch",
    "contradictory_required_behavior",
    "invalid_generated_test_contract",
    "proof_execution_infra",
    "capability_probe_environment_mismatch",
    "dependency_provision_required",
    "headless_proof_uses_real_gui_root",
)

PLAN_REVISION_DELTA_SCHEMA = {
    "type": "object",
    "required": ["op", "path"],
    "properties": {
        "op": {"type": "string", "enum": ["remove", "replace", "add"]},
        "path": {"type": "string"},
        "values": {"type": "array", "items": {"type": "string"}},
        "value": {},
        "replacement": {},
        "target_subtask_id": {"type": "string"},
        "source_issue_code": {"type": "string"},
    },
}

GENERATED_TEST_CONTRACT_SCHEMA = {
    "type": "object",
    "description": (
        "Typed oracle model for generated tests. Tests are evidence for these "
        "claims, not the source of truth for contradictory behavior."
    ),
    "properties": {
        "interface_model": {"type": "object"},
        "proof_budget": {
            "type": "object",
            "properties": {
                "max_generated_tests_per_subtask": {"type": "integer"},
                "allow_expanded_generated_tests": {"type": "boolean"},
                "override_reason": {"type": "string"},
            },
        },
        "oracle_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim_id", "source"],
                "properties": {
                    "claim_id": {"type": "string"},
                    "source": {
                        "type": "string",
                        "enum": [
                            "task_requirement",
                            "interface_model",
                            "reference_behavior",
                            "harness_contract",
                        ],
                    },
                    "subject": {"type": "string"},
                    "event": {"type": "string"},
                    "api": {"type": "string"},
                    "input": {},
                    "input_value": {},
                    "input_values": {"type": "array"},
                    "input_sequence": {"type": "array"},
                    "accepted": {"type": "boolean"},
                    "valid": {"type": "boolean"},
                    "expectation": {"type": "string"},
                    "expected_behavior": {"type": "string"},
                    "expected_output": {},
                    "expected_display": {},
                    "expected_status": {},
                    "expected_result": {},
                    "test_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

REVIEW_COVERAGE_SCHEMA = {
    "type": "object",
    "description": (
        "Review coverage checklist. For verdict ok, every field must be true; "
        "true means checked/no blocker, including checked-not-applicable."
    ),
    "required": [
        "policy_conflicts",
        "oracle_compatibility",
        "proof_strength",
        "scope_validity",
        "runtime_capabilities",
        "test_validity",
    ],
    "properties": {
        "policy_conflicts": {"type": "boolean"},
        "oracle_compatibility": {"type": "boolean"},
        "proof_strength": {"type": "boolean"},
        "scope_validity": {"type": "boolean"},
        "runtime_capabilities": {"type": "boolean"},
        "test_validity": {"type": "boolean"},
    },
}

FULL_REVIEW_COVERAGE = {
    "policy_conflicts": True,
    "oracle_compatibility": True,
    "proof_strength": True,
    "scope_validity": True,
    "runtime_capabilities": True,
    "test_validity": True,
}

PROOF_CONTRACT_SCHEMA = {
    "type": "object",
    "required": ["execution", "oracle", "scope"],
    "properties": {
        "execution": {
            "type": "object",
            "required": ["kind", "command"],
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "pytest",
                        "verification_step",
                        "http_boot",
                        "behavioral_http",
                        "input_sensitivity",
                        "mutation_smoke",
                        "metamorphic",
                        "property_test",
                        "import_check",
                        "build",
                        "command",
                    ],
                },
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout_sec": {"type": "integer"},
                "shell": {"type": "boolean"},
                "subdir": {"type": "string"},
                "execution_environment_id": {"type": "string"},
                "environment_id": {"type": "string"},
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
        },
        "execution_environment_id": {"type": "string"},
        "oracle": {
            "type": "object",
            "required": ["oracle_type", "required_properties"],
            "properties": {
                "oracle_type": {
                    "type": "string",
                    "enum": [
                        "unit_assertions",
                        "behavioral_http",
                        "input_sensitivity",
                        "metamorphic",
                        "snapshot",
                        "mutation_kill",
                        "golden_file",
                        "build",
                        "import",
                    ],
                },
                "required_properties": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "distinct_inputs_distinct_outputs",
                            "invalid_input_rejected",
                            "round_trip",
                            "idempotence",
                            "monotonicity",
                            "no_test_tampering",
                            "mutation_killed",
                            "runtime_started",
                            "module_imports",
                            "build_succeeds",
                        ],
                    },
                },
                "negative_cases_required": {"type": "boolean"},
                "input_sensitivity_required": {"type": "boolean"},
            },
        },
        "scope": {
            "type": "object",
            "properties": {
                "files_under_test": {"type": "array", "items": {"type": "string"}},
                "changed_files_expected": {"type": "array", "items": {"type": "string"}},
                "pytest_targets": {"type": "array", "items": {"type": "string"}},
            },
        },
        "anti_gaming": {
            "type": "object",
            "properties": {
                "allows_mock": {"type": "boolean"},
                "allows_snapshot_update": {"type": "boolean"},
                "allows_test_only_change": {"type": "boolean"},
                "requires_real_runtime": {"type": "boolean"},
            },
        },
        "harness_profile": {"type": "string"},
        "harness_options": {
            "type": "object",
            "description": (
                "Harness-specific launch, interaction, evidence, timeout, "
                "and cleanup details. Required for real runtime GUI proof."
            ),
        },
        "generated_test_contract": GENERATED_TEST_CONTRACT_SCHEMA,
        "required_capabilities": {"type": "array", "items": {"type": "string"}},
        "human_claims": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": EVIDENCE_REF_SCHEMA},
    },
}

SUBTASK_MEMORY_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "baseline": {"type": "array", "items": {"type": "string"}},
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "ref"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "codeptr",
                            "knowledge_md",
                            "github_repo",
                            "github_snippet",
                            "web_page",
                            "web_section",
                            "web_search_hit",
                            "palace_finding",
                            "gmas_context",
                            "workspace_file",
                            "mcp_server",
                            "skill",
                            "terminal_tail",
                        ],
                    },
                    "ref": {"type": "string"},
                    "title": {"type": "string"},
                    "inject_mode": {
                        "type": "string",
                        "enum": ["preload", "on_demand", "search_only"],
                    },
                    "max_chars": {"type": "integer"},
                    "source_id": {"type": "string"},
                },
            },
        },
        "palace_search_queries": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}

PHASE_PLAN_SUBTASK_SCHEMA = {
    "type": "object",
    "required": ["id", "title", "goal", "proof"],
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "goal": {"type": "string"},
        "files_to_create": {"type": "array", "items": {"type": "string"}},
        "files_to_change": {"type": "array", "items": {"type": "string"}},
        "files_affected": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "acceptance_claims": {"type": "array", "items": {"type": "string"}},
        "proof": PROOF_CONTRACT_SCHEMA,
        "generated_test_contract": GENERATED_TEST_CONTRACT_SCHEMA,
        "memory_scope": SUBTASK_MEMORY_SCOPE_SCHEMA,
        "allowed_tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extra phase tool names needed by this leaf.",
        },
        "allowed_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extra skills/prompts the execute phase should load.",
        },
        "codeptr_refs": {"type": "array", "items": {"type": "string"}},
        "mcp_refs": {"type": "array", "items": {"type": "string"}},
    },
}

PHASE_PLAN_SCHEMA = {
    "type": "object",
    "required": ["subtasks"],
    "properties": {
        "plan_id": {"type": "string"},
        "run_id": {"type": "string"},
        "workspace_id": {"type": "string"},
        "subtasks": {
            "type": "array",
            "minItems": 1,
            "items": PHASE_PLAN_SUBTASK_SCHEMA,
        },
    },
    "description": (
        "Executable Umbrella phase plan. Use exactly one top-level `subtasks` "
        "array with typed proof objects and put "
        "memory_scope, allowed_tools, allowed_skills, codeptr_refs, and "
        "mcp_refs on each leaf that needs extra context or tools."
    ),
}

REVIEW_ISSUE_SCHEMA = {
    "type": "object",
    "required": ["code", "severity"],
    "properties": {
        "code": {
            "type": "string",
            "enum": list(VALID_REVIEW_CODES),
        },
        "severity": {
            "type": "string",
            "enum": ["info", "warning", "error", "blocking", "human_required"],
        },
        "phase": {"type": "string"},
        "subtask_id": {"type": "string"},
        "target_subtask_id": {"type": "string"},
        "target_path": {"type": "string"},
        "contract_path": {"type": "string"},
        "invalid_values": {"type": "array", "items": {"type": "string"}},
        "required_deltas": {"type": "array", "items": PLAN_REVISION_DELTA_SCHEMA},
        "failure_hash": {"type": "string"},
        "failure_phase": {"type": "string"},
        "production_code_entered": {"type": "boolean"},
        "capability_id": {"type": "string"},
        "env_id": {"type": "string"},
        "env_hash": {"type": "string"},
        "message": {"type": "string"},
        "evidence_refs": {"type": "array", "items": EVIDENCE_REF_SCHEMA},
    },
}

VERIFICATION_REPORT_REF_SCHEMA = {
    "type": "object",
    "required": [
        "report_id",
        "report_hash",
        "workspace_hash",
        "diff_hash",
        "produced_after_event_id",
        "verifier_id",
        "passed",
    ],
    "properties": {
        "report_id": {"type": "string"},
        "report_hash": {"type": "string"},
        "workspace_hash": {"type": "string"},
        "diff_hash": {"type": "string"},
        "produced_after_event_id": {"type": "string"},
        "verifier_id": {"type": "string"},
        "passed": {"type": "boolean"},
        "ledger_hash": {"type": "string"},
        "execution_environment_id": {"type": "string"},
        "env_hash": {"type": "string"},
        "proof_env_hash": {"type": "string"},
        "python_executable": {"type": "string"},
    },
}

COMPLETION_CONTRACT_SCHEMA = {
    "type": "object",
    "required": ["subtask_id", "status", "completed_claims", "changed_files"],
    "properties": {
        "subtask_id": {"type": "string"},
        "status": {"type": "string", "enum": ["done"]},
        "completed_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim_id", "text", "files", "proof_refs"],
                "properties": {
                    "claim_id": {"type": "string"},
                    "text": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "proof_refs": {
                        "type": "array",
                        "items": LEDGER_EVIDENCE_REF_SCHEMA,
                    },
                },
            },
        },
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": LEDGER_EVIDENCE_REF_SCHEMA},
        "verification_report": VERIFICATION_REPORT_REF_SCHEMA,
        "notes": {"type": "string"},
    },
}
