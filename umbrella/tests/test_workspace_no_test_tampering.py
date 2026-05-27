from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from umbrella.deep_agent_tools.workspace_ops import (
    apply_workspace_patch,
    replace_workspace_file,
)
from umbrella.deep_agent_tools.workspace_read import read_workspace_file


def _phase_ctx(
    tmp_path: Path,
    *,
    path_key: str = "files_to_create",
    harness_profile: str = "",
):
    workspace = tmp_path / "workspaces" / "demo"
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (workspace / "tests").mkdir(parents=True)
    subtask = {
        "id": "behavioral",
        "status": "pending",
        path_key: ["tests/test_e2e.py"],
        "proof": {
            "execution": {
                "kind": "pytest",
                "command": ["python", "-m", "pytest", "tests/test_e2e.py", "-q"],
            },
            "oracle": {"required_properties": ["no_test_tampering"]},
            "scope": {
                "files_under_test": ["tests/test_e2e.py"],
                "changed_files_expected": ["tests/test_e2e.py"],
                "pytest_targets": ["tests/test_e2e.py"],
            },
        },
    }
    if harness_profile:
        subtask["proof"]["harness_profile"] = harness_profile
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [subtask],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        host_repo_root=tmp_path,
        repo_dir=tmp_path,
        drive_root=drive,
        task_id="run:execute:1",
        current_task_type="phase_run",
        workspace_root_overrides={"demo": str(workspace)},
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )
    return ctx, workspace


def test_no_test_tampering_blocks_skip_on_added_test_file(tmp_path: Path) -> None:
    ctx, workspace = _phase_ctx(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: tests/test_e2e.py\n"
        "+import pytest\n"
        "+def test_behavior():\n"
        "+    pytest.skip('tk unavailable')\n"
        "*** End Patch\n"
    )

    payload = json.loads(apply_workspace_patch(ctx, "demo", patch))

    assert payload["reason"] == "no_test_tampering_static_guard"
    assert payload["issues"][0]["code"] == "pytest_skip_or_xfail"
    assert not (workspace / "tests" / "test_e2e.py").exists()


def test_no_test_tampering_blocks_skipif_on_replacement(tmp_path: Path) -> None:
    ctx, workspace = _phase_ctx(tmp_path, path_key="files_to_change")
    target = workspace / "tests" / "test_e2e.py"
    target.write_text(
        "def test_behavior():\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    read_workspace_file(ctx, "demo", "tests/test_e2e.py")
    before_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    payload = json.loads(
        replace_workspace_file(
            ctx,
            "demo",
            "tests/test_e2e.py",
            before_sha,
            "import pytest\n"
            "@pytest.mark.skipif(True, reason='tk unavailable')\n"
            "def test_behavior():\n"
            "    assert 1 + 1 == 2\n",
        )
    )

    assert payload["reason"] == "no_test_tampering_static_guard"
    assert payload["issues"][0]["code"] == "pytest_skip_or_xfail"
    assert target.read_text(encoding="utf-8").startswith("def test_behavior")


def test_no_test_tampering_blocks_weak_only_none_assertion_on_replacement(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path, path_key="files_to_change")
    target = workspace / "tests" / "test_e2e.py"
    target.write_text(
        "def test_behavior():\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    read_workspace_file(ctx, "demo", "tests/test_e2e.py")
    before_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    payload = json.loads(
        replace_workspace_file(
            ctx,
            "demo",
            "tests/test_e2e.py",
            before_sha,
            "def test_behavior():\n"
            "    state = type('S', (), {'display': '0', 'pending': None})()\n"
            "    assert state.pending is None\n",
        )
    )

    assert payload["reason"] == "no_test_tampering_static_guard"
    assert payload["issues"][0]["code"] == "weak_not_none_assertion"
    assert "state.pending is None" not in target.read_text(encoding="utf-8")


def test_no_test_tampering_allows_none_assertion_with_behavioral_signal(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: tests/test_e2e.py\n"
        "+def test_behavior():\n"
        "+    state = type('S', (), {'display': '0', 'pending': None})()\n"
        "+    assert state.display == '0'\n"
        "+    assert state.pending is None\n"
        "*** End Patch\n"
    )

    payload = json.loads(apply_workspace_patch(ctx, "demo", patch))

    assert payload["status"] == "applied"
    assert (workspace / "tests" / "test_e2e.py").exists()


def test_no_test_tampering_allows_mixed_none_assertion_with_behavioral_signal(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: tests/test_e2e.py\n"
        "+def test_behavior():\n"
        "+    state = type('S', (), {'display': '0', 'pending': None})()\n"
        "+    assert state.display == '0' and state.pending is None\n"
        "*** End Patch\n"
    )

    payload = json.loads(apply_workspace_patch(ctx, "demo", patch))

    assert payload["status"] == "applied"
    assert (workspace / "tests" / "test_e2e.py").exists()


def test_no_test_tampering_blocks_test_only_oracle_rewrite_after_failed_proof(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path, path_key="files_to_change")
    target = workspace / "tests" / "test_e2e.py"
    target.write_text(
        "def test_display_format(calc):\n"
        "    calc.press('1')\n"
        "    calc.press('0')\n"
        "    assert calc.get_display() == '10.0'\n",
        encoding="utf-8",
    )
    success_test = "python -m pytest tests/test_e2e.py -q"
    failed = {
        "task_id": ctx.task_id,
        "tool": "run_subtask_proof",
        "args": {"subtask_id": "behavioral"},
        "result_preview": json.dumps(
            {
                "passed": False,
                "exit_code": 1,
                "subtask_id": "behavioral",
                "command": ["python", "-m", "pytest", "tests/test_e2e.py", "-q"],
                "shell_result": {
                    "output": "AssertionError: assert '10' == '10.0'"
                },
            }
        ),
    }
    watcher = {
        "task_id": ctx.task_id,
        "tool": "request_watcher_review",
        "result_preview": json.dumps(
            {
                "status": "review_recorded",
                "reviewer": "umbrella",
                "review_kind": "retry_watcher",
                "subtask_id": "behavioral",
                "success_test": success_test,
                "failed_attempts": 3,
                "verdict": "implementation_bug",
                "can_edit_tests": False,
            }
        ),
    }
    logs = Path(ctx.drive_root) / "logs"
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in [failed, failed, failed, watcher]) + "\n",
        encoding="utf-8",
    )
    read_workspace_file(ctx, "demo", "tests/test_e2e.py")

    payload = json.loads(
        apply_workspace_patch(
            ctx,
            "demo",
            "*** Begin Patch\n"
            "*** Update File: tests/test_e2e.py\n"
            "@@\n"
            "-    assert calc.get_display() == '10.0'\n"
            "+    assert calc.get_display() == '10'\n"
            "*** End Patch\n",
        )
    )

    assert payload["reason"] == "no_test_tampering_oracle_freeze"
    assert payload["test_paths"] == ["tests/test_e2e.py"]
    assert "10.0" in target.read_text(encoding="utf-8")


def test_no_test_tampering_blocks_real_tk_root_in_gui_proof(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path, harness_profile="desktop_gui_headless")
    patch = (
        "*** Begin Patch\n"
        "*** Add File: tests/test_e2e.py\n"
        "+import tkinter as tk\n"
        "+def test_gui_behavior():\n"
        "+    root = tk.Tk()\n"
        "+    assert root is not None\n"
        "*** End Patch\n"
    )

    payload = json.loads(apply_workspace_patch(ctx, "demo", patch))

    assert payload["reason"] == "no_test_tampering_static_guard"
    assert payload["issues"][0]["code"] == "native_gui_root_in_test"
    assert not (workspace / "tests" / "test_e2e.py").exists()


def test_no_test_tampering_blocks_indirect_gui_root_in_headless_proof(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path, harness_profile="desktop_gui_headless")
    patch = (
        "*** Begin Patch\n"
        "*** Add File: tests/test_e2e.py\n"
        "+from calculator.gui import CalculatorGUI\n"
        "+def test_gui_behavior():\n"
        "+    gui = CalculatorGUI(root=None)\n"
        "+    assert gui.display_value() == '0'\n"
        "*** End Patch\n"
    )

    payload = json.loads(apply_workspace_patch(ctx, "demo", patch))

    assert payload["reason"] == "no_test_tampering_static_guard"
    assert payload["issues"][0]["code"] == "implicit_native_gui_root_in_test"
    assert not (workspace / "tests" / "test_e2e.py").exists()


def test_no_test_tampering_blocks_gui_root_none_inside_test_helper(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path, harness_profile="desktop_gui_headless")
    patch = (
        "*** Begin Patch\n"
        "*** Add File: tests/test_e2e.py\n"
        "+from calculator.gui import CalculatorGUI\n"
        "+class HeadlessHarness:\n"
        "+    def __init__(self):\n"
        "+        self.gui = CalculatorGUI(root=None)\n"
        "+    def display(self):\n"
        "+        return self.gui.display_value()\n"
        "+def test_gui_behavior():\n"
        "+    assert HeadlessHarness().display() == '0'\n"
        "*** End Patch\n"
    )

    payload = json.loads(apply_workspace_patch(ctx, "demo", patch))

    assert payload["reason"] == "no_test_tampering_static_guard"
    assert payload["issues"][0]["code"] == "implicit_native_gui_root_in_test"
    assert not (workspace / "tests" / "test_e2e.py").exists()


def test_desktop_gui_runtime_harness_allows_real_root_smoke_shape(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path, harness_profile="desktop_gui_runtime")
    patch = (
        "*** Begin Patch\n"
        "*** Add File: tests/test_e2e.py\n"
        "+import tkinter as tk\n"
        "+from calculator.gui import CalculatorGUI\n"
        "+def test_gui_smoke_shape():\n"
        "+    root = tk.Tk()\n"
        "+    gui = CalculatorGUI(root=None)\n"
        "+    assert root.winfo_exists() >= 0\n"
        "+    assert gui.display_value() == '0'\n"
        "*** End Patch\n"
    )

    payload = json.loads(apply_workspace_patch(ctx, "demo", patch))

    assert payload["status"] == "applied"
    assert (workspace / "tests" / "test_e2e.py").exists()


def test_no_test_tampering_without_headless_harness_does_not_globally_block_tk_root(
    tmp_path: Path,
) -> None:
    ctx, workspace = _phase_ctx(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: tests/test_e2e.py\n"
        "+import tkinter as tk\n"
        "+def test_gui_smoke_shape():\n"
        "+    root = tk.Tk()\n"
        "+    assert root.winfo_exists() >= 0\n"
        "*** End Patch\n"
    )

    payload = json.loads(apply_workspace_patch(ctx, "demo", patch))

    assert payload["status"] == "applied"
    assert (workspace / "tests" / "test_e2e.py").exists()
