"""run_subtask_proof returns verifier ledger refs for completion contracts."""

from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from umbrella.contracts.models import CompletionContract
from umbrella.contracts.validators import ContractValidator
from umbrella.contracts import ContractBundle, build_workspace_context
from umbrella.deep_agent_tools.phase_control_actions import (
    _mark_subtask_complete,
    _request_watcher_review,
    _run_subtask_proof,
)
from umbrella.deep_agent_tools.phase_control_retry import (
    _phase_subtask_retry_escalation_block,
    _phase_subtask_retry_state,
    _phase_subtask_retry_watcher_review_payload,
    _tool_row_success_status,
)
from umbrella.deep_agent_tools.workspace_ops import replace_workspace_file
from umbrella.deep_agent_tools.workspace_read import read_workspace_file
from umbrella.enforcement.ledger import read_supervisor_ledger_events


def test_run_subtask_proof_records_verifier_ledger(tmp_path: Path) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    pkg = workspace / "src" / "demoapp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "demoapp"\nversion = "0.0.1"\n',
        encoding="utf-8",
    )
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan = {
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "project-setup",
                        "status": "pending",
                        "files_to_create": ["src/demoapp/__init__.py"],
                        "proof": {
                            "execution": {
                                "kind": "bool",
                                "command": [
                                    "python",
                                    "-c",
                                    "import demoapp; print(demoapp.__version__)",
                                ],
                                "timeout_sec": 30,
                                "shell": False,
                            },
                            "oracle": {
                                "oracle_type": "unit_assertions",
                                "required_properties": ["exists"],
                            },
                        },
                    }
                ],
            }
        ]
    }
    (state / "phase_plan.json").write_text(json.dumps(plan), encoding="utf-8")

    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        umbrella_managed=True,
        umbrella_phase_id="execute",
        context_overlays={
            "phase_manifest": {"id": "execute"},
            "phase_node": {"id": "execute", "manifest_id": "execute"},
        },
        current_task_type="phase_run",
    )
    seen: dict[str, object] = {}

    def _fake_command(*_args, **kwargs) -> str:
        seen.update(kwargs)
        return json.dumps(
            {
                "workspace_id": ws,
                "exit_code": 0,
                "command": ["python", "-c", "import demoapp"],
                "output": "0.0.1\n",
                "backend": "oneshot",
            }
        )

    with patch(
        "umbrella.deep_agent_tools.workspace_commands.run_workspace_command",
        side_effect=_fake_command,
    ):
        raw = _run_subtask_proof(ctx, subtask_id="project-setup")
    assert seen.get("subdir") == ""
    assert not str(raw).startswith("ERROR"), raw
    payload = json.loads(raw)
    assert payload["passed"] is True
    assert payload["ledger_event_id"]
    assert payload["verification_report"]["passed"] is True

    ledger_rows = read_supervisor_ledger_events(
        repo_root=repo, workspace_id=ws
    )
    assert any(row.get("tool") == "run_subtask_proof" for row in ledger_rows)

    hint = payload["completion_contract_hint"]
    hint["changed_files"] = ["src/demoapp/__init__.py"]
    completion = CompletionContract.from_mapping(hint)
    context = build_workspace_context(
        repo_root=repo,
        workspace_root=workspace,
        workspace_id=ws,
        changed_files=completion.changed_files,
    )
    issues = ContractValidator.validate(
        ContractBundle(run_id="r1", workspace_id=ws, completions=(completion,)),
        context=context,
    )
    assert issues == [], [issue.message for issue in issues]


def test_run_subtask_proof_blocks_passed_hint_when_declared_file_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    pkg = workspace / "src" / "demoapp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan = {
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "project-setup",
                        "status": "pending",
                        "files_to_create": [
                            "src/demoapp/__init__.py",
                            "tests/test_calculator_logic.py",
                        ],
                        "proof": {
                            "execution": {
                                "kind": "bool",
                                "command": [
                                    "python",
                                    "-c",
                                    "import demoapp; print(demoapp.__version__)",
                                ],
                                "timeout_sec": 30,
                                "shell": False,
                            },
                            "oracle": {
                                "oracle_type": "unit_assertions",
                                "required_properties": ["module_imports"],
                            },
                        },
                    }
                ],
            }
        ]
    }
    (state / "phase_plan.json").write_text(json.dumps(plan), encoding="utf-8")

    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        umbrella_managed=True,
        umbrella_phase_id="execute",
        context_overlays={
            "phase_manifest": {"id": "execute"},
            "phase_node": {"id": "execute", "manifest_id": "execute"},
        },
        current_task_type="phase_run",
    )

    def _fake_command(*_args, **_kwargs) -> str:
        return json.dumps(
            {
                "workspace_id": ws,
                "exit_code": 0,
                "command": ["python", "-c", "import demoapp"],
                "output": "0.0.1\n",
                "backend": "oneshot",
            }
        )

    with patch(
        "umbrella.deep_agent_tools.workspace_commands.run_workspace_command",
        side_effect=_fake_command,
    ):
        raw = _run_subtask_proof(ctx, subtask_id="project-setup")

    assert not str(raw).startswith("ERROR"), raw
    payload = json.loads(raw)
    assert payload["passed"] is False
    assert payload["proof_command_passed"] is True
    assert payload["materialization_passed"] is False
    assert "completion_contract_hint" not in payload
    assert any(
        issue["code"] == "subtask_materialization_missing"
        and "tests/test_calculator_logic.py" in issue["message"]
        for issue in payload["materialization_issues"]
    )
    assert "Do not call mark_subtask_complete yet" in payload["next_step"]


def test_run_subtask_proof_review_phase_allows_micro_review(tmp_path: Path) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    pkg = workspace / "src" / "demoapp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan = {
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "project-setup",
                        "status": "pending",
                        "files_to_create": ["src/demoapp/__init__.py"],
                        "proof": {
                            "execution": {
                                "kind": "bool",
                                "command": [
                                    "python",
                                    "-c",
                                    "import demoapp; print(demoapp.__version__)",
                                ],
                                "timeout_sec": 30,
                                "shell": False,
                            },
                            "oracle": {
                                "oracle_type": "unit_assertions",
                                "required_properties": ["exists"],
                            },
                        },
                    }
                ],
            }
        ]
    }
    (state / "phase_plan.json").write_text(json.dumps(plan), encoding="utf-8")

    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        umbrella_managed=True,
        task_id="phase_web_demo:subtask_review:project-setup",
        context_overlays={
            "phase_manifest": {"id": "subtask_review"},
            "phase_node": {"id": "subtask_review", "manifest_id": "subtask_review"},
        },
        current_task_type="phase_run",
    )

    def _fake_command(*_args, **kwargs) -> str:
        return json.dumps(
            {
                "workspace_id": ws,
                "exit_code": 0,
                "command": ["python", "-c", "import demoapp"],
                "output": "0.0.1\n",
                "backend": "oneshot",
            }
        )

    with patch(
        "umbrella.deep_agent_tools.workspace_commands.run_workspace_command",
        side_effect=_fake_command,
    ):
        raw = _run_subtask_proof(ctx, subtask_id="project-setup")
    assert not str(raw).startswith("ERROR"), raw
    payload = json.loads(raw)
    assert payload["passed"] is True

    session = ctx.loop_state_view.get("completion_session") or {}
    allowed = session.get("allowed_tools") or []
    assert "submit_micro_review" in allowed
    assert "mark_subtask_complete" not in allowed


def test_run_subtask_proof_passes_execution_subdir(tmp_path: Path) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan = {
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "ui-build",
                        "status": "pending",
                        "proof": {
                            "execution": {
                                "kind": "build",
                                "command": ["npm", "run", "build"],
                                "subdir": "frontend",
                            },
                        },
                    }
                ],
            }
        ]
    }
    (state / "phase_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        umbrella_managed=True,
        umbrella_phase_id="execute",
        context_overlays={
            "phase_manifest": {"id": "execute"},
            "phase_node": {"id": "execute", "manifest_id": "execute"},
        },
        current_task_type="phase_run",
    )
    seen: dict[str, object] = {}

    def _fake_command(*_args, **kwargs) -> str:
        seen.update(kwargs)
        return json.dumps({"workspace_id": ws, "exit_code": 0, "backend": "oneshot"})

    with patch(
        "umbrella.deep_agent_tools.workspace_commands.run_workspace_command",
        side_effect=_fake_command,
    ):
        raw = _run_subtask_proof(ctx, subtask_id="ui-build")
    assert not str(raw).startswith("ERROR"), raw
    assert seen.get("subdir") == "frontend"


def test_run_subtask_proof_managed_runtime_launches_and_cleans_up(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    workspace.mkdir(parents=True)
    (workspace / "runtime_app.py").write_text(
        "import time\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan = {
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "runtime-smoke",
                        "status": "pending",
                        "files_to_create": ["runtime_app.py"],
                        "proof": {
                            "harness_profile": "desktop_gui_runtime",
                            "required_capabilities": [
                                "python",
                                "subprocess",
                                "desktop_gui_runtime",
                            ],
                            "harness_options": {
                                "managed_runtime": True,
                                "readiness": {"type": "log_contains", "text": "READY"},
                                "startup_timeout_sec": 5,
                                "cleanup": "kill process group after readiness proof",
                                "evidence": ["stdout log tail"],
                            },
                            "execution": {
                                "kind": "command",
                                "command": [sys.executable, "runtime_app.py"],
                                "timeout_sec": 10,
                                "shell": False,
                            },
                            "oracle": {
                                "oracle_type": "unit_assertions",
                                "required_properties": [
                                    "runtime_started",
                                    "no_test_tampering",
                                ],
                            },
                            "scope": {
                                "files_under_test": ["runtime_app.py"],
                                "changed_files_expected": ["runtime_app.py"],
                            },
                        },
                    }
                ],
            }
        ]
    }
    (state / "phase_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        umbrella_managed=True,
        umbrella_phase_id="execute",
        context_overlays={
            "phase_manifest": {"id": "execute"},
            "phase_node": {"id": "execute", "manifest_id": "execute"},
        },
        current_task_type="phase_run",
    )

    raw = _run_subtask_proof(ctx, subtask_id="runtime-smoke")

    assert not str(raw).startswith("ERROR"), raw
    payload = json.loads(raw)
    assert payload["passed"] is True
    shell_result = payload["shell_result"]
    assert shell_result["backend"] == "managed_runtime"
    assert shell_result["managed_runtime"]["ready"] is True
    assert shell_result["managed_runtime"]["missing_driver"] is False
    assert shell_result["managed_runtime"]["cleanup"]["alive_after"] is False


def test_mark_subtask_complete_requires_typed_contract_for_phase_subtask(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    workspace.mkdir(parents=True)
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan = {
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "project-setup",
                        "status": "pending",
                        "files_to_create": ["src/demoapp/__init__.py"],
                        "proof": {
                            "execution": {
                                "kind": "bool",
                                "command": ["python", "-c", "import demoapp"],
                            },
                            "oracle": {"required_properties": ["module_imports"]},
                        },
                    }
                ],
            }
        ]
    }
    plan_path = state / "phase_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        umbrella_managed=True,
        umbrella_phase_id="execute",
        context_overlays={
            "phase_manifest": {"id": "execute"},
            "phase_node": {"id": "execute", "manifest_id": "execute"},
        },
        current_task_type="phase_run",
    )

    raw = _mark_subtask_complete(
        ctx,
        subtask_id="project-setup",
        summary="Package import verification passed",
        evidence=["ledger_event:proof-1"],
    )

    assert "completion_contract is required" in raw
    saved = json.loads(plan_path.read_text(encoding="utf-8"))
    subtask = saved["nodes"][0]["subtasks"][0]
    assert subtask["status"] == "pending"
    assert subtask.get("completion") is None


def test_retry_state_counts_typed_run_subtask_proof_failures(tmp_path: Path) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "gmas-bot",
                                "status": "pending",
                                "files_to_create": ["src/demo/bots/economy_agent.py"],
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": [
                                            "python",
                                            "-m",
                                            "pytest",
                                            "tests/test_bots.py",
                                            "-q",
                                        ],
                                    },
                                    "scope": {
                                        "files_under_test": ["tests/test_bots.py"],
                                        "changed_files_expected": [
                                            "src/demo/bots/economy_agent.py"
                                        ],
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    row = {
        "task_id": "task:execute",
        "tool": "run_subtask_proof",
        "args": {"subtask_id": "gmas-bot"},
        "result_preview": json.dumps(
            {
                "passed": False,
                "exit_code": 0,
                "subtask_id": "gmas-bot",
                "command": ["python", "-m", "pytest", "tests/test_bots.py", "-q"],
                "shell_result": {
                    "output": "AttributeError: module 'gmas' has no attribute 'LLMConfig'"
                },
            }
        ),
    }
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for _ in range(3)) + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id="task:execute",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    assert _tool_row_success_status(row) == (False, "passed=false")
    retry_state = _phase_subtask_retry_state(ctx)
    assert retry_state is not None
    assert retry_state["failures"] == 3
    block = _phase_subtask_retry_escalation_block(
        ctx, tool_name="apply_workspace_patch"
    )
    assert block is not None
    assert block["reason"] == "phase_subtask_retry_escalation_required"
    assert "tests/test_bots.py" in block["required_context_reads"]


def test_retry_watcher_returns_typed_bad_test_contract_verdict(tmp_path: Path) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    task_id = "task:execute"
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "gmas-bot",
                                "status": "pending",
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": [
                                            "python",
                                            "-m",
                                            "pytest",
                                            "tests/test_bots.py",
                                            "-q",
                                        ],
                                    },
                                    "scope": {
                                        "files_under_test": ["tests/test_bots.py"],
                                        "changed_files_expected": [
                                            "src/demo/bots/economy_agent.py"
                                        ],
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    failed = {
        "task_id": task_id,
        "tool": "run_subtask_proof",
        "args": {"subtask_id": "gmas-bot"},
        "result_preview": json.dumps(
            {
                "passed": False,
                "exit_code": 1,
                "subtask_id": "gmas-bot",
                "command": ["python", "-m", "pytest", "tests/test_bots.py", "-q"],
                "shell_result": {"output": "AssertionError: expected impossible value"},
            }
        ),
    }
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(failed) for _ in range(3)) + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id=task_id,
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    payload = _phase_subtask_retry_watcher_review_payload(
        ctx,
        reason="bad generated success test contract: assertion is internally inconsistent",
    )

    assert payload["status"] == "review_recorded"
    assert payload["verdict"] == "bad_test_contract"
    assert payload["can_edit_tests"] is False
    assert payload["requires_plan_mutation"] is True
    assert payload["contract_migration"]["target_files"] == ["tests/test_bots.py"]
    assert payload["contract_migration"]["contract_migration_id"]


def test_retry_watcher_prefers_exact_declared_proof_failure(tmp_path: Path) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    task_id = "task:execute"
    declared = ["python", "-m", "pytest", "tests/test_gui.py", "-v"]
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "gui",
                                "status": "pending",
                                "files_to_change": ["src/demo/gui.py"],
                                "files_to_create": ["tests/test_gui.py"],
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": declared,
                                    },
                                    "scope": {
                                        "files_under_test": ["src/demo/gui.py"],
                                        "pytest_targets": ["tests/test_gui.py"],
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    partial = {
        "task_id": task_id,
        "tool": "shell",
        "args": {"command": [*declared, "-k", "equals_pressed"]},
        "result_preview": json.dumps(
            {
                "exit_code": 5,
                "command": [*declared, "-k", "equals_pressed"],
                "output": "collected 28 items / 28 deselected / 0 selected",
            }
        ),
    }
    exact = {
        "task_id": task_id,
        "tool": "shell",
        "args": {"command": declared},
        "result_preview": json.dumps(
            {
                "exit_code": 1,
                "command": declared,
                "output": "FAILED tests/test_gui.py::test_controller_repeated_equals",
            }
        ),
    }
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in [partial, exact, exact]) + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id=task_id,
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    state_payload = _phase_subtask_retry_state(ctx)
    assert state_payload is not None
    assert state_payload["failures"] == 3
    payload = _phase_subtask_retry_watcher_review_payload(
        ctx,
        reason="repeated declared proof failures",
    )

    assert payload["status"] == "review_recorded"
    assert payload["latest_failure"]["command"] == declared
    assert "-k" not in payload["latest_failure"]["output_excerpt"]
    assert "test_controller_repeated_equals" in payload["latest_failure"]["output_excerpt"]


def test_watcher_implementation_bug_clears_oracle_freeze_gate_and_prioritizes_source_repair(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    task_id = "task:execute"
    success_test = "python -m pytest tests/test_gui.py -q"
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "gui",
                                "status": "pending",
                                "files_to_change": ["src/demo/gui.py"],
                                "files_to_create": ["tests/test_gui.py"],
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": [
                                            "python",
                                            "-m",
                                            "pytest",
                                            "tests/test_gui.py",
                                            "-q",
                                        ],
                                    },
                                    "scope": {
                                        "files_under_test": ["src/demo/gui.py"],
                                        "pytest_targets": ["tests/test_gui.py"],
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    patch_mismatch = {
        "task_id": task_id,
        "tool": "apply_workspace_patch",
        "args": {"patch": "*** Update File: tests/test_gui.py\n@@\n"},
        "result_preview": json.dumps(
            {
                "status": "blocked",
                "reason": "patch_hunk_mismatch",
                "file_path": "tests/test_gui.py",
            }
        ),
    }
    failed = {
        "task_id": task_id,
        "tool": "run_subtask_proof",
        "args": {"subtask_id": "gui"},
        "result_preview": json.dumps(
            {
                "passed": False,
                "exit_code": 1,
                "subtask_id": "gui",
                "command": ["python", "-m", "pytest", "tests/test_gui.py", "-q"],
                "shell_result": {
                    "output": "AssertionError: controller display did not reset"
                },
            }
        ),
    }
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in [patch_mismatch, failed, failed, failed])
        + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id=task_id,
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
        loop_state_view={
            "typed_action_gate": {
                "reason": "no_test_tampering_oracle_freeze",
                "blocked_tools": ["apply_workspace_patch"],
                "allowed_next_tools": ["read_file", "request_watcher_review"],
            }
        },
    )

    payload = json.loads(
        _request_watcher_review(
            ctx,
            reason=(
                "Implementation bug in src/demo/gui.py: display state is not reset; "
                "repair source, do not edit tests."
            ),
        )
    )

    assert payload["status"] == "review_recorded"
    assert payload["verdict"] == "implementation_bug"
    assert payload["cleared_typed_action_gate"] == "no_test_tampering_oracle_freeze"
    assert "typed_action_gate" not in ctx.loop_state_view
    assert "implementation repair" in payload["recommendation"]
    assert "exact-context repair" not in payload["recommendation"]
    assert "exact-context repair" in payload["secondary_patch_guidance"]
    assert payload["patch_guidance"] == ""
    assert payload["suppressed_patch_guidance"]
    assert payload["repair_focus"]["source_files"] == ["src/demo/gui.py"]


def test_watcher_review_handles_successful_patch_before_failed_proof(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    task_id = "task:execute"
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "setup",
                                "status": "pending",
                                "files_to_change": ["src/demo/core.py"],
                                "files_to_create": ["tests/test_core.py"],
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": [
                                            "python",
                                            "-m",
                                            "pytest",
                                            "tests/test_core.py",
                                            "-q",
                                        ],
                                    },
                                    "scope": {
                                        "files_under_test": ["src/demo/core.py"],
                                        "pytest_targets": ["tests/test_core.py"],
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    applied_patch = {
        "task_id": task_id,
        "tool": "apply_workspace_patch",
        "args": {"patch": "*** Add File: src/demo/core.py\n+VALUE = 1\n"},
        "result_preview": json.dumps({"status": "applied", "applied": True}),
    }
    failed = {
        "task_id": task_id,
        "tool": "run_subtask_proof",
        "args": {"subtask_id": "setup"},
        "result_preview": json.dumps(
            {
                "passed": False,
                "exit_code": 1,
                "subtask_id": "setup",
                "command": ["python", "-m", "pytest", "tests/test_core.py", "-q"],
                "shell_result": {"output": "NameError: name 'pytest' is not defined"},
            }
        ),
    }
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in [applied_patch, failed]) + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id=task_id,
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
        loop_state_view={},
    )

    payload = json.loads(
        _request_watcher_review(
            ctx,
            reason=(
                "Latest proof failed after a successful source patch; watcher "
                "review should classify the failure instead of crashing."
            ),
        )
    )

    assert payload["status"] == "review_recorded"
    assert payload["verdict"] == "implementation_bug"
    assert payload["patch_guidance"] == ""
    assert payload["latest_failure"]["tool"] == "run_subtask_proof"


def test_watcher_not_required_suppresses_protected_test_patch_guidance(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    task_id = "task:execute"
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "core",
                                "status": "pending",
                                "files_to_change": ["src/demo/core.py"],
                                "files_to_create": ["tests/test_core.py"],
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": [
                                            "python",
                                            "-m",
                                            "pytest",
                                            "tests/test_core.py",
                                            "-q",
                                        ],
                                    },
                                    "scope": {
                                        "files_under_test": ["src/demo/core.py"],
                                        "pytest_targets": ["tests/test_core.py"],
                                    },
                                    "anti_gaming": {
                                        "allows_test_only_change": False,
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": task_id,
            "tool": "run_subtask_proof",
            "args": {"subtask_id": "core"},
            "result_preview": json.dumps(
                {
                    "passed": False,
                    "exit_code": 1,
                    "subtask_id": "core",
                    "command": ["python", "-m", "pytest", "tests/test_core.py", "-q"],
                    "shell_result": {"output": "ValueError: invalid literal"},
                }
            ),
        },
        {
            "task_id": task_id,
            "tool": "apply_workspace_patch",
            "args": {"patch": "*** Update File: tests/test_core.py\n@@\n"},
            "result_preview": json.dumps(
                {
                    "status": "blocked",
                    "reason": "patch_hunk_mismatch",
                    "file_path": "tests/test_core.py",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id=task_id,
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    payload = _phase_subtask_retry_watcher_review_payload(
        ctx,
        reason="The current proof failed; I need guidance before editing tests.",
    )

    assert payload["status"] == "review_not_required"
    assert payload["verdict"] == "not_required"
    assert payload["can_edit_tests"] is False
    assert payload["patch_guidance"] == ""
    assert "tests/test_core.py" in payload["suppressed_patch_guidance"]
    assert "tests/test_core.py" not in payload["recommendation"]
    assert "implementation repair" in payload["recommendation"]
    assert "protected test/proof oracle" in payload["message"]


def test_replace_workspace_file_counts_as_repair_after_retry_watcher(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    success_test = "python -m pytest tests/test_bots.py -q"
    task_id = "task:execute"
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "gmas-bot",
                                "status": "pending",
                                "success_test": success_test,
                                "files_to_change": ["src/demo/bots/economy_agent.py"],
                                "files_under_test": ["tests/test_bots.py"],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    failed = {
        "task_id": task_id,
        "tool": "run_subtask_proof",
        "args": {"subtask_id": "gmas-bot"},
        "result_preview": json.dumps(
            {
                "passed": False,
                "exit_code": 1,
                "subtask_id": "gmas-bot",
                "command": ["python", "-m", "pytest", "tests/test_bots.py", "-q"],
                "shell_result": {"output": "AssertionError"},
            }
        ),
    }
    watcher = {
        "task_id": task_id,
        "tool": "request_watcher_review",
        "result_preview": json.dumps(
            {
                "status": "review_recorded",
                "reviewer": "umbrella",
                "review_kind": "retry_watcher",
                "subtask_id": "gmas-bot",
                "success_test": success_test,
                "failed_attempts": 3,
            }
        ),
    }
    repair = {
        "task_id": task_id,
        "tool": "replace_workspace_file",
        "result_preview": json.dumps(
            {"status": "ok", "path": "src/demo/bots/economy_agent.py"}
        ),
    }
    rows = [failed, failed, failed, watcher, repair]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id=task_id,
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    retry_state = _phase_subtask_retry_state(ctx)
    assert retry_state is not None
    assert retry_state["failures"] == 3
    assert retry_state["watcher_reviews"] == 1
    block = _phase_subtask_retry_escalation_block(ctx, tool_name="run_subtask_proof")
    assert block is None


def test_replace_workspace_file_blocked_before_retry_watcher(tmp_path: Path) -> None:
    repo = tmp_path
    ws = "demo"
    workspace = repo / "workspaces" / ws
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (workspace / "src" / "demo").mkdir(parents=True)
    target = workspace / "src" / "demo" / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    task_id = "task:execute"
    success_test = "python -m pytest tests/test_app.py -q"
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "app",
                                "status": "pending",
                                "success_test": success_test,
                                "files_to_change": ["src/demo/app.py"],
                                "files_under_test": ["tests/test_app.py"],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    failed = {
        "task_id": task_id,
        "tool": "run_subtask_proof",
        "args": {"subtask_id": "app"},
        "result_preview": json.dumps(
            {
                "passed": False,
                "exit_code": 1,
                "subtask_id": "app",
                "command": ["python", "-m", "pytest", "tests/test_app.py", "-q"],
                "shell_result": {"output": "AssertionError"},
            }
        ),
    }
    logs.joinpath("tools.jsonl").write_text(
        "\n".join(json.dumps(failed) for _ in range(3)) + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id=task_id,
        current_task_type="phase_run",
        workspace_root_overrides={ws: str(workspace)},
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )
    read_workspace_file(ctx, ws, "src/demo/app.py")
    before_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    payload = json.loads(
        replace_workspace_file(ctx, ws, "src/demo/app.py", before_sha, "value = 2\n")
    )

    assert payload["reason"] == "phase_subtask_retry_escalation_required"
    assert payload["tool"] == "replace_workspace_file"
    assert target.read_text(encoding="utf-8") == "value = 1\n"
