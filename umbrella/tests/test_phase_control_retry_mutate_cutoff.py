"""Retry escalation ignores proof failures recorded before mutate_phase_plan."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from umbrella.deep_agent_tools.phase_control_retry import _phase_subtask_retry_state


def _write_phase_plan(drive: Path) -> None:
    plan = {
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "project-setup",
                        "status": "pending",
                        "proof": {
                            "execution": {
                                "kind": "build",
                                "command": [
                                    "python",
                                    "-c",
                                    "import calculator",
                                ],
                            }
                        },
                    }
                ],
            }
        ],
    }
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False),
        encoding="utf-8",
    )


def test_retry_state_ignores_failures_before_mutate_phase_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace_id = "demo"
    drive = repo / "workspaces" / workspace_id / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    _write_phase_plan(drive)

    task_id = "run-mutate:execute"
    proof_args = {
        "command": [
            "python",
            "-c",
            "import calculator",
        ]
    }
    tool_rows = [
        {
            "created_at": 100.0,
            "tool": "run_subtask_proof",
            "task_id": task_id,
            "args": proof_args,
            "result_preview": json.dumps({"passed": False, "exit_code": 1}),
        },
        {
            "created_at": 110.0,
            "tool": "run_subtask_proof",
            "task_id": task_id,
            "args": proof_args,
            "result_preview": json.dumps({"passed": False, "exit_code": 1}),
        },
        {
            "created_at": 120.0,
            "tool": "run_subtask_proof",
            "task_id": task_id,
            "args": proof_args,
            "result_preview": json.dumps({"passed": False, "exit_code": 1}),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in tool_rows) + "\n",
        encoding="utf-8",
    )

    ctx = SimpleNamespace(
        task_id=task_id,
        drive_root=drive,
        workspace_id=workspace_id,
        repo_root=str(repo),
        current_task_type="phase_run",
    )
    before_mutate = _phase_subtask_retry_state(ctx)
    assert before_mutate is not None
    assert int(before_mutate.get("failures") or 0) == 3

    (state / "phase_control_signals.jsonl").write_text(
        json.dumps(
            {
                "kind": "mutate_phase_plan",
                "created_at": 130.0,
                "task_id": task_id,
                "payload": {"patch": {"subtasks": [{"id": "project-setup"}]}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    after_mutate = _phase_subtask_retry_state(ctx)
    assert after_mutate is not None
    assert int(after_mutate.get("failures") or 0) == 0

    (logs / "tools.jsonl").write_text(
        (logs / "tools.jsonl").read_text(encoding="utf-8")
        + json.dumps(
            {
                "created_at": 140.0,
                "tool": "run_subtask_proof",
                "task_id": task_id,
                "args": proof_args,
                "result_preview": json.dumps({"passed": False, "exit_code": 1}),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with_failures = _phase_subtask_retry_state(ctx)
    assert with_failures is not None
    assert int(with_failures.get("failures") or 0) == 1
