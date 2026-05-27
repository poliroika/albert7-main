import json
import time
from pathlib import Path

from ouroboros.tools.registry import ToolContext
from umbrella.deep_agent_tools.phase_control_actions import (
    _watcher_force_verify_completion_issue,
)


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-1:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {
        "phase_node": {
            "id": "execute",
            "manifest_id": "execute",
            "overlay": {
                "watcher_force_verify": True,
                "watcher_force_verify_after": 100.0,
                "watcher_force_verify_tool_row_floor": 1,
            },
        }
    }
    (tmp_path / "logs").mkdir(exist_ok=True)
    return ctx


def _write_rows(tmp_path: Path, rows: list[dict]) -> None:
    (tmp_path / "logs" / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_watcher_force_verify_blocks_without_fresh_proof(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _write_rows(
        tmp_path,
        [
            {
                "task_id": "run-1:execute",
                "tool": "run_subtask_proof",
                "ts": time.time(),
                "result_preview": json.dumps({"passed": True}),
            }
        ],
    )

    issue = _watcher_force_verify_completion_issue(ctx)

    assert "watcher_force_verify is active" in issue


def test_watcher_force_verify_accepts_fresh_passing_proof(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _write_rows(
        tmp_path,
        [
            {
                "task_id": "run-1:execute",
                "tool": "apply_workspace_patch",
                "ts": time.time(),
                "result_preview": json.dumps({"status": "applied", "applied": True}),
            },
            {
                "task_id": "run-1:execute",
                "tool": "run_subtask_proof",
                "ts": time.time(),
                "result_preview": json.dumps({"passed": True}),
            },
        ],
    )

    assert _watcher_force_verify_completion_issue(ctx) == ""


def test_watcher_force_verify_rejects_write_after_proof(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.context_overlays["phase_node"]["overlay"][
        "watcher_force_verify_tool_row_floor"
    ] = 0
    _write_rows(
        tmp_path,
        [
            {
                "task_id": "run-1:execute",
                "tool": "run_subtask_proof",
                "ts": time.time(),
                "result_preview": json.dumps({"passed": True}),
            },
            {
                "task_id": "run-1:execute",
                "tool": "apply_workspace_patch",
                "ts": time.time(),
                "result_preview": json.dumps({"status": "applied", "applied": True}),
            },
        ],
    )

    issue = _watcher_force_verify_completion_issue(ctx)

    assert "workspace changed after the latest passing proof" in issue
