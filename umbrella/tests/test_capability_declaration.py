"""Capability declaration module tests."""

import json
from pathlib import Path

from umbrella.contracts.capability_declaration import (
    CapabilityDeclaration,
    capability_text_contradiction_errors,
    declaration_effective_capabilities,
    validate_declaration_payload,
)


def test_validate_declaration_accepts_custom_capability_slug() -> None:
    from umbrella.contracts.capability_declaration import validate_declaration_payload

    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "custom_ml_gpu": {"available": True, "source": "declared"},
            },
            "notes": "Custom capability slug from discovery, no built-in registry.",
        }
    )
    assert not errors


def test_validate_declaration_rejects_unprobed_harness_runtime_capability() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "desktop_gui_runtime": {
                    "available": True,
                    "source": "declared",
                    "reason": "Tkinter imports successfully.",
                },
            },
            "probe_audit": {"python": True, "subprocess": True},
            "notes": "Desktop GUI runtime requires a real display automation probe.",
        }
    )

    assert any("desktop_gui_runtime" in item and "successful probe" in item for item in errors)


def test_validate_declaration_rejects_import_only_desktop_runtime_probe() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "desktop_gui_runtime": {
                    "available": True,
                    "source": "probe",
                    "reason": "",
                    "probe": {
                        "kind": "command",
                        "command": ["python", "-c", "import tkinter; print('tk ok')"],
                    },
                },
            },
            "probe_audit": {"desktop_gui_runtime": True},
            "notes": "Desktop GUI runtime requires a real window/root probe.",
        }
    )

    assert any("desktop_gui_runtime" in item and "Import-only" in item for item in errors)


def test_validate_declaration_rejects_available_runtime_with_stale_negative_text() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "desktop_gui_runtime": {
                    "available": True,
                    "source": "probe",
                    "reason": "",
                    "probe": {
                        "kind": "command",
                        "intent": "real_gui_root_lifecycle",
                        "command": [
                            "python",
                            "-c",
                            (
                                "import tkinter as tk; "
                                "root = tk.Tk(); root.update(); root.destroy()"
                            ),
                        ],
                    },
                },
            },
            "probe_audit": {"desktop_gui_runtime": True},
            "constraints": ["Desktop GUI runtime not available; use headless tests."],
            "notes": "Desktop GUI runtime capability is currently unavailable.",
        }
    )

    assert any("marked available" in item and "unavailable" in item for item in errors)


def test_validate_declaration_rejects_available_runtime_with_stale_probe_needed_text() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "desktop_gui_runtime": {
                    "available": True,
                    "source": "probe",
                    "reason": "",
                    "probe": {
                        "kind": "command",
                        "intent": "real_gui_root_lifecycle",
                        "command": [
                            "python",
                            "-c",
                            (
                                "import tkinter as tk; "
                                "root = tk.Tk(); root.update(); root.destroy()"
                            ),
                        ],
                    },
                },
            },
            "probe_audit": {"desktop_gui_runtime": True},
            "notes": (
                "Desktop GUI runtime needs probe verification before declaring "
                "available."
            ),
        }
    )

    assert any("marked available" in item and "unverified" in item for item in errors)


def test_validate_declaration_rejects_available_runtime_with_probe_confirmation_text() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "desktop_gui_runtime": {
                    "available": True,
                    "source": "probe",
                    "reason": "",
                    "probe": {
                        "kind": "command",
                        "intent": "real_gui_root_lifecycle",
                        "command": [
                            "python",
                            "-c",
                            (
                                "import tkinter as tk; "
                                "root = tk.Tk(); root.update(); root.destroy()"
                            ),
                        ],
                    },
                },
            },
            "probe_audit": {"desktop_gui_runtime": True},
            "notes": (
                "Desktop GUI runtime availability requires probe confirmation, "
                "but headless capability is assured."
            ),
        }
    )

    assert any("marked available" in item and "unverified" in item for item in errors)


def test_capability_text_contradiction_catches_research_summary_probe_todo() -> None:
    errors = capability_text_contradiction_errors(
        {
            "desktop_gui_runtime": {
                "available": True,
                "source": "probe",
                "probe": {
                    "kind": "command",
                    "intent": "real_gui_root_lifecycle",
                    "command": [
                        "python",
                        "-c",
                        "import tkinter as tk; root = tk.Tk(); root.destroy()",
                    ],
                },
            }
        },
        [
            (
                "Implementation strategy: Probe desktop GUI runtime capability "
                "before creating the Tkinter calculator."
            )
        ],
    )

    assert any("marked available" in item and "unverified" in item for item in errors)


def test_validate_declaration_rejects_mentioned_runtime_without_entry() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "python": {"available": True, "source": "probe"},
                "subprocess": {"available": True, "source": "probe"},
            },
            "probe_audit": {"python": True, "subprocess": True},
            "notes": (
                "Simple Tkinter app needs desktop GUI runtime for real-window "
                "smoke testing."
            ),
        }
    )

    assert any(
        "desktop_gui_runtime" in item and "missing from capabilities" in item
        for item in errors
    )


def test_validate_declaration_rejects_unverified_unavailable_runtime() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "desktop_gui_runtime": {
                    "available": False,
                    "source": "standard_library",
                    "reason": (
                        "Tkinter should be available, but the display path "
                        "needs verification."
                    ),
                },
                "desktop_gui_headless": {
                    "available": True,
                    "source": "declared",
                    "reason": "Tkinter imports can be tested without a display.",
                },
            },
            "probe_audit": {"python": True, "subprocess": True},
            "notes": (
                "Desktop GUI runtime needs verification before a real-window "
                "smoke proof can be selected."
            ),
        }
    )

    assert any(
        "desktop_gui_runtime" in item and "needs verification" in item
        for item in errors
    )


def test_validate_declaration_rejects_native_gui_without_usable_harness_capability() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "python": {
                    "available": True,
                    "source": "probe",
                    "reason": "Python runtime is available.",
                },
                "desktop_gui_headless": {
                    "available": False,
                    "source": "declared",
                    "reason": (
                        "Not detected in current workspace, but not required "
                        "for this simple calculator task."
                    ),
                },
            },
            "probe_audit": {"python": True, "subprocess": True},
            "notes": (
                "Research recommends Python with Tkinter for a simple GUI "
                "application because it is built in and has zero dependencies."
            ),
        }
    )

    assert any("GUI harness capability" in item for item in errors)


def test_validate_declaration_rejects_preference_as_unavailable_capability() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "python": {"available": True, "source": "probe"},
                "desktop_gui_headless": {
                    "available": False,
                    "source": "declared",
                    "reason": (
                        "Calculator requires actual desktop GUI with buttons; "
                        "headless mode is not suitable for this app."
                    ),
                },
                "desktop_gui_runtime": {
                    "available": True,
                    "source": "probe",
                    "probe": {
                        "kind": "command",
                        "command": [
                            "python",
                            "-c",
                            "import tkinter; root = tkinter.Tk(); root.update(); root.destroy()",
                        ],
                    },
                },
            },
            "probe_audit": {
                "python": True,
                "subprocess": True,
                "desktop_gui_runtime": True,
            },
            "notes": (
                "Research recommends Python with Tkinter for a native desktop "
                "GUI calculator."
            ),
        }
    )

    assert any("planning preference" in item for item in errors)


def test_validate_declaration_accepts_native_gui_with_headless_capability() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "python": {"available": True, "source": "probe"},
                "desktop_gui_headless": {
                    "available": True,
                    "source": "declared",
                    "reason": "Tkinter imports and controller tests can run headlessly.",
                },
            },
            "probe_audit": {"python": True, "subprocess": True},
            "notes": (
                "Research recommends Python with Tkinter for a native desktop "
                "GUI calculator and plans headless adapter/controller proof."
            ),
        }
    )

    assert not errors


def test_effective_capabilities_fail_closed_for_unprobed_harness_runtime() -> None:
    decl = CapabilityDeclaration.from_mapping(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "desktop_gui_runtime": {
                    "available": True,
                    "source": "declared",
                    "reason": "Library import passed, but runtime was not probed.",
                },
                "python": {"available": True, "source": "probe"},
            },
            "probe_audit": {"python": True, "subprocess": True},
            "notes": "Desktop GUI runtime was not actually probed under its slug.",
        }
    )

    effective = declaration_effective_capabilities(decl)

    assert effective["desktop_gui_runtime"] is False
    assert effective["python"] is True


def test_validate_declaration_requires_submitted_notes() -> None:
    errors = validate_declaration_payload(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {"python": {"available": True}},
            "notes": "short",
        }
    )
    assert any("notes" in item for item in errors)


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_validate_phase_plan_contract_reads_submitted_declaration(tmp_path: Path) -> None:
    from ouroboros.tools.registry import ToolContext
    from umbrella.deep_agent_tools.phase_contract_handlers import _validate_phase_plan_contract

    repo = tmp_path
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    (workspace / "src" / "calc").mkdir(parents=True)
    (workspace / "src" / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    (workspace / "tests" / "test_calc.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "capabilities": {
                    "python": {"available": True, "source": "probe"},
                },
                "notes": "Python runtime is available for calculator workspace planning.",
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=repo, host_repo_root=repo, drive_root=drive)
    ctx.task_id = "run-1:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": workspace_id}
    plan = {
        "subtasks": [
            {
                "id": "scaffold",
                "title": "Package scaffold",
                "goal": "Create the calculator package root.",
                "files_to_create": ["src/calc/__init__.py", "tests/test_calc.py"],
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_calc.py",
                            "-q",
                        ],
                        "shell": False,
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": [
                            "distinct_inputs_distinct_outputs",
                            "no_test_tampering",
                        ],
                        "negative_cases_required": True,
                    },
                    "scope": {
                        "files_under_test": ["src/calc/__init__.py"],
                        "changed_files_expected": [
                            "src/calc/__init__.py",
                            "tests/test_calc.py",
                        ],
                        "pytest_targets": ["tests/test_calc.py"],
                    },
                    "anti_gaming": {"requires_real_runtime": True},
                    "generated_test_contract": {
                        "interface_model": {
                            "api": "calc scaffold",
                            "valid_values": ["package import smoke"],
                        },
                        "oracle_claims": [
                            {
                                "claim_id": "scaffold_test_ok",
                                "source": "task_requirement",
                                "subject": "scaffold",
                                "input_values": ["package import smoke"],
                                "accepted": True,
                                "expected_behavior": "pytest smoke passes",
                                "test_refs": ["tests/test_calc.py"],
                            }
                        ],
                    },
                    "required_capabilities": ["python"],
                },
            }
        ]
    }
    issues = _validate_phase_plan_contract(ctx, plan)
    assert "missing_capability_declaration" not in _codes(issues)


def test_validate_phase_plan_rejects_stale_available_capability_note(
    tmp_path: Path,
) -> None:
    from ouroboros.tools.registry import ToolContext
    from umbrella.deep_agent_tools.phase_contract_handlers import _validate_phase_plan_contract

    repo = tmp_path
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "capabilities": {
                    "python": {"available": True, "source": "probe"},
                    "desktop_gui_runtime": {
                        "available": True,
                        "source": "probe",
                        "probe": {
                            "kind": "command",
                            "intent": "real_gui_root_lifecycle",
                            "command": [
                                "python",
                                "-c",
                                "import tkinter as tk; root=tk.Tk(); root.destroy()",
                            ],
                            "expect_exit": 0,
                        },
                    },
                },
                "probe_audit": {"desktop_gui_runtime": True},
                "notes": (
                    "Python and desktop_gui_runtime are available for this "
                    "workspace after successful runtime probes."
                ),
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=repo, host_repo_root=repo, drive_root=drive)
    ctx.task_id = "run-1:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": workspace_id}
    plan = {
        "notes": (
            "Use headless proof strategy since desktop_gui_runtime capability "
            "is not verified as available."
        ),
        "subtasks": [
            {
                "id": "config",
                "title": "Create workspace config",
                "goal": "Create the calculator workspace config.",
                "files_to_create": ["workspace.toml"],
                "proof": {
                    "execution": {
                        "kind": "command",
                        "command": ["python", "-c", "print('ok')"],
                        "shell": False,
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["build_succeeds"],
                    },
                    "scope": {
                        "files_under_test": ["workspace.toml"],
                        "changed_files_expected": ["workspace.toml"],
                    },
                    "anti_gaming": {"requires_real_runtime": False},
                    "required_capabilities": ["python"],
                },
            }
        ],
    }

    issues = _validate_phase_plan_contract(ctx, plan)

    assert any(
        issue.code == "capability_probe_failed"
        and "desktop_gui_runtime" in issue.message
        and "phase plan text" in issue.message
        for issue in issues
    )


def test_validate_phase_plan_rejects_stale_available_capability_tool_notes(
    tmp_path: Path,
) -> None:
    from ouroboros.tools.registry import ToolContext
    from umbrella.deep_agent_tools.phase_contract_handlers import _validate_phase_plan_contract

    repo = tmp_path
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "capabilities": {
                    "python": {"available": True, "source": "probe"},
                    "desktop_gui_runtime": {
                        "available": True,
                        "source": "probe",
                        "probe": {
                            "kind": "command",
                            "intent": "real_gui_root_lifecycle",
                            "command": [
                                "python",
                                "-c",
                                "import tkinter as tk; root=tk.Tk(); root.destroy()",
                            ],
                            "expect_exit": 0,
                        },
                    },
                },
                "probe_audit": {"desktop_gui_runtime": True},
                "notes": (
                    "Python and desktop_gui_runtime are available for this "
                    "workspace after successful runtime probes."
                ),
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=repo, host_repo_root=repo, drive_root=drive)
    ctx.task_id = "run-1:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": workspace_id}
    plan = {
        "subtasks": [
            {
                "id": "config",
                "title": "Create workspace config",
                "goal": "Create the calculator workspace config.",
                "files_to_create": ["workspace.toml"],
                "proof": {
                    "execution": {
                        "kind": "command",
                        "command": ["python", "-c", "print('ok')"],
                        "shell": False,
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["build_succeeds"],
                    },
                    "scope": {
                        "files_under_test": ["workspace.toml"],
                        "changed_files_expected": ["workspace.toml"],
                    },
                    "anti_gaming": {"requires_real_runtime": False},
                    "required_capabilities": ["python"],
                },
            }
        ],
    }

    issues = _validate_phase_plan_contract(
        ctx,
        plan,
        notes="desktop_gui_runtime remains not verified, so use headless only.",
    )

    assert "capability_probe_failed" in _codes(issues)


def test_submit_phase_plan_reads_submitted_declaration_not_state_nested_draft(
    tmp_path: Path,
) -> None:
    from ouroboros.tools.registry import ToolContext
    from umbrella.deep_agent_tools.phase_control_actions import _submit_phase_plan

    repo = tmp_path
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    (workspace / "src" / "calc").mkdir(parents=True)
    (workspace / "src" / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    (workspace / "tests" / "test_calc.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    submitted = {
        "schema_version": "1",
        "status": "submitted",
        "capabilities": {"python": {"available": True, "source": "probe"}},
        "notes": "Python runtime is available for calculator workspace planning.",
    }
    (state / "capability_declaration.json").write_text(
        json.dumps(submitted),
        encoding="utf-8",
    )
    nested = state / "state"
    nested.mkdir(parents=True)
    (nested / "capability_declaration.json").write_text(
        json.dumps({**submitted, "status": "draft", "actor": "harness"}),
        encoding="utf-8",
    )
    plan = {
        "subtasks": [
            {
                "id": "scaffold",
                "title": "Package scaffold",
                "goal": "Create the calculator package root.",
                "files_to_create": ["src/calc/__init__.py", "tests/test_calc.py"],
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_calc.py",
                            "-q",
                        ],
                        "shell": False,
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": [
                            "distinct_inputs_distinct_outputs",
                            "no_test_tampering",
                        ],
                        "negative_cases_required": True,
                    },
                    "scope": {
                        "files_under_test": ["src/calc/__init__.py"],
                        "changed_files_expected": [
                            "src/calc/__init__.py",
                            "tests/test_calc.py",
                        ],
                        "pytest_targets": ["tests/test_calc.py"],
                    },
                    "anti_gaming": {"requires_real_runtime": True},
                    "generated_test_contract": {
                        "interface_model": {
                            "api": "calc scaffold",
                            "valid_values": ["package import smoke"],
                        },
                        "oracle_claims": [
                            {
                                "claim_id": "scaffold_test_ok",
                                "source": "task_requirement",
                                "subject": "scaffold",
                                "input_values": ["package import smoke"],
                                "accepted": True,
                                "expected_behavior": "pytest smoke passes",
                                "test_refs": ["tests/test_calc.py"],
                            }
                        ],
                    },
                    "required_capabilities": ["python"],
                },
            }
        ]
    }
    plan_id = "phase_plan:scaffold"
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps({"plan_id": plan_id, "plan": plan, "notes": "test"}),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=repo, host_repo_root=repo, drive_root=drive)
    ctx.task_id = "run-1:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": workspace_id}
    result = _submit_phase_plan(ctx, plan_id=plan_id, notes="handoff")
    assert result.startswith("OK:")
    assert "missing_capability_declaration" not in result


def test_effective_capabilities_merge_declaration_over_probe() -> None:
    decl = CapabilityDeclaration.from_mapping(
        {
            "schema_version": "1",
            "status": "submitted",
            "capabilities": {
                "network": {"available": False, "source": "declared", "reason": "offline sandbox"},
            },
            "notes": "Sandbox has no outbound network during this run.",
        }
    )
    effective = declaration_effective_capabilities(decl, probed={"network": True, "python": True})
    assert effective == {"network": False}
