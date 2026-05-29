from umbrella.contracts.recovery_policy import derive_recovery_options


def test_headless_real_root_recovery_uses_scope_pytest_targets() -> None:
    [option] = derive_recovery_options(
        {
            "code": "headless_proof_uses_real_gui_root",
            "target_subtask_id": "gui",
            "contract_path": "proof.pytest_targets[0]",
            "evidence_refs": ["ledger_event:proof-1"],
        }
    )

    [change] = option.required_plan_changes
    assert change["path"] == "proof.scope.pytest_targets"
    assert change["source"] == "RecoveryPolicy.headless_proof_uses_real_gui_root"


def test_recovery_policy_does_not_trust_issue_contract_path() -> None:
    [option] = derive_recovery_options(
        {
            "code": "headless_proof_uses_real_gui_root",
            "target_subtask_id": "gui",
            "contract_path": "proof.not_real",
        }
    )

    [change] = option.required_plan_changes
    assert change["path"] == "proof.scope.pytest_targets"


def test_recovery_policy_rejects_unknown_issue_without_plan_delta() -> None:
    assert derive_recovery_options(
        {
            "code": "unknown_infra_issue",
            "target_subtask_id": "gui",
            "contract_path": "proof.pytest_targets",
        }
    ) == ()


def test_recovery_policy_runtime_upgrade_option_requires_capability() -> None:
    options = derive_recovery_options(
        {
            "code": "headless_proof_uses_real_gui_root",
            "target_subtask_id": "gui",
        },
        runtime_capability_available=True,
    )

    upgrade = options[1]
    assert [delta.path for delta in upgrade.required_deltas] == [
        "proof.harness_profile",
        "proof.required_capabilities",
    ]


def test_recovery_policy_package_import_env_mismatch_produces_proof_contract_repair() -> None:
    options = derive_recovery_options(
        {
            "code": "package_import_env_mismatch",
            "target_subtask_id": "calculator-core",
            "evidence_refs": ["ledger_event:proof-1"],
        }
    )

    proof_repair = options[0]
    assert proof_repair.code == "proof_contract_repair"
    assert proof_repair.required_deltas[0].path == "proof.execution.env"
    assert proof_repair.required_deltas[0].value == {"PYTHONPATH": "src"}
    assert proof_repair.required_plan_changes[0]["source"] == (
        "RecoveryPolicy.package_import_env_mismatch"
    )


def test_recovery_policy_setup_harness_mismatch_updates_project_setup_proof() -> None:
    options = derive_recovery_options(
        {
            "code": "setup_harness_mismatch",
            "target_subtask_id": "project-setup",
        }
    )

    assert options[2].code == "plan_contract_revision"
    assert options[2].required_plan_changes[0]["path"] == "proof.execution"


def test_package_import_repair_forbids_tests_by_default() -> None:
    options = derive_recovery_options(
        {
            "code": "package_import_env_mismatch",
            "target_subtask_id": "calculator-core",
        }
    )

    packaging = options[1]
    change = packaging.required_plan_changes[0]
    assert packaging.code == "packaging_import_repair"
    assert change["allowed_files"] == [
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "workspace.toml",
    ]
    assert change["forbidden_files"] == ["src/", "tests/"]
