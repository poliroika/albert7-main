from __future__ import annotations

import json
from pathlib import Path

from umbrella.analysis import analyze_jsts_test_source, analyze_python_test_source
from umbrella.contracts import (
    CompletionContract,
    ContractBundle,
    ContractEnvelope,
    ContractValidator,
    EvidenceRef,
    EvidenceResolver,
    ProofAntiGamingSpec,
    ProofExecutionSpec,
    ProofOracleSpec,
    ProofScopeSpec,
    ProofSpec,
    ReviewContract,
    VerificationReportRef,
    build_workspace_context,
    diff_hash,
    hash_value,
    validate_envelope,
    validate_proof_spec,
    validate_review_contract,
    validate_verification_report_ref,
    workspace_hash,
)
from umbrella.contracts.compiler import ContractCompiler
from umbrella.contracts.plan_ir import canonicalize_phase_plan, compile_phase_plan
from umbrella.contracts.schemas import REVIEW_ISSUE_SCHEMA, VALID_REVIEW_CODES
from umbrella.contracts.validators import VALID_REVIEW_CODES as VALIDATOR_REVIEW_CODES
from umbrella.enforcement.ledger import append_supervisor_ledger_event


def _workspace(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path
    workspace_id = "ws"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_app.py").write_text(
        "from src.app import add\n\n"
        "def test_add_behavior():\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return repo, workspace, workspace_id


def _valid_pytest_proof() -> ProofSpec:
    return ProofSpec(
        execution=ProofExecutionSpec(
            kind="pytest",
            command=("python", "-m", "pytest", "tests/test_app.py::test_add_behavior", "-q"),
        ),
        oracle=ProofOracleSpec(
            oracle_type="unit_assertions",
            required_properties=("distinct_inputs_distinct_outputs",),
            negative_cases_required=True,
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/app.py",),
            changed_files_expected=("src/app.py",),
            pytest_targets=("tests/test_app.py::test_add_behavior",),
        ),
        anti_gaming=ProofAntiGamingSpec(requires_real_runtime=True),
        human_claims=("addition uses operands instead of a constant",),
    )


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_review_issue_schema_enum_matches_validator_codes():
    schema_codes = set(REVIEW_ISSUE_SCHEMA["properties"]["code"]["enum"])
    assert schema_codes == set(VALID_REVIEW_CODES)
    assert schema_codes == set(VALIDATOR_REVIEW_CODES)
    assert "greenfield_python_src_layout_policy" in schema_codes


def test_compile_phase_plan_preserves_context_contract_fields():
    plan, issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "gui-runtime-proof",
                    "title": "GUI runtime proof",
                    "goal": "Launch the desktop calculator and verify one click path.",
                    "files_to_change": ["src/calculator_app.py"],
                    "allowed_tools": ["shell"],
                    "allowed_skills": ["desktop-gui-testing"],
                    "codeptr_refs": ["ek:tkinter-button-grid"],
                    "mcp_refs": ["display-server"],
                    "memory_scope": {
                        "assets": [
                            {
                                "kind": "knowledge_md",
                                "ref": "ek:tkinter-button-grid",
                                "inject_mode": "preload",
                            }
                        ],
                        "notes": "Load GUI testing guidance before proof.",
                    },
                    "proof": {
                        "execution": {
                            "kind": "command",
                            "command": ["python", "scripts/gui_smoke.py"],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["runtime_started"],
                        },
                        "scope": {
                            "files_under_test": ["src/calculator_app.py"],
                            "changed_files_expected": ["src/calculator_app.py"],
                        },
                    },
                }
            ]
        },
        run_id="run-1",
        workspace_id="calculator",
    )

    assert issues == []
    assert plan is not None
    subtask = plan.subtasks[0]
    assert subtask.memory_scope["notes"] == "Load GUI testing guidance before proof."
    assert subtask.allowed_tools == ("shell",)
    assert subtask.allowed_skills == ("desktop-gui-testing",)
    assert subtask.codeptr_refs == ("ek:tkinter-button-grid",)
    assert subtask.mcp_refs == ("display-server",)


def test_canonicalize_phase_plan_flattens_legacy_phase_aliases():
    plan = canonicalize_phase_plan(
        {
            "plan_id": "demo",
            "workspace_id": "calculator",
            "phases": [
                {
                    "id": "gui",
                    "title": "GUI wrapper",
                    "subtasks": [
                        {
                            "id": "gui-runtime",
                            "title": "GUI runtime",
                            "goal": "Launch and click the calculator.",
                            "proof": {
                                "execution": {
                                    "kind": "command",
                                    "command": ["python", "tests/gui_smoke.py"],
                                },
                                "oracle": {
                                    "oracle_type": "unit_assertions",
                                    "required_properties": ["runtime_started"],
                                },
                                "scope": {
                                    "files_under_test": ["src/calculator_app.py"],
                                    "changed_files_expected": ["src/calculator_app.py"],
                                },
                            },
                        }
                    ],
                }
            ],
        }
    )

    assert plan["plan_id"] == "demo"
    assert plan["workspace_id"] == "calculator"
    assert "phases" not in plan
    assert [item["id"] for item in plan["subtasks"]] == ["gui-runtime"]


def test_compile_phase_plan_rejects_unknown_proof_metadata_key():
    plan, issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "gui-runtime",
                    "title": "GUI runtime",
                    "goal": "Launch and click the calculator.",
                    "proof": {
                        "execution": {
                            "kind": "command",
                            "command": ["python", "tests/gui_smoke.py"],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["runtime_started"],
                        },
                        "scope": {
                            "files_under_test": ["src/calculator_app.py"],
                            "changed_files_expected": ["src/calculator_app.py"],
                        },
                        "ant i_gaming": {"requires_real_runtime": True},
                    },
                }
            ]
        },
        run_id="run-1",
        workspace_id="calculator",
    )

    assert plan is not None
    assert any(issue.code == "invalid_plan_contract" for issue in issues)
    assert "`ant i_gaming`" in " ".join(issue.message for issue in issues)


def test_unknown_contract_version_fails():
    envelope = ContractEnvelope(
        schema_name="plan",
        schema_version="0",
        run_id="r",
        phase="plan",
        actor="agent",
        payload={},
    )

    assert "unknown_contract_version" in _codes(validate_envelope(envelope))


def test_compile_plan_rejects_legacy_success_test():
    plan, issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "s1",
                    "title": "legacy",
                    "goal": "old proof",
                    "files_to_change": ["src/app.py"],
                    "success_test": "python -m pytest tests/test_app.py -q",
                }
            ]
        }
    )

    assert plan is not None
    assert "legacy_contract_used" in _codes(issues)


def test_plan_contract_blocks_candidate_control_paths_and_workspace_escapes():
    plan, compile_issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "control",
                    "title": "control file",
                    "goal": "bad plan",
                    "files_to_change": [
                        "workspace.toml",
                        ".git/config",
                        "workspaces/calculator/workspace.toml",
                    ],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": ["python", "-m", "pytest", "tests/test_app.py", "-q"],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["no_test_tampering"],
                        },
                        "scope": {
                            "files_under_test": ["../umbrella/runner.py"],
                            "changed_files_expected": ["verification.toml"],
                            "pytest_targets": ["tests/test_app.py"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                    },
                }
            ]
        }
    )

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    codes = _codes(issues)
    assert "candidate_control_path_forbidden" in codes
    assert "candidate_path_outside_workspace" in codes
    assert any(
        "workspaces/calculator/workspace.toml" in issue.message
        and "repository-relative" in issue.message
        for issue in issues
    )


def test_plan_contract_requires_no_test_tampering_on_test_changing_subtask():
    plan, compile_issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "core",
                    "title": "Core",
                    "goal": "Implement core and tests.",
                    "files_to_create": ["src/app.py", "tests/test_app.py"],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": ["python", "-m", "pytest", "tests/test_app.py", "-q"],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["distinct_inputs_distinct_outputs"],
                        },
                        "scope": {
                            "files_under_test": ["src/app.py"],
                            "changed_files_expected": ["src/app.py", "tests/test_app.py"],
                            "pytest_targets": ["tests/test_app.py"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                    },
                }
            ]
        }
    )

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    assert "test_tampering_risk" in _codes(issues)
    assert any(issue.subtask_id == "core" for issue in issues)


def test_plan_contract_accepts_no_test_tampering_on_same_subtask():
    plan, compile_issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "core",
                    "title": "Core",
                    "goal": "Implement core and tests.",
                    "files_to_create": ["src/app.py", "tests/test_app.py"],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": ["python", "-m", "pytest", "tests/test_app.py", "-q"],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": [
                                "distinct_inputs_distinct_outputs",
                                "no_test_tampering",
                            ],
                        },
                        "scope": {
                            "files_under_test": ["src/app.py"],
                            "changed_files_expected": ["src/app.py", "tests/test_app.py"],
                            "pytest_targets": ["tests/test_app.py"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                        "generated_test_contract": {
                            "interface_model": {
                                "events": [
                                    {
                                        "name": "app_behavior",
                                        "valid_values": ["basic_case"],
                                    }
                                ]
                            },
                            "proof_budget": {
                                "max_generated_tests_per_subtask": 6,
                                "allow_expanded_generated_tests": False,
                            },
                            "oracle_claims": [
                                {
                                    "claim_id": "basic_case_returns_expected_output",
                                    "source": "task_requirement",
                                    "subject": "app_behavior",
                                    "input_values": ["basic_case"],
                                    "accepted": True,
                                    "expected_behavior": "returns expected output",
                                    "test_refs": ["tests/test_app.py"],
                                }
                            ],
                        },
                    },
                }
            ]
        }
    )

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    assert "test_tampering_risk" not in _codes(issues)
    assert "invalid_generated_test_contract" not in _codes(issues)


def test_plan_contract_requires_generated_test_contract_for_generated_pytest_oracle():
    plan, compile_issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "core",
                    "title": "Core",
                    "goal": "Implement core and generated tests.",
                    "files_to_create": ["src/app.py", "tests/test_app.py"],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": ["python", "-m", "pytest", "tests/test_app.py", "-q"],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": [
                                "distinct_inputs_distinct_outputs",
                                "no_test_tampering",
                            ],
                        },
                        "scope": {
                            "files_under_test": ["src/app.py"],
                            "changed_files_expected": ["src/app.py", "tests/test_app.py"],
                            "pytest_targets": ["tests/test_app.py"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                    },
                }
            ]
        }
    )

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    assert "invalid_generated_test_contract" in _codes(issues)
    issue = next(issue for issue in issues if issue.code == "invalid_generated_test_contract")
    assert issue.contract_path == "proof.generated_test_contract"
    assert issue.required_deltas


def test_no_test_tampering_scope_cannot_overlap_only_changed_test_file():
    plan, compile_issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "setup",
                    "title": "Setup",
                    "goal": "Create package and tests.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/app/__init__.py",
                        "tests/__init__.py",
                    ],
                    "proof": {
                        "execution": {
                            "kind": "import_check",
                            "command": [
                                "python",
                                "-c",
                                "import sys; sys.path.insert(0, 'src'); import app",
                            ],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["module_imports", "no_test_tampering"],
                        },
                        "scope": {
                            "files_under_test": ["tests/__init__.py"],
                            "changed_files_expected": [
                                "pyproject.toml",
                                "src/app/__init__.py",
                                "tests/__init__.py",
                            ],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                    },
                }
            ]
        }
    )

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    assert "proof_scope_mismatch" in _codes(issues)
    assert any(
        issue.subtask_id == "setup"
        and "test-file overlap alone" in issue.message
        for issue in issues
    )


def test_no_test_tampering_scope_allows_pure_test_verification_subtask():
    plan, compile_issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "e2e-tests",
                    "title": "E2E tests",
                    "goal": "Add a verification-only test.",
                    "files_to_create": ["tests/test_e2e.py"],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": ["python", "-m", "pytest", "tests/test_e2e.py", "-q"],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["runtime_started", "no_test_tampering"],
                        },
                        "scope": {
                            "files_under_test": ["tests/test_e2e.py"],
                            "changed_files_expected": ["tests/test_e2e.py"],
                            "pytest_targets": ["tests/test_e2e.py"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                    },
                }
            ]
        }
    )

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    assert "proof_scope_mismatch" not in _codes(issues)


def test_plan_contract_allows_workspace_toml_as_workspace_manifest_path():
    plan, compile_issues = compile_phase_plan(
        {
            "subtasks": [
                {
                    "id": "workspace-manifest",
                    "title": "workspace manifest",
                    "goal": "Add workspace verification metadata.",
                    "files_to_change": ["workspace.toml"],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": ["python", "-m", "pytest", "tests/test_app.py", "-q"],
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["distinct_inputs_distinct_outputs"],
                            "negative_cases_required": True,
                        },
                        "scope": {
                            "files_under_test": ["src/app.py"],
                            "changed_files_expected": ["src/app.py", "workspace.toml"],
                            "pytest_targets": ["tests/test_app.py"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                    },
                }
            ]
        }
    )

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    assert "candidate_control_path_forbidden" not in _codes(issues)


def test_valid_pytest_proof_passes_contract_validation():
    assert validate_proof_spec(_valid_pytest_proof()) == []


def test_plan_blocks_import_only_proof_for_production_source_leaf():
    raw_plan = {
        "subtasks": [
            {
                "id": "gmas-agents",
                "title": "Implement GMAS multi-agent system",
                "goal": (
                    "Create GMAS agents with proper schemas for LLM-powered "
                    "Civ bots including economic planner, diplomat, and "
                    "strategic advisor agents"
                ),
                "files_to_create": [
                    "src/civgame/agents/graph.py",
                    "src/civgame/agents/bots.py",
                    "src/civgame/agents/decisions.py",
                    "tests/test_gmas_agents.py",
                ],
                "proof": {
                    "execution": {
                        "kind": "import_check",
                        "command": [
                            "python",
                            "-c",
                            (
                                "from src.civgame.agents.graph import "
                                "create_civ_agent_graph"
                            ),
                        ],
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["module_imports"],
                    },
                    "scope": {
                        "files_under_test": ["src/civgame/agents/*.py"],
                        "changed_files_expected": [
                            "src/civgame/agents/*.py",
                            "tests/test_gmas_agents.py",
                        ],
                    },
                    "anti_gaming": {"allows_mock": False},
                    "human_claims": [
                        "GMAS agent graph can be constructed with all required agents"
                    ],
                },
            }
        ]
    }
    plan, compile_issues = compile_phase_plan(raw_plan, run_id="r", workspace_id="ws")

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    assert "weak_proof" in _codes(issues)


def test_plan_allows_import_check_for_package_init_only_leaf():
    raw_plan = {
        "subtasks": [
            {
                "id": "package-export",
                "title": "Expose package namespace",
                "goal": "Create package init exports",
                "files_to_create": ["src/civgame/__init__.py"],
                "proof": {
                    "execution": {
                        "kind": "import_check",
                        "command": ["python", "-c", "import src.civgame"],
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["module_imports"],
                    },
                    "scope": {
                        "files_under_test": ["src/civgame/__init__.py"],
                        "changed_files_expected": ["src/civgame/__init__.py"],
                    },
                },
            }
        ]
    }
    plan, compile_issues = compile_phase_plan(raw_plan, run_id="r", workspace_id="ws")

    assert compile_issues == []
    assert plan is not None
    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id="ws", plan=plan)
    )

    assert "weak_proof" not in _codes(issues)


def test_review_ok_cannot_downgrade_weak_proof_to_warning():
    review = ReviewContract.from_mapping(
        {
            "verdict": "ok",
            "issues": [
                {
                    "code": "weak_proof",
                    "severity": "warning",
                    "phase": "plan",
                    "subtask_id": "gmas-agents",
                    "message": "import_check does not prove agent behavior",
                }
            ],
        }
    )

    issues = validate_review_contract(review, phase="plan_review")

    assert "review_ok_with_blocking_issue" in _codes(issues)


def test_contract_compiler_ignores_plan_review_older_than_latest_submitted_plan(
    tmp_path: Path,
):
    repo, workspace, workspace_id = _workspace(tmp_path)
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    raw_plan = {
        "subtasks": [
            {
                "id": "s1",
                "title": "repair",
                "goal": "repair old weak proof",
                "files_to_change": ["src/app.py"],
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_app.py::test_add_behavior",
                            "-q",
                        ],
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["distinct_inputs_distinct_outputs"],
                        "negative_cases_required": True,
                    },
                    "scope": {
                        "files_under_test": ["src/app.py"],
                        "changed_files_expected": ["src/app.py"],
                        "pytest_targets": ["tests/test_app.py::test_add_behavior"],
                    },
                    "anti_gaming": {"requires_real_runtime": True},
                },
            }
        ]
    }
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "workspace_id": workspace_id,
                "plan_id": "phase_plan:repair",
                "plan": raw_plan,
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "created_at": 10.0,
            "kind": "submit_phase_plan",
            "payload": {"plan_id": "phase_plan:weak"},
            "task_id": "run-1:plan",
            "phase": "plan",
        },
        {
            "created_at": 20.0,
            "kind": "submit_micro_review",
            "payload": {
                "verdict": "revise",
                "issues": [
                    {
                        "code": "weak_proof",
                        "severity": "blocking",
                        "phase": "plan",
                        "subtask_id": "old-websocket",
                        "message": "old submitted plan had a weak proof",
                    }
                ],
            },
            "task_id": "run-1:plan_review",
            "phase": "plan_review",
        },
        {
            "created_at": 30.0,
            "kind": "submit_phase_plan",
            "payload": {"plan_id": "phase_plan:repair"},
            "task_id": "run-1:plan",
            "phase": "plan",
        },
    ]
    (state / "phase_control_signals.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    bundle = ContractCompiler.from_run(
        repo_root=repo,
        drive_root=drive,
        workspace_id=workspace_id,
        run_id="run-1",
    )
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=workspace_id,
    )

    assert bundle.reviews == ()
    assert "weak_proof" not in _codes(
        ContractValidator.validate(bundle, context=context)
    )


def test_contract_compiler_keeps_plan_review_after_latest_submitted_plan(
    tmp_path: Path,
):
    repo, workspace, workspace_id = _workspace(tmp_path)
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    raw_plan = {
        "subtasks": [
            {
                "id": "s1",
                "title": "still weak",
                "goal": "needs review",
                "files_to_change": ["src/app.py"],
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_app.py::test_add_behavior",
                            "-q",
                        ],
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["distinct_inputs_distinct_outputs"],
                    },
                    "scope": {
                        "files_under_test": ["src/app.py"],
                        "changed_files_expected": ["src/app.py"],
                        "pytest_targets": ["tests/test_app.py::test_add_behavior"],
                    },
                    "anti_gaming": {"requires_real_runtime": True},
                },
            }
        ]
    }
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "workspace_id": workspace_id,
                "plan_id": "phase_plan:still-weak",
                "plan": raw_plan,
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "created_at": 10.0,
            "kind": "submit_phase_plan",
            "payload": {"plan_id": "phase_plan:still-weak"},
            "task_id": "run-1:plan",
            "phase": "plan",
        },
        {
            "created_at": 20.0,
            "kind": "submit_micro_review",
            "payload": {
                "verdict": "revise",
                "issues": [
                    {
                        "code": "weak_proof",
                        "severity": "blocking",
                        "phase": "plan",
                        "subtask_id": "s1",
                        "message": "current plan is still weak",
                    }
                ],
            },
            "task_id": "run-1:plan_review",
            "phase": "plan_review",
        },
    ]
    (state / "phase_control_signals.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    bundle = ContractCompiler.from_run(
        repo_root=repo,
        drive_root=drive,
        workspace_id=workspace_id,
        run_id="run-1",
    )
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=workspace_id,
    )

    assert len(bundle.reviews) == 1
    assert "weak_proof" in _codes(
        ContractValidator.validate(bundle, context=context)
    )


def test_contract_compiler_does_not_treat_available_llm_as_llm_task(
    tmp_path: Path,
) -> None:
    repo, workspace, workspace_id = _workspace(tmp_path)
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "workspace_id": workspace_id,
                "capabilities": {
                    "python": {"available": True, "source": "probe"},
                    "subprocess": {"available": True, "source": "probe"},
                    "llm_api": {"available": True, "source": "probe"},
                },
                "notes": "LLM runtime is available to the runner, but this plan is ordinary Python code.",
            }
        ),
        encoding="utf-8",
    )
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "workspace_id": workspace_id,
                "plan_id": "phase_plan:calculator",
                "plan": {
                    "subtasks": [
                        {
                            "id": "core",
                            "title": "Calculator core",
                            "goal": "Implement deterministic arithmetic.",
                            "files_to_create": [
                                "src/calculator/core.py",
                                "tests/test_calculator_core.py",
                            ],
                            "proof": {
                                "execution": {
                                    "kind": "pytest",
                                    "command": [
                                        "python",
                                        "-m",
                                        "pytest",
                                        "tests/test_calculator_core.py",
                                        "-q",
                                    ],
                                },
                                "oracle": {
                                    "oracle_type": "unit_assertions",
                                    "required_properties": [
                                        "distinct_inputs_distinct_outputs",
                                        "no_test_tampering",
                                    ],
                                },
                                "scope": {
                                    "files_under_test": ["src/calculator/core.py"],
                                    "changed_files_expected": [
                                        "src/calculator/core.py",
                                        "tests/test_calculator_core.py",
                                    ],
                                    "pytest_targets": ["tests/test_calculator_core.py"],
                                },
                                "anti_gaming": {"requires_real_runtime": True},
                            },
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    bundle = ContractCompiler.from_run(
        repo_root=repo,
        drive_root=drive,
        workspace_id=workspace_id,
        run_id="run-1",
    )
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=workspace_id,
    )

    assert bundle.risk.llm_or_prompt_logic is False
    assert "missing_behavioral_oracle" not in _codes(
        ContractValidator.validate(bundle, context=context, exit_phase="plan")
    )


def test_proof_blocks_shell_collect_only_and_human_claims_without_oracle():
    shell_proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="pytest",
            command=("bash", "-lc", "pytest --collect-only || true"),
            shell=True,
        ),
        oracle=ProofOracleSpec(oracle_type="unit_assertions"),
        scope=ProofScopeSpec(pytest_targets=("tests/test_app.py",)),
        human_claims=("looks fine",),
    )

    codes = _codes(validate_proof_spec(shell_proof))

    assert "shell_proof_forbidden" in codes
    assert "shell_process_control_forbidden" in codes
    assert "collect_only_proof" in codes
    assert "human_claims_without_machine_oracle" in codes


def test_proof_blocks_shell_chain_tokens_in_argv():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="build",
            command=("cd", "frontend", "&&", "npm", "run", "build"),
            shell=False,
        ),
        oracle=ProofOracleSpec(
            oracle_type="build",
            required_properties=("build_succeeds",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("frontend/package.json",),
            changed_files_expected=("frontend/package.json",),
        ),
    )

    codes = _codes(validate_proof_spec(proof))

    assert "shell_operator_in_argv" in codes


def test_proof_blocks_umbrella_tool_pseudo_command():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="verification_step",
            command=("run_workspace_verify",),
            shell=False,
        ),
        oracle=ProofOracleSpec(
            oracle_type="behavioral_http",
            required_properties=("runtime_started",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("tests/test_e2e.py",),
            changed_files_expected=("tests/test_e2e.py",),
        ),
    )

    codes = _codes(validate_proof_spec(proof))

    assert "unavailable_proof_target" in codes


def test_proof_blocks_python_c_subprocess_shell_bypass():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="build",
            command=(
                "python",
                "-c",
                "import subprocess; "
                "subprocess.check_call(['npm', 'install', '--silent'], "
                "cwd='frontend', shell=True); "
                "subprocess.run(['npm', 'run', 'build'], cwd='frontend', check=False)",
            ),
            shell=False,
        ),
        oracle=ProofOracleSpec(
            oracle_type="build",
            required_properties=("build_succeeds",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("frontend/package.json",),
            changed_files_expected=("frontend/package.json",),
        ),
    )

    codes = _codes(validate_proof_spec(proof))

    assert "python_subprocess_shell_forbidden" in codes
    assert "python_subprocess_check_false" in codes


def test_proof_blocks_invalid_python_c_script():
    proof = ProofSpec(
        execution=ProofExecutionSpec(
            kind="http_boot",
            command=(
                "python",
                "-c",
                "import subprocess, time; "
                "server = subprocess.Popen(['python', '-m', 'uvicorn', 'src.api.main:app']); "
                "time.sleep(3); try: print('ok') except Exception: server.terminate()",
            ),
            shell=False,
        ),
        oracle=ProofOracleSpec(
            oracle_type="behavioral_http",
            required_properties=("runtime_started",),
        ),
        scope=ProofScopeSpec(
            files_under_test=("src/api/main.py",),
            changed_files_expected=("src/api/main.py",),
        ),
    )

    codes = _codes(validate_proof_spec(proof))

    assert "invalid_python_c_proof" in codes


def test_fake_evidence_ref_fails(tmp_path: Path):
    repo, workspace, workspace_id = _workspace(tmp_path)
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=workspace_id,
    )
    proof = ProofSpec(
        execution=ProofExecutionSpec(kind="build", command=("python", "-m", "compileall", "src")),
        oracle=ProofOracleSpec(oracle_type="build", required_properties=("build_succeeds",)),
        scope=ProofScopeSpec(files_under_test=("src/app.py",), changed_files_expected=("src/app.py",)),
        evidence_refs=(
            EvidenceRef(
                ref_type="ledger_event",
                ref_id="does-not-exist",
                produced_by="verifier",
            ),
        ),
    )

    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id=workspace_id, plan=None),
        context=context,
    )
    assert issues == []
    codes = _codes(validate_proof_spec(proof, resolver=EvidenceResolver(context)))
    assert "fake_evidence_ref" in codes


def test_stale_completion_evidence_fails(tmp_path: Path):
    repo, workspace, workspace_id = _workspace(tmp_path)
    proof_event = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id=workspace_id,
        actor="verifier",
        phase="execute",
        tool="pytest",
        result={"passed": True},
    )
    patch_event = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id=workspace_id,
        actor="supervisor",
        phase="execute",
        tool="apply_workspace_patch",
        result={"changed": ["src/app.py"]},
        touched_files=("src/app.py",),
    )
    completion = CompletionContract.from_mapping(
        {
            "subtask_id": "s1",
            "completed_claims": [
                {
                    "claim_id": "c1",
                    "text": "implemented behavior",
                    "proof_refs": [
                        {
                            "ref_type": "ledger_event",
                            "ref_id": proof_event.event_id,
                            "hash": proof_event.event_hash,
                            "produced_by": "verifier",
                            "created_after_event": patch_event.event_id,
                        }
                    ],
                }
            ],
            "changed_files": ["src/app.py"],
        }
    )
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=workspace_id,
        changed_files=("src/app.py",),
    )

    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id=workspace_id, completions=(completion,)),
        context=context,
    )

    assert "stale_proof_ref" in _codes(issues)


def test_completion_rejects_artifact_refs_as_proof(tmp_path: Path):
    repo, workspace, workspace_id = _workspace(tmp_path)
    completion = CompletionContract.from_mapping(
        {
            "subtask_id": "project-scaffold",
            "status": "done",
            "completed_claims": [
                {
                    "claim_id": "project-scaffold.claim.1",
                    "text": "backend dependencies configured",
                    "files": ["backend/requirements.txt"],
                    "proof_refs": [
                        {
                            "ref_type": "artifact",
                            "ref_id": "backend/requirements.txt",
                            "produced_by": "agent",
                            "phase": "execute",
                            "subtask_id": "project-scaffold",
                        }
                    ],
                }
            ],
            "changed_files": ["backend/requirements.txt"],
        }
    )
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=workspace_id,
        changed_files=("backend/requirements.txt",),
    )

    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id=workspace_id, completions=(completion,)),
        context=context,
    )

    assert "non_ledger_evidence_ref" in _codes(issues)


def test_completion_rejects_depth_limited_evidence_ref_shape(tmp_path: Path):
    repo, workspace, workspace_id = _workspace(tmp_path)
    placeholder = {"_depth_limit": True}
    completion = CompletionContract.from_mapping(
        {
            "subtask_id": "project-scaffold",
            "status": "done",
            "completed_claims": [
                {
                    "claim_id": "project-scaffold.claim.1",
                    "text": "backend dependencies configured",
                    "files": ["backend/requirements.txt"],
                    "proof_refs": [
                        {
                            "ref_type": placeholder,
                            "ref_id": placeholder,
                            "produced_by": placeholder,
                            "phase": placeholder,
                            "subtask_id": placeholder,
                        }
                    ],
                }
            ],
            "changed_files": ["backend/requirements.txt"],
        }
    )
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=workspace_id,
        changed_files=("backend/requirements.txt",),
    )

    issues = ContractValidator.validate(
        ContractBundle(run_id="r", workspace_id=workspace_id, completions=(completion,)),
        context=context,
    )
    codes = _codes(issues)

    assert "invalid_evidence_ref" in codes
    assert "fake_evidence_ref" in codes


def test_verification_report_hash_and_workspace_mismatch_fail(tmp_path: Path):
    repo, workspace, workspace_id = _workspace(tmp_path)
    report_hash = hash_value({"passed": True, "steps": []})
    original_workspace_hash = workspace_hash(workspace)
    original_diff_hash = diff_hash(workspace, ("src/app.py",))
    event = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id=workspace_id,
        actor="verifier",
        phase="verify",
        tool="run_workspace_verify",
        result={
            "report_hash": report_hash,
            "passed": True,
            "workspace_hash": original_workspace_hash,
            "diff_hash": original_diff_hash,
        },
    )
    (workspace / "src" / "app.py").write_text("def add(a, b):\n    return 42\n")
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=workspace_id,
        changed_files=("src/app.py",),
    )

    bad_hash = VerificationReportRef(
        report_id=event.event_id,
        report_hash="wrong",
        workspace_hash=original_workspace_hash,
        diff_hash=original_diff_hash,
        produced_after_event_id="",
        verifier_id="run_workspace_verify",
        passed=True,
        ledger_hash=event.event_hash,
    )
    stale_workspace = VerificationReportRef(
        report_id=event.event_id,
        report_hash=report_hash,
        workspace_hash=original_workspace_hash,
        diff_hash=original_diff_hash,
        produced_after_event_id="",
        verifier_id="run_workspace_verify",
        passed=True,
        ledger_hash=event.event_hash,
    )

    assert "verification_report_hash_mismatch" in _codes(
        validate_verification_report_ref(bad_hash, context=context)
    )
    assert "proof_stale_rerun_required" in _codes(
        validate_verification_report_ref(stale_workspace, context=context)
    )


def test_review_decision_uses_typed_issue_not_notes():
    review = ReviewContract.from_mapping(
        {
            "verdict": "revise",
            "notes": "готово 已完成 todo просто временно",
            "issues": [
                {
                    "code": "missing_proof",
                    "severity": "blocking",
                    "phase": "plan",
                    "subtask_id": "s1",
                    "message": "typed issue drives the decision",
                }
            ],
        }
    )

    assert validate_review_contract(review) == []
    issues = ContractValidator.validate(ContractBundle(run_id="r", workspace_id="ws", reviews=(review,)))
    assert "missing_proof" in _codes(issues)


def test_ast_analyzers_catch_python_and_jsts_tampering():
    py_issues = analyze_python_test_source(
        "import pytest\n"
        "from unittest.mock import MagicMock\n"
        "def test_skip():\n"
        "    pytest.skip('later')\n"
        "def test_true():\n"
        "    assert True\n"
        "def test_mock():\n"
        "    MagicMock()\n",
        path="tests/test_bad.py",
    )
    js_issues = analyze_jsts_test_source(
        "test.skip('x', () => {})\n"
        "expect(true).toBe(true)\n"
        "vi.mock('../src/api')\n",
        path="src/api.test.ts",
    )

    assert {"pytest_skip_or_xfail", "assert_true", "target_behavior_mock"} <= _codes(py_issues)
    assert {"js_test_skip", "js_expect_true", "js_target_mock"} <= _codes(js_issues)


def test_workspace_hash_ignores_dot_memory(tmp_path: Path) -> None:
    _repo, workspace, _workspace_id = _workspace(tmp_path)
    before = workspace_hash(workspace)
    memory_dir = workspace / ".memory" / "drive" / "state"
    memory_dir.mkdir(parents=True)
    (memory_dir / "note.json").write_text('{"x": 1}', encoding="utf-8")
    assert workspace_hash(workspace) == before
    (workspace / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b + 1\n",
        encoding="utf-8",
    )
    assert workspace_hash(workspace) != before
