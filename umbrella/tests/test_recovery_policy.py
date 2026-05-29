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
