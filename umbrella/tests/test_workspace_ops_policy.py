import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from umbrella.deep_agent_tools.workspace_ops import (
    apply_workspace_patch,
    replace_workspace_file,
)
from umbrella.deep_agent_tools.workspace_read import read_workspace_file
from umbrella.enforcement.kernel import check_workspace_paths, phase_from_context


def _workspace_ctx(tmp_path: Path) -> tuple[SimpleNamespace, Path]:
    workspace = tmp_path / "workspaces" / "demo"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    (workspace / "src" / "demo").mkdir(parents=True)
    ctx = SimpleNamespace(
        host_repo_root=tmp_path,
        repo_dir=tmp_path,
        drive_root=drive,
        task_id="run:execute:1",
        current_task_type="phase_run",
        workspace_root_overrides={"demo": str(workspace)},
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
        loop_state_view={},
    )
    return ctx, workspace


def test_repeated_hunk_mismatch_points_to_single_replacement_protocol(
    tmp_path: Path,
) -> None:
    ctx, workspace = _workspace_ctx(tmp_path)
    target = workspace / "src" / "demo" / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tools_log = workspace / ".memory" / "drive" / "logs" / "tools.jsonl"
    mismatch_payload = {
        "status": "blocked",
        "reason": "patch_hunk_mismatch",
        "file_path": "src/demo/app.py",
    }
    tools_log.write_text(
        "\n".join(
            json.dumps(
                {
                    "ts": f"2000-01-01T00:00:0{idx}Z",
                    "task_id": ctx.task_id,
                    "tool": "apply_workspace_patch",
                    "result_preview": json.dumps(mismatch_payload),
                }
            )
            for idx in range(2)
        )
        + "\n",
        encoding="utf-8",
    )
    read_workspace_file(ctx, "demo", "src/demo/app.py")

    blocked_update = json.loads(
        apply_workspace_patch(
            ctx,
            "demo",
            (
                "*** Begin Patch\n"
                "*** Update File: src/demo/app.py\n"
                "@@\n"
                "-missing = 1\n"
                "+value = 2\n"
                "*** End Patch\n"
            ),
        )
    )

    assert blocked_update["reason"] == "patch_hunk_mismatch_replacement_required"
    assert "required_mode" not in blocked_update
    assert "apply_workspace_patch" in blocked_update["next_step"]
    assert "*** Delete File: src/demo/app.py" in blocked_update["required_patch_shape"]

    before_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    blocked_replace = json.loads(
        replace_workspace_file(ctx, "demo", "src/demo/app.py", before_sha, "value = 2\n")
    )

    assert blocked_replace["reason"] == "patch_hunk_mismatch_replacement_required"
    assert "required_mode" not in blocked_replace

    replaced = json.loads(
        apply_workspace_patch(
            ctx,
            "demo",
            (
                "*** Begin Patch\n"
                "*** Delete File: src/demo/app.py\n"
                "*** Add File: src/demo/app.py\n"
                "+value = 2\n"
                "*** End Patch\n"
            ),
            validation_summary="Replacing after repeated hunk mismatch.",
        )
    )

    assert replaced["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_workspace_toml_is_not_special_cased_as_agent_policy_edit(tmp_path: Path) -> None:
    issues = check_workspace_paths(
        "apply_workspace_patch",
        "execute",
        ["workspace.toml", "pyproject.toml"],
        write_kind="patch",
    )

    assert [issue.code for issue in issues] == [
        "verifier_policy_write_requires_supervisor_approval"
    ]


def test_workspace_toml_allowance_does_not_cover_other_policy_files(tmp_path: Path) -> None:
    issues = check_workspace_paths(
        "apply_workspace_patch",
        "execute",
        ["workspace.toml", "verification.toml"],
        write_kind="patch",
    )

    assert [issue.code for issue in issues] == [
        "verifier_policy_write_requires_supervisor_approval",
        "verifier_policy_write_requires_supervisor_approval",
    ]


def test_phase_from_context_reads_phase_overlay_before_timestamp_suffix(tmp_path: Path) -> None:
    ctx = SimpleNamespace(
        task_id="phase_web_demo:execute:1779802980685",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )

    assert phase_from_context(ctx) == "execute"


def test_replace_workspace_file_records_ledger_phase_from_overlay(tmp_path: Path) -> None:
    ctx, workspace = _workspace_ctx(tmp_path)
    ctx.task_id = "phase_web_demo:execute:1779802980685"
    target = workspace / "src" / "demo" / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    read_workspace_file(ctx, "demo", "src/demo/app.py")
    before_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    payload = json.loads(
        replace_workspace_file(
            ctx,
            "demo",
            "src/demo/app.py",
            before_sha,
            "value = 2\n",
            validation_summary="Replace with current full file content.",
        )
    )

    assert payload["status"] == "ok"
    assert payload["ledger_event_id"]
    assert payload["ledger_ref"]["phase"] == "execute"


def test_execute_file_scope_is_advisory_for_shared_source_files(tmp_path: Path) -> None:
    ctx, workspace = _workspace_ctx(tmp_path)
    state = workspace / ".memory" / "drive" / "state"
    (workspace / "src" / "demo" / "__init__.py").write_text(
        "from demo.core import App\n",
        encoding="utf-8",
    )
    (workspace / "src" / "demo" / "core.py").write_text(
        "class App:\n    pass\n",
        encoding="utf-8",
    )
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
                                "id": "logic",
                                "status": "pending",
                                "files_to_change": ["src/demo/core.py"],
                            },
                            {
                                "id": "gui",
                                "status": "pending",
                                "files_to_change": ["src/demo/__init__.py"],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    read_workspace_file(ctx, "demo", "src/demo/core.py")
    read_workspace_file(ctx, "demo", "src/demo/__init__.py")

    payload = json.loads(
        apply_workspace_patch(
            ctx,
            "demo",
            (
                "*** Begin Patch\n"
                "*** Update File: src/demo/__init__.py\n"
                "@@\n"
                "-from demo.core import App\n"
                "+from demo.core import App, Engine\n"
                "*** End Patch\n"
            ),
        )
    )

    assert payload["status"] == "applied"
    assert "Engine" in (workspace / "src" / "demo" / "__init__.py").read_text(
        encoding="utf-8"
    )
