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
)

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

