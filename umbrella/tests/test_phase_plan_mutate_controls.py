import json
from types import SimpleNamespace

from umbrella.deep_agent_tools.phase_control_actions import (
    _apply_plan_revision_patch,
    _apply_phase_plan_subtask_patch,
    _legacy_phase_subtask_materialization_issue,
    _merge_phase_plan_string_list,
    _mutate_phase_plan,
    _phase_plan_string_items,
    _request_scope_change,
)


def test_phase_plan_string_items_flattens_nested_lists() -> None:
    assert _phase_plan_string_items(["a", ["b", "c"]]) == ["a", "b", "c"]


def test_merge_phase_plan_string_list_dedupes() -> None:
    merged = _merge_phase_plan_string_list(["src/a.py"], ["src/b.py", "src/a.py"])
    assert merged == ["src/a.py", "src/b.py"]


def test_apply_phase_plan_subtask_patch_replace_files_to_create() -> None:
    plan = {
        "nodes": [
            {
                "id": "execute",
                "subtasks": [
                    {
                        "id": "scaffold",
                        "files_to_create": ["backend/src/app.py"],
                        "files_to_change": [],
                        "files_affected": [],
                    }
                ],
            }
        ]
    }
    ctx = SimpleNamespace()
    applied, issue = _apply_phase_plan_subtask_patch(
        ctx,
        plan,
        [
            {
                "id": "scaffold",
                "replace_files_to_create": [
                    "src/civilization/backend/app.py",
                    "tests/test_app.py",
                ],
            }
        ],
    )
    assert issue is None
    assert applied == ["subtasks.scaffold"]
    subtask = plan["nodes"][0]["subtasks"][0]
    assert subtask["files_to_create"] == [
        "src/civilization/backend/app.py",
        "tests/test_app.py",
    ]


def test_apply_phase_plan_subtask_patch_direct_files_to_create_replaces() -> None:
    plan = {
        "nodes": [
            {
                "id": "execute",
                "subtasks": [
                    {
                        "id": "launcher",
                        "files_to_create": ["main.py", "README.md"],
                        "files_to_change": [],
                        "files_affected": [],
                    }
                ],
            }
        ]
    }
    applied, issue = _apply_phase_plan_subtask_patch(
        SimpleNamespace(),
        plan,
        [
            {
                "id": "launcher",
                "files_to_create": ["src/calculator/__main__.py", "README.md"],
            }
        ],
    )
    assert issue is None
    assert applied == ["subtasks.launcher"]
    assert plan["nodes"][0]["subtasks"][0]["files_to_create"] == [
        "src/calculator/__main__.py",
        "README.md",
    ]


def test_apply_phase_plan_subtask_patch_merges_partial_proof() -> None:
    plan = {
        "nodes": [
            {
                "id": "execute",
                "subtasks": [
                    {
                        "id": "gui-core",
                        "files_to_create": [
                            "src/calculator/gui.py",
                            "tests/test_gui_core.py",
                        ],
                        "proof": {
                            "execution": {
                                "kind": "pytest",
                                "command": [
                                    "python",
                                    "-m",
                                    "pytest",
                                    "tests/test_gui_core.py::test_old",
                                    "-q",
                                ],
                            },
                            "oracle": {
                                "oracle_type": "unit_assertions",
                                "required_properties": [
                                    "distinct_inputs_distinct_outputs",
                                    "invalid_input_rejected",
                                    "no_test_tampering",
                                ],
                            },
                            "scope": {
                                "files_under_test": ["src/calculator/gui.py"],
                                "changed_files_expected": [
                                    "src/calculator/gui.py",
                                    "tests/test_gui_core.py",
                                ],
                                "pytest_targets": [
                                    "tests/test_gui_core.py::test_old"
                                ],
                            },
                        },
                    }
                ],
            }
        ]
    }

    applied, issue = _apply_phase_plan_subtask_patch(
        SimpleNamespace(),
        plan,
        [
            {
                "id": "gui-core",
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_gui_core.py",
                            "-q",
                        ],
                    }
                },
            }
        ],
    )

    assert issue is None
    assert applied == ["subtasks.gui-core"]
    proof = plan["nodes"][0]["subtasks"][0]["proof"]
    assert proof["execution"]["command"] == [
        "python",
        "-m",
        "pytest",
        "tests/test_gui_core.py",
        "-q",
    ]
    assert "no_test_tampering" in proof["oracle"]["required_properties"]
    assert proof["scope"]["changed_files_expected"] == [
        "src/calculator/gui.py",
        "tests/test_gui_core.py",
    ]


def test_apply_phase_plan_subtask_patch_keeps_no_test_tampering_property() -> None:
    plan = {
        "nodes": [
            {
                "id": "execute",
                "subtasks": [
                    {
                        "id": "gui-core",
                        "files_to_create": ["tests/test_gui_core.py"],
                        "proof": {
                            "execution": {
                                "kind": "pytest",
                                "command": ["python", "-m", "pytest"],
                            },
                            "oracle": {
                                "oracle_type": "unit_assertions",
                                "required_properties": [
                                    "no_test_tampering",
                                ],
                            },
                            "scope": {
                                "changed_files_expected": [
                                    "tests/test_gui_core.py"
                                ],
                                "pytest_targets": ["tests/test_gui_core.py"],
                            },
                        },
                    }
                ],
            }
        ]
    }

    applied, issue = _apply_phase_plan_subtask_patch(
        SimpleNamespace(),
        plan,
        [
                {
                    "id": "gui-core",
                    "proof": {
                        "add_required_properties": [
                            "distinct_inputs_distinct_outputs"
                        ]
                    },
                }
            ],
    )

    assert issue is None
    assert applied == ["subtasks.gui-core"]
    required = plan["nodes"][0]["subtasks"][0]["proof"]["oracle"][
        "required_properties"
    ]
    assert required == [
        "no_test_tampering",
        "distinct_inputs_distinct_outputs",
    ]


def test_apply_phase_plan_subtask_patch_remove_files_to_change() -> None:
    plan = {
        "nodes": [
            {
                "id": "execute",
                "subtasks": [
                    {
                        "id": "scaffold",
                        "files_to_create": [],
                        "files_to_change": ["backend/src/app.py", "README.md"],
                        "files_affected": [],
                    }
                ],
            }
        ]
    }
    ctx = SimpleNamespace()
    applied, issue = _apply_phase_plan_subtask_patch(
        ctx,
        plan,
        [{"id": "scaffold", "remove_files_to_change": ["backend/src/app.py"]}],
    )
    assert issue is None
    assert applied
    assert plan["nodes"][0]["subtasks"][0]["files_to_change"] == ["README.md"]


def test_request_scope_change_treats_future_subtask_owned_source_as_advisory(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        """
{
  "plan_id": "plan-1",
  "workspace_id": "demo",
  "run_id": "run-1",
  "version": 1,
  "nodes": [
    {
      "id": "execute",
      "manifest_id": "execute",
      "status": "running",
      "subtasks": [
        {
          "id": "project-setup",
          "status": "pending",
          "files_to_create": ["pyproject.toml"]
        },
        {
          "id": "calculator-gui",
          "status": "pending",
          "files_to_create": ["src/calculator/gui.py"]
        }
      ]
    }
  ]
}
""",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _request_scope_change(
        ctx,
        paths=["src/calculator/gui.py"],
        rationale="Need a stub for import",
    )

    assert result.startswith("Scope change not required")
    assert "src/calculator/gui.py -> calculator-gui" in result
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    active = plan["nodes"][0]["subtasks"][0]
    assert active["files_to_create"] == ["pyproject.toml"]


def test_mutate_phase_plan_rejects_top_level_legacy_plan_revision_metadata(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "workspace_id": "demo",
                "run_id": "run-1",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "gui-controller",
                                "status": "pending",
                                "files_to_create": ["tests/test_gui_state.py"],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        patch={
            "contract_migration_reason": "Watcher proved the generated test contract is internally inconsistent.",
            "contract_migration_files": ["tests/test_gui_state.py"],
            "contract_migration_id": "migration-1",
            "acceptance_criteria": [
                "Revise generated GUI state oracle to match accepted task behavior."
            ],
        },
    )

    assert "legacy plan-revision metadata is not accepted" in result
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    subtask = plan["nodes"][0]["subtasks"][0]
    assert "contract_migration_reason" not in subtask
    assert "acceptance_criteria" not in subtask
    assert plan["version"] == 1


def test_mutate_phase_plan_accepts_target_subtask_id_selector(tmp_path) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "workspace_id": "demo",
                "run_id": "run-1",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {"id": "setup", "status": "done"},
                            {"id": "logic", "status": "pending"},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        target_subtask_id="logic",
        patch={
            "acceptance_criteria": [
                "Replace contradictory generated oracle with task-derived assertions."
            ],
        },
    )

    assert result.startswith("PhasePlan mutated")
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    setup, logic = plan["nodes"][0]["subtasks"]
    assert "acceptance_criteria" not in setup
    assert logic["acceptance_criteria"] == [
        "Replace contradictory generated oracle with task-derived assertions."
    ]


def test_mutate_phase_plan_rejects_metadata_only_plan_revision(tmp_path) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "workspace_id": "demo",
                "run_id": "run-1",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [{"id": "logic", "status": "pending"}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        subtask_id="logic",
        patch={
            "required_deltas": [
                {
                    "op": "remove",
                    "path": "proof.required_properties",
                    "values": ["distinct_inputs_distinct_outputs"],
                }
            ]
        },
    )

    assert "metadata-only revisions are not accepted" in result
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    logic = plan["nodes"][0]["subtasks"][0]
    assert "contract_migration_files" not in logic
    assert plan["version"] == 1


def test_mutate_phase_plan_rejects_same_semantic_plan_revision(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    proof = {
        "execution": {
            "kind": "pytest",
            "command": ["python", "-m", "pytest", "tests/test_logic.py", "-q"],
        },
        "oracle": {
            "required_properties": [
                "distinct_inputs_distinct_outputs",
                "no_test_tampering",
            ],
        },
        "scope": {"pytest_targets": ["tests/test_logic.py"]},
    }
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "workspace_id": "demo",
                "run_id": "run-1",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "logic",
                                "status": "pending",
                                "proof": proof,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        target_subtask_id="logic",
        patch={
            "reason_code": "bad_generated_oracle",
            "proof": proof,
        },
    )

    assert "did not change the semantic proof/test/oracle contract" in result
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    assert plan["version"] == 1
    assert "contract_migration_reason" not in plan["nodes"][0]["subtasks"][0]


def test_mutate_phase_plan_rejects_unsatisfied_required_removal_ticket(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    proof = {
        "execution": {
            "kind": "pytest",
            "command": ["python", "-m", "pytest", "tests/test_logic.py", "-q"],
        },
        "oracle": {
            "required_properties": [
                "distinct_inputs_distinct_outputs",
                "no_test_tampering",
            ],
        },
        "scope": {"pytest_targets": ["tests/test_logic.py"]},
    }
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "workspace_id": "demo",
                "run_id": "run-1",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "logic",
                                "status": "pending",
                                "proof": proof,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        target_subtask_id="logic",
        patch={
            "reason_code": "bad_generated_oracle",
            "required_deltas": [
                {
                    "op": "remove",
                    "path": "proof.required_properties",
                    "values": ["distinct_inputs_distinct_outputs"],
                }
            ],
            "acceptance_criteria": ["Use task-derived examples."],
        },
    )

    assert "incomplete" in result
    assert "distinct_inputs_distinct_outputs" in result
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    assert plan["version"] == 1
    required = plan["nodes"][0]["subtasks"][0]["proof"]["oracle"][
        "required_properties"
    ]
    assert "distinct_inputs_distinct_outputs" in required


def test_mutate_phase_plan_accepts_top_level_subtask_id_alias_with_contract_change(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "workspace_id": "demo",
                "run_id": "run-1",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [{"id": "logic", "status": "pending"}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        subtask_id="logic",
        patch={
            "acceptance_criteria": ["Use task-derived examples."],
        },
    )

    assert result.startswith("PhasePlan mutated")
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    logic = plan["nodes"][0]["subtasks"][0]
    assert logic["acceptance_criteria"] == ["Use task-derived examples."]


def test_mutate_phase_plan_typed_patch_can_remove_oracle_property(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    proof = {
        "execution": {
            "kind": "pytest",
            "command": ["python", "-m", "pytest", "tests/test_logic.py", "-q"],
            "timeout_sec": 60,
            "shell": False,
        },
        "oracle": {
            "oracle_type": "unit_assertions",
            "required_properties": [
                "distinct_inputs_distinct_outputs",
                "invalid_input_rejected",
                "no_test_tampering",
            ],
            "negative_cases_required": True,
        },
        "scope": {
            "files_under_test": ["src/demo/logic.py"],
            "changed_files_expected": ["src/demo/logic.py", "tests/test_logic.py"],
            "pytest_targets": ["tests/test_logic.py"],
        },
        "anti_gaming": {
            "allows_mock": False,
            "allows_snapshot_update": False,
            "allows_test_only_change": False,
            "requires_real_runtime": True,
        },
        "harness_profile": "python_src_layout",
    }
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "workspace_id": "demo",
                "run_id": "run-1",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "logic",
                                "status": "pending",
                                "proof": proof,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        target_subtask_id="logic",
        patch={
            "reason_code": "bad_generated_oracle",
            "required_deltas": [
                {
                    "op": "remove",
                    "path": "proof.required_properties",
                    "values": ["distinct_inputs_distinct_outputs"],
                }
            ],
            "proof": {
                "remove_required_properties": [
                    "distinct_inputs_distinct_outputs"
                ],
                "add_required_properties": [
                    "operation_semantics_match_task_examples"
                ],
            },
        },
    )

    assert result.startswith("PhasePlan mutated")
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    required = plan["nodes"][0]["subtasks"][0]["proof"]["oracle"][
        "required_properties"
    ]
    assert "distinct_inputs_distinct_outputs" not in required
    assert "invalid_input_rejected" in required
    assert "no_test_tampering" in required
    assert "operation_semantics_match_task_examples" in required


def test_apply_plan_revision_patch_removes_invalid_required_property(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "logic",
                                "status": "pending",
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": [
                                            "python",
                                            "-m",
                                            "pytest",
                                            "tests/test_logic.py",
                                            "-q",
                                        ],
                                    },
                                    "oracle": {
                                        "required_properties": [
                                            "distinct_inputs_distinct_outputs",
                                            "no_test_tampering",
                                        ],
                                    },
                                    "scope": {
                                        "files_under_test": ["src/demo/logic.py"],
                                        "changed_files_expected": [
                                            "src/demo/logic.py",
                                            "tests/test_logic.py",
                                        ],
                                        "pytest_targets": ["tests/test_logic.py"],
                                    },
                                },
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _apply_plan_revision_patch(
        ctx,
        target_subtask_id="logic",
        reason_code="bad_generated_oracle",
        deltas=[
            {
                "op": "remove",
                "path": "proof.required_properties",
                "values": ["distinct_inputs_distinct_outputs"],
            }
        ],
        patch={
            "proof": {
                "remove_required_properties": [
                    "distinct_inputs_distinct_outputs"
                ]
            },
        },
        evidence_refs=[{"kind": "proof_result", "id": "proof-1"}],
    )

    assert result.startswith("PhasePlan mutated")
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    logic = plan["nodes"][0]["subtasks"][0]
    assert plan["version"] == 2
    assert logic["proof"]["oracle"]["required_properties"] == ["no_test_tampering"]
    assert "required_deltas" not in logic
    signal = json.loads((state / "phase_control_signal.json").read_text())
    assert signal["kind"] == "apply_plan_revision_patch"


def test_apply_plan_revision_patch_rejects_reason_only_change(tmp_path) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [{"id": "logic", "status": "pending"}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _apply_plan_revision_patch(
        ctx,
        target_subtask_id="logic",
        reason_code="bad_generated_oracle",
        patch={},
        evidence_refs=[{"kind": "proof_result", "id": "proof-1"}],
    )

    assert "reason/evidence-only revisions are not accepted" in result
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    assert plan["version"] == 1


def test_mutate_phase_plan_required_deltas_replaces_oracle_property(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "logic",
                                "status": "pending",
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": [
                                            "python",
                                            "-m",
                                            "pytest",
                                            "tests/test_logic.py",
                                            "-q",
                                        ],
                                    },
                                    "oracle": {
                                        "required_properties": [
                                            "distinct_inputs_distinct_outputs",
                                            "no_test_tampering",
                                        ],
                                    },
                                    "scope": {
                                        "pytest_targets": ["tests/test_logic.py"],
                                    },
                                },
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        target_subtask_id="logic",
        patch={
            "required_deltas": [
                {
                    "op": "remove",
                    "path": "proof.required_properties",
                    "values": ["distinct_inputs_distinct_outputs"],
                }
            ],
            "proof": {
                "oracle": {
                    "required_properties": ["no_test_tampering"],
                },
            },
        },
    )

    assert result.startswith("PhasePlan mutated")
    plan = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    required = plan["nodes"][0]["subtasks"][0]["proof"]["oracle"][
        "required_properties"
    ]
    assert required == ["no_test_tampering"]
    assert "required_deltas" not in plan["nodes"][0]["subtasks"][0]


def test_mutate_phase_plan_rejects_subtask_id_inside_patch(tmp_path) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [{"id": "logic", "status": "pending"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:1",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtask_id": "logic",
            "acceptance_criteria": ["Use task-derived examples."],
        },
    )

    assert "subtask_id is a selector" in result


def test_legacy_completion_checks_declared_files_before_accepting(tmp_path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = SimpleNamespace(
        drive_root=str(drive),
        host_repo_root=str(repo),
        task_id="run-1:execute:123",
    )
    current_phase = {
        "id": "execute",
        "subtasks": [
            {
                "id": "launcher",
                "status": "pending",
                "files_to_create": ["main.py"],
            }
        ],
    }

    issue = _legacy_phase_subtask_materialization_issue(
        ctx,
        current_phase=current_phase,
        subtask_id="launcher",
    )

    assert "subtask_materialization_missing" in issue
    assert "main.py" in issue
