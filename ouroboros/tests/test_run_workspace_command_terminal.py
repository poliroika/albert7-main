"""Tests for the persistent-terminal wrapping of ``run_workspace_command``.

Focus: the wrapper must (a) delegate to the per-workspace
``TerminalSession`` it gets from ``get_or_create_session``, and (b) append
the result to ``<drive_root>/memory/terminal_scrollback.md`` so the
``## Recent terminal`` context section can show it on the next round.

These tests stub the session backend with a recording fake so they are
fully cross-platform (no tmux/bash dependency).
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from collections.abc import Sequence

import pytest

from ouroboros.tools import umbrella_tools
from ouroboros.tools.terminal_session import RunResult, TerminalSession
from ouroboros.tools.terminal_session import OneShotBackend


class _RecordingBackend:
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.started = False
        self.killed = False
        self.reset_count = 0
        self._scrollback_lines: list[str] = []

    def start(self) -> None:
        self.started = True

    def run(
        self, cmd: Sequence[str] | str, *, cwd: str | None, timeout: int
    ) -> RunResult:
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        cmd_repr = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        body = f"recorded: {cmd_repr}"
        self._scrollback_lines.append(body)
        now = time.time()
        return RunResult(
            exit_code=0,
            output=body,
            marker="00112233",
            started_at=now - 0.1,
            finished_at=now,
            raw_output=body,
        )

    def view(self, *, last_lines: int = 200, grep: str | None = None) -> str:
        return "\n".join(self._scrollback_lines[-last_lines:])

    def reset(self) -> None:
        self.reset_count += 1
        self._scrollback_lines.clear()

    def kill(self) -> None:
        self.killed = True


class _FakeCtx:
    """Minimal ToolContext-shaped stub."""

    def __init__(self, repo_root: Path, drive_root: Path) -> None:
        self.repo_dir = repo_root
        self.host_repo_root = repo_root
        self.drive_root = drive_root
        self.task_id = ""


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[_FakeCtx, Path, str]:
    repo_root = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    workspaces_dir = repo_root / "workspaces" / "demo"
    workspaces_dir.mkdir(parents=True)
    (repo_root / "umbrella").mkdir()
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(repo_root, drive_root)
    return ctx, workspaces_dir, "demo"


def _install_recording_session(ctx: _FakeCtx, workspace_id: str) -> _RecordingBackend:
    backend = _RecordingBackend()
    sess = TerminalSession(workspace_id=workspace_id, backend=backend)
    ctx._terminal_sessions = {workspace_id: sess}
    return backend


def test_run_workspace_command_uses_persistent_session(workspace, monkeypatch) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["echo", "hi"],
        timeout_seconds=5,
    )

    assert backend.calls, "wrapper must delegate to the persistent session"
    assert backend.calls[0]["cmd"] == ["echo", "hi"]

    payload = json.loads(out)
    assert payload["exit_code"] == 0
    assert "hi" in payload["output"]
    assert payload["backend"] == "recording"


def test_run_workspace_command_rewrites_python_to_repo_venv(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    venv_python = (
        ctx.host_repo_root
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-c", "print('ok')"],
        timeout_seconds=5,
    )

    assert backend.calls[0]["cmd"] == [str(venv_python), "-c", "print('ok')"]


def test_run_workspace_command_blocks_after_stop_request(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    ctx.task_id = "run_stop__remediation_1"
    backend = _install_recording_session(ctx, ws_id)
    state_dir = ctx.drive_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "stop_requested.json").write_text(
        json.dumps(
            {"run_id": "run_stop", "attempt_task_ids": ["run_stop"], "reason": "cancel"}
        ),
        encoding="utf-8",
    )

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["echo", "should-not-run"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "stop_requested"
    assert backend.calls == []


def test_update_workspace_seed_blocks_after_stop_request(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.task_id = "run_stop__remediation_1"
    state_dir = ctx.drive_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "stop_requested.json").write_text(
        json.dumps(
            {"run_id": "run_stop", "attempt_task_ids": ["run_stop"], "reason": "cancel"}
        ),
        encoding="utf-8",
    )

    raw = umbrella_tools.update_workspace_seed(
        ctx,
        workspace_id=ws_id,
        file_path="blocked.py",
        new_content="print('blocked')\n",
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "stop_requested"
    assert not (ws_dir / "blocked.py").exists()


def test_run_workspace_command_appends_scrollback(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    _install_recording_session(ctx, ws_id)

    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["echo", "scrollback_probe"],
        timeout_seconds=5,
    )

    scrollback = ctx.drive_root / "memory" / "terminal_scrollback.md"
    assert scrollback.exists(), "scrollback file must be created"
    text = scrollback.read_text(encoding="utf-8")
    assert f"ws={ws_id}" in text
    assert "echo scrollback_probe" in text
    assert "scrollback_probe" in text


def test_terminal_view_returns_recorded_lines(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    _install_recording_session(ctx, ws_id)

    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["echo", "alpha"],
        timeout_seconds=5,
    )
    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["echo", "beta"],
        timeout_seconds=5,
    )

    raw = umbrella_tools.terminal_view(ctx, workspace_id=ws_id, last_lines=10)
    payload = json.loads(raw)
    assert "beta" in payload["scrollback"]


def test_terminal_reset_requires_reason(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.terminal_reset(ctx, workspace_id=ws_id, reason="")
    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert backend.reset_count == 0


def test_terminal_reset_with_reason_resets_session(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.terminal_reset(
        ctx,
        workspace_id=ws_id,
        reason="shell hung after long-running build",
    )
    payload = json.loads(raw)
    assert payload["status"] == "reset"
    assert backend.reset_count == 1


def test_run_workspace_command_unwraps_python_c_payload_quotes(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-c", "\"print('hello')\""],
        timeout_seconds=5,
    )

    assert backend.calls[0]["cmd"] == ["python", "-c", "print('hello')"]


def test_run_workspace_command_blocks_secret_file_reads(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["cmd", "/c", "type", "C:\\repo\\.env"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "secret_path_guard"
    assert backend.calls == []


def test_run_workspace_command_blocks_shell_file_mutations(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["powershell", "-Command", "Set-Content -Path pipeline.py -Value '# bad'"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_mutation_guard"
    assert backend.calls == []


def test_run_workspace_command_blocks_python_c_file_mutations(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-c", "open('pipeline.py', 'w').write('bad')"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_mutation_guard"
    assert backend.calls == []


def test_run_workspace_command_allows_python_c_read_only_open(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    (ws_dir / "pipeline.py").write_text("print('ok')\n", encoding="utf-8")

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-c", "print(open('pipeline.py', encoding='utf-8').read())"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["exit_code"] == 0
    assert backend.calls, "read-only python -c should execute, not hit mutation guard"


def test_update_workspace_seed_requires_explicit_gmas_context_before_first_write(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {"last_write_round": -1, "explicit_gmas_context_calls": 0}
    (ws_dir / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )

    raw = umbrella_tools.update_workspace_seed(
        ctx,
        workspace_id=ws_id,
        file_path="src/app.py",
        new_content="print('hi')\n",
        create_backup=False,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "gmas_context_before_first_write"


def test_apply_workspace_patch_requires_read_before_update(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    (ws_dir / "app.py").write_text("print('old')\n", encoding="utf-8")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            "-print('old')\n"
            "+print('new')\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "read_before_patch_required"
    assert (ws_dir / "app.py").read_text(encoding="utf-8") == "print('old')\n"


def test_apply_workspace_patch_updates_after_read(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    (ws_dir / "app.py").write_text("print('old')\n", encoding="utf-8")

    umbrella_tools.read_workspace_file(ctx, workspace_id=ws_id, file_path="app.py")
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            "-print('old')\n"
            "+print('new')\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (ws_dir / "app.py").read_text(encoding="utf-8") == "print('new')\n"
    assert ctx.loop_state_view["subtask_diff"]["app.py"]["lines_added"] == 0


def test_apply_workspace_patch_adds_new_file_without_read(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/new_module.py\n"
            "+VALUE = 1\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (ws_dir / "src" / "new_module.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert (
        ctx.loop_state_view["subtask_diff"]["src/new_module.py"]["added_file"] is True
    )


def test_workspace_toml_verification_guard_blocks_step_drop(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    (ws_dir / "workspace.toml").write_text(
        """
[[verification.steps]]
name = "smoke"
kind = "shell"
command = ["python", "-m", "pytest"]

[[verification.steps]]
name = "readme"
kind = "file_exists"
path = "README.md"
""",
        encoding="utf-8",
    )

    raw = umbrella_tools.update_workspace_seed(
        ctx,
        workspace_id=ws_id,
        file_path="workspace.toml",
        new_content=(
            "[[verification.steps]]\n"
            'name = "readme"\n'
            'kind = "file_exists"\n'
            'path = "README.md"\n'
        ),
        create_backup=False,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "verification_self_weakening_blocked"


def test_blocks_uv_run_python_main_when_workspace_declares_http_boot(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    (ws_dir / "workspace.toml").write_text(
        """
[[verification.steps]]
name = "boot"
kind = "http_boot"
command = ["python", "main.py"]
health_url = "http://127.0.0.1:8080/health"
""",
        encoding="utf-8",
    )

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["uv", "run", "python", "main.py"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "blocking_server_in_foreground"
    assert "http_boot" in payload["matched"] or "server entry" in payload["matched"]
    assert backend.calls == []


def test_blocks_python_script_with_uvicorn_run_in_source(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    (ws_dir / "main.py").write_text(
        "import uvicorn\n\nif __name__ == '__main__':\n    uvicorn.run('main:app')\n",
        encoding="utf-8",
    )

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "main.py"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "blocking_server_in_foreground"
    assert "contains server entrypoint" in payload["matched"]
    assert backend.calls == []


def test_blocks_python_main_for_interactive_launch_guard(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    (ws_dir / "main.py").write_text("print('game loop')\n", encoding="utf-8")

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "main.py"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "interactive_app_launch_guard"
    assert "entrypoint" in payload["matched"]
    assert backend.calls == []


def test_blocks_python_module_main_for_interactive_launch_guard(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-m", "main"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "interactive_app_launch_guard"
    assert "python -m" in payload["matched"]
    assert backend.calls == []


def test_allows_python_import_checks_for_fastapi_and_uvicorn(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-c", "import fastapi, uvicorn; print('ok')"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["exit_code"] == 0
    assert backend.calls, "import-check must execute, not be blocked by server guard"


def test_oneshot_backend_kills_process_on_timeout(tmp_path: Path) -> None:
    backend = OneShotBackend()

    result = backend.run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        timeout=1,
    )

    assert result.timed_out is True
    assert result.exit_code == 124
    assert "was killed" in result.output


def test_commit_workspace_changes_disabled_by_default(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace

    raw = umbrella_tools.commit_workspace_changes(
        ctx,
        workspace_id=ws_id,
        commit_message="test commit",
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "git_commit_disabled_by_policy"
