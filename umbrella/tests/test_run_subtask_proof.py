"""run_subtask_proof returns verifier ledger refs for completion contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from umbrella.contracts.models import CompletionContract
from umbrella.contracts.validators import ContractValidator
from umbrella.contracts import ContractBundle, build_workspace_context
from umbrella.deep_agent_tools.phase_control_actions import _run_subtask_proof
from umbrella.deep_agent_tools.phase_control_retry import (
    _phase_subtask_retry_escalation_block,
    _phase_subtask_retry_state,
    _tool_row_success_status,
)
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
