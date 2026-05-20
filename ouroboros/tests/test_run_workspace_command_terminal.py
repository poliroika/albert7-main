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

from ouroboros.tools import core, phase_control, umbrella_tools
from ouroboros.context import _filter_terminal_scrollback_for_task
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

    def repo_path(self, rel: str) -> Path:
        return (self.repo_dir / rel).resolve()


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


def _write_execute_phase_with_success_test(
    drive_root: Path,
    *,
    success_test: str = "pytest tests/test_game_engine.py::test_turn_processing -v",
) -> None:
    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_e3137dc0",
                "nodes": [
                    {"id": "preflight", "status": "done"},
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {"id": "1.1", "status": "done"},
                            {
                                "id": "1.2",
                                "status": "pending",
                                "title": "Implement game engine with turn mechanics",
                                "success_test": {"kind": "cmd", "value": success_test},
                            },
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_execute_phase_with_declared_subtasks(drive_root: Path) -> None:
    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_scope",
                "nodes": [
                    {"id": "preflight", "status": "done"},
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "setup-project",
                                "status": "pending",
                                "title": "Initialize project structure",
                                "files_to_create": [
                                    "pyproject.toml",
                                    "src/civilization_game/__init__.py",
                                    "src/civilization_game/config.py",
                                    "frontend/package.json",
                                    "frontend/vite.config.ts",
                                    "frontend/tsconfig.json",
                                    "tests/test_project_setup.py",
                                ],
                                "success_test": {
                                    "kind": "cmd",
                                    "value": (
                                        "python -m pytest "
                                        "tests/test_project_setup.py -q"
                                    ),
                                },
                            },
                            {
                                "id": "frontend-setup",
                                "status": "pending",
                                "title": "Set up React frontend",
                                "files_to_create": [
                                    "frontend/src/main.tsx",
                                    "frontend/src/App.tsx",
                                    "frontend/src/App.css",
                                ],
                                "success_test": {
                                    "kind": "cmd",
                                    "value": "cd frontend && npm run build",
                                },
                            },
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def _append_shell_result(
    drive_root: Path,
    *,
    ts: str,
    command: list[str],
    exit_code: int,
    output: str = "FAILED tests/test_game_engine.py::test_turn_processing",
) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "task_id": "phase_web_e3137dc0:execute",
                    "tool": "shell",
                    "args": {},
                    "result_preview": json.dumps(
                        {
                            "workspace_id": "demo",
                            "cwd": "workspaces/demo",
                            "command": command,
                            "exit_code": exit_code,
                            "output": output,
                        }
                    ),
                }
            )
            + "\n"
        )


def _append_gmas_context_result(
    drive_root: Path,
    *,
    ts: str,
    subtask_id: str,
    status: str = "ok",
) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "status": status,
        "active_subtask_id": subtask_id,
        "query": f"GMAS context for {subtask_id}",
    }
    if status == "ok":
        payload["recommended_pattern"] = "Use MACPRunner with AgentProfile tools"
    else:
        payload["error"] = "GMAS index unavailable"
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "task_id": "phase_web_e3137dc0:execute",
                    "tool": "get_gmas_context",
                    "args": {
                        "query": payload["query"],
                        "active_subtask_id": subtask_id,
                    },
                    "result_preview": json.dumps(payload),
                }
            )
            + "\n"
        )


def _append_tool_result(
    drive_root: Path,
    *,
    ts: str,
    tool: str,
    args: dict[str, Any] | None = None,
    result: dict[str, Any] | str | None = None,
) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    preview = result if isinstance(result, str) else json.dumps(result or {})
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "task_id": "phase_web_e3137dc0:execute",
                    "tool": tool,
                    "args": args or {},
                    "result_preview": preview,
                }
            )
            + "\n"
        )


def _append_blocked_success_test_command(
    drive_root: Path,
    *,
    ts: str,
    reason: str = "phase_subtask_repair_required_after_watcher",
) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "task_id": "phase_web_e3137dc0:execute",
                    "tool": "shell",
                    "args": {
                        "argv": [
                            "pytest",
                            "tests/test_game_engine.py::test_turn_processing",
                            "-v",
                        ],
                        "workspace_id": "demo",
                    },
                    "result_preview": json.dumps(
                        {
                            "status": "blocked",
                            "reason": reason,
                            "tool": "shell",
                            "subtask_id": "1.2",
                            "success_test": (
                                "pytest "
                                "tests/test_game_engine.py::test_turn_processing "
                                "-v"
                            ),
                            "failed_attempts": 3,
                            "threshold": 3,
                            "message": (
                                "The current execute subtask `1.2` already has "
                                "watcher review for repeated failures, but no "
                                "successful repair write has landed after that review."
                            ),
                            "next_step": (
                                "Apply one focused implementation repair with "
                                "`apply_workspace_patch` before rerunning the "
                                "declared success_test."
                            ),
                        }
                    ),
                }
            )
            + "\n"
        )


def _append_captured_python_pytest_shell_result(
    drive_root: Path,
    *,
    ts: str,
    exit_code: int = 2,
    output: str = "ERROR tests/test_agents.py",
) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    python_exe = r"C:\Users\poliroika\Documents\albert7\.venv\Scripts\python.exe"
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "task_id": "phase_web_e3137dc0:execute",
                    "tool": "shell",
                    "args": {
                        "argv": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_agents.py",
                            "-q",
                        ],
                        "command": "python",
                        "subdir": ".",
                        "workspace_id": "demo",
                    },
                    "result_preview": json.dumps(
                        {
                            "workspace_id": "demo",
                            "cwd": r"C:\Users\poliroika\Documents\albert7\workspaces\demo",
                            "command": [
                                python_exe,
                                "-m",
                                "pytest",
                                "tests/test_agents.py",
                                "-q",
                            ],
                            "exit_code": exit_code,
                            "output": output,
                            "backend": "oneshot",
                        }
                    ),
                }
            )
            + "\n"
        )


def _append_watcher_review(
    drive_root: Path,
    *,
    ts: str = "2026-05-18T09:25:00+00:00",
    reason: str = "Declared success test failed repeatedly on 1.2.",
    success_test: str = "pytest tests/test_game_engine.py::test_turn_processing -v",
    command: list[str] | None = None,
) -> None:
    if command is None:
        command = ["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"]
    _append_watcher_review_row(
        drive_root,
        ts=ts,
        result_preview=json.dumps(
            {
                "status": "review_recorded",
                "reviewer": "umbrella",
                "review_kind": "retry_watcher",
                "operator_reason": reason,
                "threshold": 3,
                "subtask_id": "1.2",
                "success_test": success_test,
                "failed_attempts": 3,
                "latest_failure": {
                    "tool": "shell",
                    "command": command,
                    "reason": "exit_code=1",
                    "output_excerpt": f"FAILED {success_test}",
                },
                "recommendation": (
                    "Apply one focused implementation repair based on the latest "
                    "declared success_test failure, then rerun that exact success_test."
                ),
            }
        ),
    )


def _append_legacy_watcher_review(
    drive_root: Path,
    *,
    ts: str = "2026-05-18T09:25:00+00:00",
    reason: str = "Declared success test failed repeatedly on 1.2.",
) -> None:
    _append_watcher_review_row(
        drive_root,
        ts=ts,
        result_preview=f"Watcher review requested: {reason} (signal: abc123)",
    )


def _append_watcher_review_row(
    drive_root: Path,
    *,
    ts: str,
    result_preview: str,
) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "task_id": "phase_web_e3137dc0:execute",
                    "tool": "request_watcher_review",
                    "args": {
                        "reason": "Declared success test failed repeatedly on 1.2."
                    },
                    "result_preview": result_preview,
                }
            )
            + "\n"
        )


def _append_watcher_signal(
    drive_root: Path,
    *,
    created_at: float = 2_000_000_000.0,
    reason: str = "Declared success test failed repeatedly on 1.2.",
) -> None:
    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    payload = {
        "reviewer": "umbrella",
        "review_kind": "retry_watcher",
        "operator_reason": reason,
        "threshold": 3,
        "status": "review_recorded",
        "subtask_id": "1.2",
        "success_test": "pytest tests/test_game_engine.py::test_turn_processing -v",
        "failed_attempts": 3,
        "latest_failure": {
            "tool": "shell",
            "command": ["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            "reason": "exit_code=1",
            "output_excerpt": "FAILED tests/test_game_engine.py::test_turn_processing",
        },
        "recommendation": (
            "Apply one focused implementation repair based on the latest "
            "declared success_test failure, then rerun that exact success_test."
        ),
    }
    with (state / "phase_control_signals.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "signal_id": "watcher-signal-1",
                    "created_at": created_at,
                    "kind": "request_watcher_review",
                    "payload": payload,
                    "actor": "worker",
                    "task_id": "phase_web_e3137dc0:execute",
                    "phase": "linear",
                }
            )
            + "\n"
        )


def _append_apply_workspace_patch(
    drive_root: Path,
    *,
    ts: str = "2026-05-18T09:26:00+00:00",
    file_path: str = "src/game_engine.py",
) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "task_id": "phase_web_e3137dc0:execute",
                    "tool": "apply_workspace_patch",
                    "args": {"file_path": file_path},
                    "result_preview": json.dumps(
                        {
                            "status": "applied",
                            "applied": True,
                            "file_path": file_path,
                        }
                    ),
                }
            )
            + "\n"
        )


def _phase_run_ctx(ctx: _FakeCtx) -> None:
    ctx.task_id = "phase_web_e3137dc0:execute"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {"phase_label": "execute"}


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


def test_run_workspace_command_blocks_git_rollback(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["git", "checkout", "HEAD", "--", "app.py"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_mutation_guard"
    assert "git checkout" in payload["hint"]
    assert backend.calls == []


def test_run_workspace_command_blocks_python_m_git_rollback(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    python_exe = str(ctx.host_repo_root / ".venv" / "Scripts" / "python.exe")

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=[python_exe, "-m", "git", "checkout", "HEAD", "--", "app.py"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_mutation_guard"
    assert "python -m git checkout" in payload["hint"]
    assert backend.calls == []


def test_run_workspace_command_blocks_direct_workspace_mkdir(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["mkdir", "-p", "src/demo"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_mutation_guard"
    assert "mkdir" in payload["hint"]
    assert backend.calls == []


def test_run_workspace_command_blocks_nonportable_file_probe(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["grep", "-n", "class GameState", "src/civ_game/game_model.py"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "nonportable_shell_probe_guard"
    assert "read_file" in payload["next_step"]
    assert backend.calls == []


def test_run_workspace_command_blocks_captured_argv_leading_dash(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=[
            "-c",
            (
                "import re; data = open('tests/test_game_core.py').read(); "
                "print('Found' if re.search('test_update_status', data) else 'no')"
            ),
        ],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "invalid_command_argv"
    assert "starts with an option" in payload["hint"]
    assert "python" in payload["hint"]
    assert backend.calls == []


def test_run_workspace_command_blocks_captured_bash_script_invocation(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["bash", "create.sh"],
        subdir="src",
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "nonportable_shell_interpreter_guard"
    assert "portable workspace command" in payload["hint"]
    assert "python -m pytest" in payload["hint"]
    assert backend.calls == []


def test_run_workspace_command_requires_watcher_after_repeated_success_test_failures(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    _write_execute_phase_with_success_test(ctx.drive_root)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T09:2{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
        )

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "phase_subtask_retry_escalation_required"
    assert payload["subtask_id"] == "1.2"
    assert payload["failed_attempts"] == 3
    assert "request_watcher_review" in payload["next_step"]
    assert backend.calls == []


def test_command_norms_include_captured_shell_args_argv() -> None:
    python_exe = r"C:\Users\poliroika\Documents\albert7\.venv\Scripts\python.exe"
    row = {
        "tool": "shell",
        "args": {
            "argv": ["python", "-m", "pytest", "tests/test_agents.py", "-q"],
            "command": "python",
            "subdir": ".",
            "workspace_id": "civilization",
        },
        "result_preview": json.dumps(
            {
                "command": [
                    python_exe,
                    "-m",
                    "pytest",
                    "tests/test_agents.py",
                    "-q",
                ],
                "exit_code": 2,
                "output": "ERROR tests/test_agents.py",
            }
        ),
    }

    norms = phase_control._tool_row_command_norms(row)

    assert "pythonmpytestteststestagentspyq" in norms


def test_run_workspace_command_counts_captured_python_argv_failures(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    _write_execute_phase_with_success_test(
        ctx.drive_root,
        success_test="python -m pytest tests/test_agents.py -q",
    )
    for idx in range(3):
        _append_captured_python_pytest_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T18:2{idx}:00+00:00",
        )

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-m", "pytest", "tests/test_agents.py", "-q"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "phase_subtask_retry_escalation_required"
    assert payload["subtask_id"] == "1.2"
    assert payload["success_test"] == "python -m pytest tests/test_agents.py -q"
    assert payload["failed_attempts"] == 3
    assert backend.calls == []


def test_run_workspace_command_counts_partial_pytest_failures_from_combined_success_test(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    success_test = (
        "python -m pytest tests/test_models.py tests/test_credentials.py "
        "tests/test_validation.py -v"
    )
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    python_exe = r"C:\Users\poliroika\Documents\albert7\.venv\Scripts\python.exe"
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-19T04:3{idx}:00+00:00",
            command=[
                python_exe,
                "-m",
                "pytest",
                "tests/test_credentials.py",
                "-v",
            ],
            exit_code=1,
            output=(
                "tests/test_credentials.py::TestCredentialInfo::"
                "test_info_with_fallback_to_llm_credentials FAILED\n"
                "E AssertionError: assert 'fallback_api_key...' == "
                "'fallback_api_key_123...'"
            ),
        )

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-m", "pytest", "tests/test_credentials.py", "-v"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "phase_subtask_retry_escalation_required"
    assert payload["success_test"] == success_test
    assert payload["failed_attempts"] == 3
    assert "request_watcher_review" in payload["next_step"]
    assert backend.calls == []


def test_mark_subtask_complete_does_not_accept_partial_pytest_success_from_combined_success_test(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = (
        "python -m pytest tests/test_models.py tests/test_credentials.py "
        "tests/test_validation.py -v"
    )
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-19T04:36:01+00:00",
        command=["python", "-m", "pytest", "tests/test_credentials.py", "-v"],
        exit_code=0,
        output="11 passed in 0.07s",
    )

    result = phase_control._mark_subtask_complete(
        ctx,
        subtask_id="1.2",
        summary="Partial pytest subset passed.",
        evidence=["python -m pytest tests/test_credentials.py -v passed"],
    )

    assert result.startswith("ERROR:")
    assert "declares success_test" in result
    assert "no matching successful shell/run_workspace_command evidence" in result


def test_mark_subtask_complete_rejects_stale_shell_success_after_repair_write(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = "python -m pytest tests/test_game_engine.py -q"
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-19T19:00:00+00:00",
        command=["python", "-m", "pytest", "tests/test_game_engine.py", "-q"],
        exit_code=0,
        output="7 passed in 0.12s",
    )
    _append_tool_result(
        ctx.drive_root,
        ts="2026-05-19T19:01:00+00:00",
        tool="apply_workspace_patch",
        result={"status": "applied", "applied": True, "files_changed": ["src/app.py"]},
    )

    result = phase_control._mark_subtask_complete(
        ctx,
        subtask_id="1.2",
        summary="Implemented game engine.",
        evidence=["pytest passed before final patch"],
    )

    assert result.startswith("ERROR:")
    assert "workspace files were modified after" in result
    assert "Rerun the declared success_test" in result


def test_mark_subtask_complete_rejects_failed_required_tool_success_test(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = "run_real_e2e"
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    _append_tool_result(
        ctx.drive_root,
        ts="2026-05-19T19:02:00+00:00",
        tool="run_real_e2e",
        args={"subtask_id": "1.2"},
        result={"passed": False, "failed_step_count": 1, "summary": "e2e failed"},
    )

    result = phase_control._mark_subtask_complete(
        ctx,
        subtask_id="1.2",
        summary="E2E attempted.",
        evidence=["run_real_e2e failed"],
    )

    assert result.startswith("ERROR:")
    assert "latest `run_real_e2e` result is not passing" in result


def test_request_watcher_review_records_structured_retry_review_from_failures(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(ctx.drive_root)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T09:2{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
            output="FAILED tests/test_game_engine.py::test_turn_processing",
        )

    out = phase_control._request_watcher_review(
        ctx,
        reason=(
            "Subtask design_domain_models has 3 failed test runs. "
            "Current test results: 27 passed, 4 failed."
        ),
    )

    payload = json.loads(out)
    assert payload["status"] == "review_recorded"
    assert payload["reviewer"] == "umbrella"
    assert payload["review_kind"] == "retry_watcher"
    assert payload["subtask_id"] == "1.2"
    assert payload["success_test"] == "pytest tests/test_game_engine.py::test_turn_processing -v"
    assert payload["failed_attempts"] == 3
    assert payload["latest_failure"]["reason"] == "exit_code=1"
    ledger = (ctx.drive_root / "state" / "phase_control_signals.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"reviewer": "umbrella"' in ledger
    assert '"review_kind": "retry_watcher"' in ledger


def test_request_watcher_review_classifies_bad_generated_success_test_contract(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = "python -m pytest tests/test_config.py -q"
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    command = ["python", "-m", "pytest", "tests/test_config.py", "-q"]
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-20T07:36:3{idx}+00:00",
            command=command,
            exit_code=1,
            output=(
                "FAILED tests/test_config.py::TestConfigResolution::"
                "test_model_alias_not_ourobotus_llm_model - ValueError: "
                "Missing LLM configuration. Set environment variables: "
                "OUROBOROS_LLM_API_KEY or LLM_API_KEY"
            ),
        )

    out = phase_control._request_watcher_review(
        ctx,
        reason=(
            "backend-setup subtask test failure: "
            "test_model_alias_not_ourobotus_llm_model fails because it calls "
            "get_llm_config() with validate=True but only sets model env vars, "
            "not api_key. Test needs validation disabled (validate=False) "
            "since it only verifies model alias priority, not full config. "
            "Proposed fix: change line 101 to use validate=False."
        ),
    )

    payload = json.loads(out)
    assert payload["status"] == "review_recorded"
    assert payload["contract_migration"]["verdict"] == (
        "bad_generated_success_test_contract"
    )
    assert payload["contract_migration"]["target_files"] == ["tests/test_config.py"]
    assert "mutate_phase_plan" in payload["recommendation"]
    assert "contract_migration_files" in payload["recommendation"]


def test_retry_state_counts_recorded_watcher_review_before_threshold(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(ctx.drive_root)
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-20T04:29:53+00:00",
        command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
        exit_code=1,
        output="FAILED tests/test_game_engine.py::test_turn_processing",
    )
    _append_watcher_review_row(
        ctx.drive_root,
        ts="2026-05-20T04:30:05+00:00",
        result_preview=json.dumps(
            {
                "status": "review_recorded",
                "reviewer": "umbrella",
                "review_kind": "retry_watcher",
                "operator_reason": (
                    "Watcher recorded a concrete failure diagnosis before "
                    "the escalation threshold."
                ),
                "threshold": 3,
                "subtask_id": "1.2",
                "success_test": (
                    "pytest tests/test_game_engine.py::test_turn_processing -v"
                ),
                "failed_attempts": 1,
                "latest_failure": {
                    "tool": "shell",
                    "command": [
                        "pytest",
                        "tests/test_game_engine.py::test_turn_processing",
                        "-v",
                    ],
                    "reason": "exit_code=1",
                    "output_excerpt": (
                        "FAILED tests/test_game_engine.py::test_turn_processing"
                    ),
                },
            }
        ),
    )

    state = phase_control._phase_subtask_retry_state(ctx)

    assert state is not None
    assert state["failures"] == 1
    assert state["watcher_reviews"] == 1


def test_request_watcher_review_prefers_declared_success_test_failure_evidence(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(
        ctx.drive_root,
        success_test="python -m pytest tests/test_game_state.py -q",
    )
    python_exe = r"C:\Users\poliroika\Documents\albert7\.venv\Scripts\python.exe"
    for idx in range(2):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-19T18:1{idx}:00+00:00",
            command=[
                python_exe,
                "-m",
                "pytest",
                "tests/test_game_state.py",
                "-q",
            ],
            exit_code=1,
            output="FAILED tests/test_game_state.py::TestGameState::test_next_turn",
        )
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-19T18:12:17+00:00",
        command=[
            python_exe,
            "-m",
            "pytest",
            "tests/test_game_state.py::TestCityBuildingType::test_add_building",
            "-v",
        ],
        exit_code=4,
        output=(
            "ERROR: not found: tests/test_game_state.py::"
            "TestCityBuildingType::test_add_building"
        ),
    )

    out = phase_control._request_watcher_review(
        ctx,
        reason="Captured retry watcher should diagnose the declared success test.",
    )

    payload = json.loads(out)
    assert payload["status"] == "review_recorded"
    assert payload["failed_attempts"] == 3
    command = payload["latest_failure"]["command"]
    assert command[-2:] == ["tests/test_game_state.py", "-q"]
    assert "TestCityBuildingType::test_add_building" not in " ".join(command)


def test_request_watcher_review_escalates_after_repeated_schema_mismatches(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(ctx.drive_root)
    failures = [
        "TypeError: 'war_turns' is an invalid keyword argument for DiplomaticStateModel",
        "TypeError: 'alliance_turns' is an invalid keyword argument for DiplomaticStateModel",
        "TypeError: 'turn' is an invalid keyword argument for TradeProposalModel",
        "TypeError: 'proposal_id' is an invalid keyword argument for TradeProposalModel",
        "TypeError: 'player_a_id' is an invalid keyword argument for TradeProposalModel",
    ]
    for idx, failure in enumerate(failures):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T09:2{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
            output=f"FAILED tests/test_repository.py::test_create_game - {failure}",
        )
    _append_watcher_review(
        ctx.drive_root,
        ts="2026-05-18T09:25:00+00:00",
        reason=(
            "Subtask arch-2 has 5 failed test runs. Every attempt reveals "
            "another missing ORM field; need comprehensive schema audit vs "
            "repository before next patch."
        ),
    )

    out = phase_control._request_watcher_review(
        ctx,
        reason=(
            "DEEP ARCHITECTURAL ISSUE: after fixing one field, the next run "
            "reveals another TradeProposalModel keyword mismatch."
        ),
    )

    payload = json.loads(out)
    assert payload["status"] == "review_recorded"
    assert payload["failed_attempts"] == 5
    assert payload["prior_watcher_reviews"] == 1
    assert "Stop chasing only the latest single error" in payload["recommendation"]
    assert "schema/API/field contract" in payload["recommendation"]


def test_request_watcher_review_reports_patch_escape_guidance_before_threshold(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(
        ctx.drive_root,
        success_test="python -m pytest tests/test_game_state.py -q",
    )
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-19T20:56:00+00:00",
        command=["python", "-m", "pytest", "tests/test_game_state.py", "-q"],
        exit_code=1,
        output=(
            "FAILED tests/test_game_state.py::TestGameState::test_contract\n"
            "tests/test_game_state.py:12: IndexError"
        ),
    )
    _append_tool_result(
        ctx.drive_root,
        ts="2026-05-19T20:57:36+00:00",
        tool="apply_workspace_patch",
        args={
            "workspace_id": ws_id,
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: tests/test_game_state.py\n"
                "@@\n"
                "        bot_city = bot_cities[0]\\r\n"
                "-        assert bot_city.owner_id == \"bot_1\"\\r\n"
                "+        assert bot_city.owner_id == \"bot_1\"\\r\n"
                "*** End Patch"
            ),
        },
        result={
            "status": "blocked",
            "reason": "patch_hunk_mismatch",
            "file_path": "tests/test_game_state.py",
            "escaped_line_endings_detected": True,
            "read_file_hint": (
                'read_file(file_path="tests/test_game_state.py", '
                "line_start=4, line_count=32)"
            ),
        },
    )

    out = phase_control._request_watcher_review(
        ctx,
        reason="Captured patch mismatch copied literal JSON CRLF escapes.",
    )

    payload = json.loads(out)
    assert payload["status"] == "review_not_required"
    assert payload["failed_attempts"] == 1
    assert "patch-mismatch repair signal" in payload["message"]
    assert "literal `\\r` or `\\n`" in payload["recommendation"]
    assert "line_start=4" in payload["recommendation"]


def test_request_watcher_review_records_first_explicit_declared_failure(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(
        ctx.drive_root,
        success_test="python -m pytest tests/test_project_structure.py -q",
    )
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-20T00:39:01+00:00",
        command=["python", "-m", "pytest", "tests/test_project_structure.py", "-q"],
        exit_code=1,
        output=(
            "FAILED tests/test_project_structure.py::"
            "test_no_unconditional_success_paths"
        ),
    )

    out = phase_control._request_watcher_review(
        ctx,
        reason=(
            "Declared project-setup success test failed because the generated "
            "test scans itself for forbidden_patterns."
        ),
    )

    payload = json.loads(out)
    assert payload["status"] == "review_recorded"
    assert payload["failed_attempts"] == 1
    assert payload["latest_failure"]["reason"] == "exit_code=1"
    assert payload["subtask_id"] == "1.2"


def test_mark_subtask_complete_normalizes_string_evidence_before_success_gate(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(
        ctx.drive_root,
        success_test="python -m pytest tests/test_project_structure.py -q",
    )
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-20T00:39:01+00:00",
        command=["python", "-m", "pytest", "tests/test_project_structure.py", "-q"],
        exit_code=1,
        output="3 failed, 5 passed",
    )

    result = phase_control._mark_subtask_complete(
        ctx,
        subtask_id="1.2",
        summary="Project setup complete.",
        evidence="python -m pytest tests/test_project_structure.py -q failed",
    )

    assert result.startswith("ERROR:")
    assert "no matching successful shell/run_workspace_command evidence" in result


def test_run_workspace_command_requires_repair_after_watcher_review_signal(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    _write_execute_phase_with_success_test(ctx.drive_root)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T09:2{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
        )
    _append_watcher_review(ctx.drive_root)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "phase_subtask_repair_required_after_watcher"
    assert payload["subtask_id"] == "1.2"
    assert "apply_workspace_patch" in payload["next_step"]
    assert "repo_write_commit" not in payload["next_step"]
    assert backend.calls == []


def test_run_workspace_command_requests_contract_audit_after_repeated_watcher_cycle(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    _write_execute_phase_with_success_test(ctx.drive_root)
    for idx, failure in enumerate(
        [
            "TypeError: 'war_turns' is an invalid keyword argument",
            "TypeError: 'alliance_turns' is an invalid keyword argument",
            "TypeError: 'turn' is an invalid keyword argument",
            "TypeError: 'proposal_id' is an invalid keyword argument",
            "TypeError: 'player_a_id' is an invalid keyword argument",
        ]
    ):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T09:2{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
            output=f"FAILED tests/test_repository.py::test_create_game - {failure}",
        )
    _append_watcher_review(ctx.drive_root, ts="2026-05-18T09:25:00+00:00")

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "phase_subtask_repair_required_after_watcher"
    assert payload["failed_attempts"] == 5
    assert payload["prior_watcher_reviews"] == 1
    assert "schema/API/field contract" in payload["next_step"]
    assert backend.calls == []


def test_run_workspace_command_rejects_legacy_self_signed_watcher_review(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    _write_execute_phase_with_success_test(ctx.drive_root)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T09:2{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
        )
    _append_legacy_watcher_review(ctx.drive_root)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "phase_subtask_retry_escalation_required"
    assert "Umbrella watcher review record" in payload["message"]
    assert backend.calls == []


def test_retry_gate_uses_signal_ledger_when_watcher_tool_preview_is_truncated(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    _write_execute_phase_with_success_test(ctx.drive_root)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T09:2{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
        )
    _append_watcher_review_row(
        ctx.drive_root,
        ts="2026-05-18T09:25:00+00:00",
        result_preview='{"status":"review_recorded","latest_failure":{"output_excerpt":"...',
    )
    _append_watcher_signal(ctx.drive_root)

    repair_block = phase_control._phase_subtask_retry_escalation_block(
        ctx,
        tool_name="apply_workspace_patch",
    )
    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert repair_block is None
    assert payload["status"] == "blocked"
    assert payload["reason"] == "phase_subtask_repair_required_after_watcher"
    assert backend.calls == []


def test_apply_workspace_patch_allows_repair_after_watcher_blocked_rerun(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    _write_execute_phase_with_success_test(ctx.drive_root)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T22:3{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
            output=(
                "VERIFICATION FAILED: Missing fallback documentation: "
                "Fallback behavior"
            ),
        )
    _append_watcher_review(ctx.drive_root, ts="2026-05-18T22:34:41+00:00")
    _append_blocked_success_test_command(
        ctx.drive_root,
        ts="2026-05-18T22:35:30+00:00",
    )

    blocked_rerun = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
        timeout_seconds=5,
    )
    rerun_payload = json.loads(blocked_rerun)
    assert rerun_payload["reason"] == "phase_subtask_repair_required_after_watcher"

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: docs/repair.md\n"
            "+repair after watcher\n"
            "*** End Patch\n"
        ),
        validation_summary=(
            "Reduced from phase_web_aee7109f: watcher review recorded, "
            "then a blocked success-test rerun must not make repair writes "
            "ask for another watcher."
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "applied"
    assert payload["applied"] == ["added docs/repair.md"]
    assert (ws_dir / "docs" / "repair.md").read_text(encoding="utf-8") == (
        "repair after watcher\n"
    )
    assert backend.calls == []


def test_apply_workspace_patch_allows_repair_after_post_watcher_failure(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    success_test = "pytest tests/test_api_server.py -v"
    command = ["pytest", "tests/test_api_server.py", "-v"]
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T22:55:2{idx}+00:00",
            command=command,
            exit_code=1,
            output="FAILED tests/test_api_server.py::test_add_bot_player",
        )
    _append_watcher_review(
        ctx.drive_root,
        ts="2026-05-18T22:55:28+00:00",
        reason="Captured api_server_websocket watcher review from phase_web_08824b47.",
        success_test=success_test,
        command=command,
    )
    _append_apply_workspace_patch(
        ctx.drive_root,
        ts="2026-05-18T22:55:31+00:00",
        file_path="src/civilization/api/server.py",
    )
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-18T22:55:37+00:00",
        command=command,
        exit_code=1,
        output=(
            "FAILED tests/test_api_server.py::test_add_bot_player - "
            "assert 400 == 200"
        ),
    )

    repair_block = phase_control._phase_subtask_retry_escalation_block(
        ctx,
        tool_name="apply_workspace_patch",
    )
    rerun = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=command,
        timeout_seconds=5,
    )
    rerun_payload = json.loads(rerun)
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: docs/api-repair.md\n"
            "+post-watcher repair window\n"
            "*** End Patch\n"
        ),
        validation_summary=(
            "Reduced from phase_web_08824b47: watcher review plus one "
            "successful repair should allow the next focused repair after a "
            "new failed test, instead of requiring another watcher immediately."
        ),
    )

    payload = json.loads(raw)
    assert repair_block is None
    assert rerun_payload["status"] == "blocked"
    assert rerun_payload["reason"] == "phase_subtask_repair_required_after_watcher"
    assert payload["status"] == "applied"
    assert (ws_dir / "docs" / "api-repair.md").read_text(encoding="utf-8") == (
        "post-watcher repair window\n"
    )
    assert backend.calls == []


def test_retry_gate_requests_second_watcher_after_post_watcher_failure_window(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = "pytest tests/test_api_server.py -v"
    command = ["pytest", "tests/test_api_server.py", "-v"]
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T22:55:2{idx}+00:00",
            command=command,
            exit_code=1,
            output="FAILED tests/test_api_server.py::test_add_bot_player",
        )
    _append_watcher_review(
        ctx.drive_root,
        ts="2026-05-18T22:55:28+00:00",
        reason="Captured api_server_websocket watcher review from phase_web_08824b47.",
        success_test=success_test,
        command=command,
    )
    for idx in range(3):
        _append_apply_workspace_patch(
            ctx.drive_root,
            ts=f"2026-05-18T22:55:{31 + (idx * 6):02d}+00:00",
            file_path="src/civilization/api/server.py",
        )
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T22:55:{37 + (idx * 6):02d}+00:00",
            command=command,
            exit_code=1,
            output="FAILED tests/test_api_server.py::test_add_bot_player",
        )

    retry_block = phase_control._phase_subtask_retry_escalation_block(
        ctx,
        tool_name="apply_workspace_patch",
    )

    assert retry_block is not None
    assert retry_block["reason"] == "phase_subtask_retry_escalation_required"
    assert retry_block["post_watcher_failed_attempts"] == 3
    assert "request_watcher_review" in retry_block["next_step"]


def test_run_workspace_command_allows_retry_after_watcher_review_and_repair_write(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    backend = _install_recording_session(ctx, ws_id)
    _write_execute_phase_with_success_test(ctx.drive_root)
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-18T09:2{idx}:00+00:00",
            command=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
            exit_code=1,
        )
    _append_watcher_review(ctx.drive_root)
    _append_apply_workspace_patch(ctx.drive_root)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["exit_code"] == 0
    assert backend.calls


def test_run_workspace_command_blocks_cat_file_probe(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["cat", "tests/test_turn_manager.py"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "nonportable_shell_probe_guard"
    assert "read_file" in payload["next_step"]
    assert backend.calls == []


def test_run_workspace_command_blocks_combined_pytest_node_and_flag(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=[
            "python",
            "-m",
            "pytest",
            "tests/test_turn_manager.py::TestTurnManager::test_unit_creation -v",
            "--tb=short",
        ],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "invalid_pytest_argv"
    assert "separate argv elements" in payload["hint"]
    assert backend.calls == []


def test_run_workspace_command_allows_env_loader_module_name(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=[
            "python",
            "-c",
            "from civilization.env_loader import get_creds; print('ok')",
        ],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["exit_code"] == 0
    assert backend.calls


def test_run_workspace_command_allows_credential_named_test_node(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=[
            "python",
            "-m",
            "pytest",
            "tests/test_env_loader.py::test_credential_import",
            "-v",
        ],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["exit_code"] == 0
    assert backend.calls


def test_run_workspace_command_blocks_dotenv_path_read(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-c", "print(open('.env').read())"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "secret_path_guard"
    assert ".env" in payload["hint"]
    assert backend.calls == []


def test_read_workspace_file_supports_offset(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    (ws_dir / "long.txt").write_text("0123456789abcdef", encoding="utf-8")

    out = umbrella_tools.read_workspace_file(
        ctx,
        workspace_id=ws_id,
        file_path="long.txt",
        max_chars=5,
        offset=10,
    )

    payload = json.loads(out)
    assert payload["offset"] == 10
    assert payload["content"] == "abcdef"
    assert payload["truncated"] is False


def test_read_workspace_file_supports_line_start(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    (ws_dir / "long.py").write_text(
        "line1\nline2\nline3\nline4\n",
        encoding="utf-8",
    )

    out = umbrella_tools.read_workspace_file(
        ctx,
        workspace_id=ws_id,
        file_path="long.py",
        line_start=2,
        line_count=2,
    )

    payload = json.loads(out)
    assert payload["line_start"] == 2
    assert payload["line_count"] == 2
    assert payload["line_end"] == 3
    assert payload["total_lines"] == 4
    assert payload["content"].replace("\r\n", "\n") == "line2\nline3\n"
    assert payload["truncated"] is False
    assert payload["line_range_complete"] is True
    assert payload["has_more_lines_after"] is True


def test_read_workspace_file_full_text_reports_line_metadata(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    (ws_dir / "TASK_MAIN.md").write_text(
        "first line\nsecond line\nthird line\n",
        encoding="utf-8",
    )

    out = umbrella_tools.read_workspace_file(
        ctx,
        workspace_id=ws_id,
        file_path="TASK_MAIN.md",
        max_chars=30000,
    )

    payload = json.loads(out)
    assert payload["line_start"] == 0
    assert payload["line_count"] == 3
    assert payload["line_end"] == 3
    assert payload["total_lines"] == 3
    assert payload["line_range_complete"] is True
    assert payload["has_more_lines_after"] is False


def test_run_workspace_command_strips_workspace_prefix_from_subdir(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    (ws_dir / "frontend").mkdir()

    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["echo", "ok"],
        subdir=f"workspaces/{ws_id}/frontend",
        timeout_seconds=5,
    )

    assert backend.calls[0]["cwd"] == str(ws_dir / "frontend")


def test_run_workspace_command_folds_cd_argv_into_workspace_cwd(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    (ws_dir / "frontend").mkdir()

    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["cd", f"workspaces/{ws_id}/frontend", "&&", "echo", "ok"],
        timeout_seconds=5,
    )

    assert backend.calls[0]["cmd"] == ["echo", "ok"]
    assert backend.calls[0]["cwd"] == str(ws_dir / "frontend")


def test_run_workspace_command_wraps_compound_cd_argv(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)
    (ws_dir / "frontend").mkdir()

    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=[
            "cd",
            f"workspaces/{ws_id}/frontend",
            "&&",
            "npm",
            "install",
            "&&",
            "npm",
            "run",
            "build",
        ],
        timeout_seconds=5,
        allow_dependency_install=True,
    )

    assert backend.calls[0]["cwd"] == str(ws_dir / "frontend")
    assert "npm install && npm run build" in backend.calls[0]["cmd"][-1]
    assert backend.calls[0]["cmd"][0] in {"cmd", "bash"}


def test_run_workspace_command_strips_posix_timeout_wrapper(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["timeout", "30", "echo", "ok"],
        timeout_seconds=5,
    )

    assert backend.calls[0]["cmd"] == ["echo", "ok"]


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


def test_run_workspace_command_blocks_phase_task_after_run_cancel(workspace) -> None:
    ctx, _ws_dir, ws_id = workspace
    ctx.task_id = "run_stop:execute"
    backend = _install_recording_session(ctx, ws_id)
    state_dir = ctx.drive_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "stop_requested.json").write_text(
        json.dumps({"run_id": "run_stop", "reason": "cancel"}),
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
    ctx.task_id = "phase_web_abc:execute"
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
    assert "task=phase_web_abc:execute" in text
    assert "run=phase_web_abc" in text
    assert "echo scrollback_probe" in text
    assert "scrollback_probe" in text


def test_recent_terminal_filters_to_current_run() -> None:
    raw = "\n".join(
        [
            "## ws=mini task=phase_web_old:execute run=phase_web_old ts=old exit=1 backend=a",
            "old failure",
            "## ws=mini task=phase_web_new:execute run=phase_web_new ts=new exit=0 backend=b",
            "new success",
        ]
    )

    filtered = _filter_terminal_scrollback_for_task(raw, "phase_web_new:verify")

    assert "new success" in filtered
    assert "old failure" not in filtered


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
    assert "apply_workspace_patch" in payload["next_step"]
    assert "update_workspace_seed" not in payload["hint"]
    assert "update_workspace_seed" not in payload["next_step"]
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
    assert "apply_workspace_patch" in payload["next_step"]
    assert "update_workspace_seed" not in payload["hint"]
    assert "update_workspace_seed" not in payload["next_step"]
    assert backend.calls == []


def test_run_workspace_command_blocks_captured_absolute_python_c_file_mutation(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    captured_code = (
        "content = open('tests/test_game_mechanics.py', 'r').read(); "
        "updated = content.replace('mechanics = GameMechanics()', "
        "'mechanics = GameMechanics.create()'); "
        "open('tests/test_game_mechanics.py', 'w').write(updated); "
        "print('Fixed test calls using regex replacements')"
    )

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=[
            r"C:\Users\poliroika\Documents\albert7\.venv\Scripts\python.exe",
            "-c",
            captured_code,
        ],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_mutation_guard"
    assert "python -c file mutation is blocked" in payload["hint"]
    assert "apply_workspace_patch" in payload["next_step"]
    assert backend.calls == []


def test_run_workspace_command_blocks_split_python_c_mutation_fragments(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=[
            "python",
            "-c",
            "from pathlib import Path;",
            "content = Path('docs/architecture.md').read_text(encoding='utf-8');",
            "Path('docs/architecture.md').write_text(content, encoding='utf-8');",
            "print('Fixed model alias phrase')",
        ],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_mutation_guard"
    assert "python -c file mutation is blocked" in payload["hint"]
    assert backend.calls == []


def test_run_workspace_command_rejects_broken_json_argv_string(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        command='["python", "-c", "with open(\'pipeline.py\', \'w\') as f:\n    f.write(\'bad\')"]',
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["status"] == "invalid_command"
    assert "JSON argv array" in payload["hint"]
    assert backend.calls == []
    assert not (ws_dir / "pipeline.py").exists()


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


def test_gmas_context_gate_skips_setup_dependency_leaf(workspace) -> None:
    from umbrella.deep_agent_tools.workspace_gmas import _gmas_context_before_write_block

    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    (ws_dir / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )
    state = ctx.drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_ce127a9e",
                "nodes": [
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "project-setup",
                                "status": "pending",
                                "title": "Initialize project structure and dependencies",
                                "goal": (
                                    "Create workspace layout with Python backend "
                                    "(src/civgame/...) and React+TSX frontend "
                                    "(frontend/src/...), configure dependencies, "
                                    "implement entrypoint files. Verify import "
                                    "infrastructure and basic build capability."
                                ),
                                "files_to_create": [
                                    "src/civgame/__init__.py",
                                    "src/civgame/game/__init__.py",
                                    "src/civgame/api/__init__.py",
                                    "src/civgame/engine/__init__.py",
                                    "src/civgame/ai/__init__.py",
                                    "pyproject.toml",
                                    "requirements.txt",
                                    "frontend/package.json",
                                    "frontend/vite.config.ts",
                                    "frontend/tsconfig.json",
                                    "frontend/index.html",
                                    "frontend/src/main.tsx",
                                    "frontend/src/App.tsx",
                                    "frontend/src/vite-env.d.ts",
                                    "tests/test_project_structure.py",
                                    "README.md",
                                    "docs/architecture.md",
                                ],
                                "success_test": {
                                    "kind": "cmd",
                                    "value": "python -m pytest tests/test_project_structure.py -q",
                                },
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert _gmas_context_before_write_block(ctx, ws_id, ws_dir) is None


def test_apply_workspace_patch_requires_fresh_gmas_context_for_active_subtask(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    (ws_dir / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )
    state = ctx.drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_e3137dc0",
                "nodes": [
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {"id": "project-setup", "status": "done"},
                            {
                                "id": "gmas-integration",
                                "status": "pending",
                                "title": "Integrate GMAS framework",
                                "goal": "Build LLM-backed civilization agent graph.",
                                "success_test": {
                                    "kind": "cmd",
                                    "value": "python -m pytest tests/test_gmas.py -q",
                                },
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _append_gmas_context_result(
        ctx.drive_root,
        ts="2026-05-19T18:04:58+00:00",
        subtask_id="project-setup",
    )
    _append_gmas_context_result(
        ctx.drive_root,
        ts="2026-05-19T18:05:58+00:00",
        subtask_id="gmas-integration",
        status="error",
    )

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/civilization/bots/context_marker.py\n"
            "+from dataclasses import dataclass\n"
            "+\n"
            "+@dataclass\n"
            "+class AgentGraphSpec:\n"
            "+    name: str\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "gmas_context_before_first_write"
    assert payload["active_subtask_id"] == "gmas-integration"
    assert not (ws_dir / "src" / "civilization" / "bots" / "context_marker.py").exists()

    _append_gmas_context_result(
        ctx.drive_root,
        ts="2026-05-19T18:06:58+00:00",
        subtask_id="gmas-integration",
    )
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/civilization/bots/context_marker.py\n"
            "+from dataclasses import dataclass\n"
            "+\n"
            "+@dataclass\n"
            "+class AgentGraphSpec:\n"
            "+    name: str\n"
            "*** End Patch\n"
        ),
    )
    payload = json.loads(raw)
    assert payload["status"] == "applied"


def test_get_gmas_context_accepts_limit_alias(workspace, monkeypatch) -> None:
    ctx, _ws_dir, _ws_id = workspace
    captured: dict[str, Any] = {}

    def fake_build_gmas_context(
        repo_root: Path,
        query: str,
        *,
        max_results: int,
        max_chars_per_hit: int,
    ) -> dict[str, Any]:
        captured["repo_root"] = repo_root
        captured["query"] = query
        captured["max_results"] = max_results
        captured["max_chars_per_hit"] = max_chars_per_hit
        return {"status": "ok", "recommended_pattern": "Use MACPRunner."}

    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        fake_build_gmas_context,
    )

    raw = umbrella_tools.get_gmas_context(
        ctx,
        query="agent graph",
        limit=2,
        max_chars_per_hit=1234,
    )

    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert captured["max_results"] == 2
    assert captured["max_chars_per_hit"] == 1234


def test_search_gmas_knowledge_accepts_intent_metadata_from_capture(
    workspace, monkeypatch
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    captured: dict[str, Any] = {}

    def fake_build_gmas_context(
        repo_root: Path,
        query: str,
        *,
        max_results: int,
        max_chars_per_hit: int,
    ) -> dict[str, Any]:
        captured["repo_root"] = repo_root
        captured["query"] = query
        captured["max_results"] = max_results
        captured["max_chars_per_hit"] = max_chars_per_hit
        return {"status": "ok", "recommended_pattern": "Use GraphBuilder."}

    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        fake_build_gmas_context,
    )

    raw = umbrella_tools.search_gmas_knowledge(
        ctx,
        query="multi-agent game AI construction tools LLM agent graph",
        max_results=3,
        intent="planner_research",
    )

    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert payload["intent"] == "planner_research"
    assert captured["query"] == (
        "multi-agent game AI construction tools LLM agent graph"
    )
    assert captured["max_results"] == 3


def test_get_gmas_context_accepts_slug_metadata_from_capture(
    workspace, monkeypatch
) -> None:
    ctx, _ws_dir, _ws_id = workspace

    def fake_build_gmas_context(
        repo_root: Path,
        query: str,
        *,
        max_results: int,
        max_chars_per_hit: int,
    ) -> dict[str, Any]:
        return {"status": "ok", "recommended_pattern": "Use MACPRunner."}

    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        fake_build_gmas_context,
    )

    raw = umbrella_tools.get_gmas_context(
        ctx,
        query="GraphBuilder MACPRunner agent profile persona tools",
        max_results=5,
        slug="gmas-overview",
    )

    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert payload["slug"] == "gmas-overview"


def test_gmas_context_tool_schema_accepts_limit_intent_and_slug() -> None:
    tools = {entry.name: entry for entry in umbrella_tools.get_tools()}
    for tool_name in ("get_gmas_context", "search_gmas_knowledge"):
        props = tools[tool_name].schema["parameters"]["properties"]
        assert "limit" in props
        assert "max_results" in props
        assert "alias for max_results" in props["limit"]["description"]
        assert "intent" in props
        assert "audit metadata" in props["intent"]["description"]
        assert "slug" in props
        assert "audit metadata label" in props["slug"]["description"]


def test_update_workspace_seed_blocks_accidental_source_truncation(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    target = ws_dir / "src" / "civsim" / "models.py"
    target.parent.mkdir(parents=True)
    old_content = (
        '"""Models."""\n\n'
        + "\n\n".join(
            f"class Model{i}:\n    def method_{i}(self):\n        return {i}\n"
            for i in range(60)
        )
    )
    target.write_text(old_content, encoding="utf-8")

    raw = umbrella_tools.update_workspace_seed(
        ctx,
        workspace_id=ws_id,
        file_path="src/civsim/models.py",
        new_content='"""Models."""\n\nimport copy\n',
        create_backup=False,
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "source_truncation_guard"
    assert "Model59" in target.read_text(encoding="utf-8")


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


def test_apply_workspace_patch_rejects_python_read_text_without_encoding(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: tests/verify_docs.py\n"
            "+from pathlib import Path\n"
            "+\n"
            "+def test_architecture_doc():\n"
            "+    content = Path('docs/architecture.md').read_text()\n"
            "+    assert 'Architecture' in content\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "python_text_read_encoding_required"
    assert not (ws_dir / "tests" / "verify_docs.py").exists()


def test_apply_workspace_patch_allows_python_read_text_with_utf8_encoding(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: tests/verify_docs.py\n"
            "+from pathlib import Path\n"
            "+\n"
            "+def test_architecture_doc():\n"
            "+    content = Path('docs/architecture.md').read_text(encoding='utf-8')\n"
            "+    assert 'Architecture' in content\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "applied"
    assert (ws_dir / "tests" / "verify_docs.py").exists()


def test_apply_workspace_patch_blocks_future_subtask_file_before_current_complete(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_declared_subtasks(ctx.drive_root)

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: frontend/src/App.tsx\n"
            "+export default function App() {\n"
            "+  return <main>Civilization</main>\n"
            "+}\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "active_subtask_write_scope"
    assert payload["active_subtask_id"] == "setup-project"
    assert payload["blocked_paths"] == ["frontend/src/App.tsx"]
    assert payload["future_subtask_owners"] == {
        "frontend/src/App.tsx": "frontend-setup"
    }
    assert "mutate_phase_plan" in payload["next_step"]
    assert not (ws_dir / "frontend" / "src" / "App.tsx").exists()


def test_apply_workspace_patch_allows_active_subtask_declared_file(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_declared_subtasks(ctx.drive_root)

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: pyproject.toml\n"
            "+[project]\n"
            '+name = "civilization-game"\n'
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "applied"
    assert (ws_dir / "pyproject.toml").exists()


def test_apply_workspace_patch_blocks_import_repair_that_keeps_missing_symbol(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    package = ws_dir / "src" / "civilization"
    package.mkdir(parents=True)
    (package / "map.py").write_text(
        "class GameMap:\n"
        "    pass\n\n"
        "class HexPosition:\n"
        "    pass\n\n"
        "class Tile:\n"
        "    pass\n\n"
        "class TerrainType:\n"
        "    pass\n",
        encoding="utf-8",
    )
    target = package / "game_state.py"
    target.write_text(
        "from .map import GameMap as Map, BorderPos, Tile, TerrainType as TileType\n\n"
        "class GameState:\n"
        "    pass\n",
        encoding="utf-8",
    )

    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="src/civilization/game_state.py"
    )
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: src/civilization/game_state.py\n"
            "@@\n"
            "from .map import GameMap as Map, BorderPos, Tile, TerrainType as TileType\n"
            "+from .map import GameMap as Map, HexPosition, Tile, TerrainType as TileType\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "python_missing_local_import_symbol"
    assert payload["imported_name"] == "BorderPos"
    assert target.read_text(encoding="utf-8").count("from .map import") == 1

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: src/civilization/game_state.py\n"
            "@@\n"
            "-from .map import GameMap as Map, BorderPos, Tile, TerrainType as TileType\n"
            "+from .map import GameMap as Map, HexPosition, Tile, TerrainType as TileType\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    content = target.read_text(encoding="utf-8")
    assert "BorderPos" not in content
    assert content.count("from .map import") == 1


def test_apply_workspace_patch_same_path_replacement_requires_read(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    (ws_dir / "app.py").write_text("print('old')\n", encoding="utf-8")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: app.py\n"
            "*** Add File: app.py\n"
            "+print('new')\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "read_before_patch_required"
    assert (ws_dir / "app.py").read_text(encoding="utf-8") == "print('old')\n"


def test_apply_workspace_patch_same_path_replacement_after_read(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    (ws_dir / "app.py").write_text("print('old')\n", encoding="utf-8")

    umbrella_tools.read_workspace_file(ctx, workspace_id=ws_id, file_path="app.py")
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: app.py\n"
            "*** Add File: app.py\n"
            "+print('new')\n"
            "*** End Patch\n"
        ),
        validation_summary="Replacing the full file after repeated patch hunk mismatches.",
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert payload["applied"] == ["updated app.py"]
    assert (ws_dir / "app.py").read_text(encoding="utf-8") == "print('new')\n"
    assert ctx.loop_state_view["subtask_diff"]["app.py"]["deleted_file"] is False


def test_apply_workspace_patch_same_path_replacement_after_captured_mismatch_loop(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    logs = ctx.drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    captured_rows = [
        {
            "ts": "2026-05-18T22:15:49.408377+00:00",
            "task_id": "phase_web_31524e1d:execute",
            "tool": "apply_workspace_patch",
            "result_preview": json.dumps(
                {
                    "status": "blocked",
                    "reason": "patch_hunk_mismatch",
                    "file_path": "tests/test_setup.py",
                    "error": "failed to match patch hunk in tests/test_setup.py",
                    "next_step": "Re-read the file and emit a patch with exact current context.",
                }
            ),
        },
        {
            "ts": "2026-05-18T22:15:51.252577+00:00",
            "task_id": "phase_web_31524e1d:execute",
            "tool": "shell",
            "result_preview": json.dumps(
                {
                    "status": "blocked",
                    "reason": "workspace_mutation_guard",
                    "command": [
                        "python",
                        "-c",
                        "pathlib.Path('tests/test_setup.py').write_text(content)",
                    ],
                    "hint": "python -c file mutation is blocked; use apply_workspace_patch for edits",
                }
            ),
        },
        {
            "ts": "2026-05-18T22:16:05.551112+00:00",
            "task_id": "phase_web_31524e1d:execute",
            "tool": "apply_workspace_patch",
            "result_preview": json.dumps(
                {
                    "status": "applied",
                    "workspace_id": "civilization",
                    "applied": ["added tests/test_setup.py.new"],
                }
            ),
        },
    ]
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as fh:
        for row in captured_rows:
            fh.write(json.dumps(row) + "\n")
    tests = ws_dir / "tests"
    tests.mkdir()
    (tests / "test_setup.py").write_text(
        "def test_project_structure():\n    assert False\n",
        encoding="utf-8",
    )

    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="tests/test_setup.py"
    )
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: tests/test_setup.py\n"
            "*** Add File: tests/test_setup.py\n"
            "+def test_project_structure():\n"
            "+    assert True\n"
            "*** End Patch\n"
        ),
        validation_summary="Captured hunk-mismatch loop requires same-path replacement.",
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (tests / "test_setup.py").read_text(encoding="utf-8") == (
        "def test_project_structure():\n    assert True\n"
    )
    assert not (tests / "test_setup.py.new").exists()


def test_apply_workspace_patch_requires_replacement_after_repeated_hunk_mismatches(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    ctx.task_id = "phase_web_27d086d3:execute"
    logs = ctx.drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    captured_rows = [
        {
            "ts": "2026-05-19T02:44:31.191329+00:00",
            "task_id": "phase_web_27d086d3:execute",
            "tool": "apply_workspace_patch",
            "result_preview": json.dumps(
                {
                    "status": "blocked",
                    "reason": "patch_hunk_mismatch",
                    "file_path": "tests/test_agents.py",
                    "error": "failed to match patch hunk in tests/test_agents.py",
                    "next_step": (
                        "Re-read the file and retry once with a smaller exact hunk."
                    ),
                }
            ),
        },
        {
            "ts": "2026-05-19T02:44:34.808730+00:00",
            "task_id": "phase_web_27d086d3:execute",
            "tool": "apply_workspace_patch",
            "result_preview": json.dumps(
                {
                    "status": "blocked",
                    "reason": "patch_hunk_mismatch",
                    "file_path": "tests/test_agents.py",
                    "error": "failed to match patch hunk in tests/test_agents.py",
                    "next_step": (
                        "Use one apply_workspace_patch with paired same-path "
                        "Delete/Add entries for an audited replacement."
                    ),
                }
            ),
        },
    ]
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as fh:
        for row in captured_rows:
            fh.write(json.dumps(row) + "\n")

    tests = ws_dir / "tests"
    tests.mkdir()
    target = tests / "test_agents.py"
    target.write_text(
        "def test_agent_contract():\n    assert False\n",
        encoding="utf-8",
    )

    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="tests/test_agents.py"
    )
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_agents.py\n"
            "@@\n"
            "-def test_agent_contract():\n"
            "-    assert False\n"
            "+def test_agent_contract():\n"
            "+    assert True\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "patch_hunk_mismatch_replacement_required"
    assert payload["file_path"] == "tests/test_agents.py"
    assert "paired" in payload["next_step"]
    assert payload["forbidden_next_write"] == "*** Update File: tests/test_agents.py"
    assert "*** Delete File: tests/test_agents.py" in payload["required_patch_shape"]
    assert target.read_text(encoding="utf-8") == (
        "def test_agent_contract():\n    assert False\n"
    )

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: tests/test_agents.py\n"
            "*** Add File: tests/test_agents.py\n"
            "+def test_agent_contract():\n"
            "+    assert True\n"
            "*** End Patch\n"
        ),
        validation_summary="Captured repeated hunk mismatch requires audited replacement.",
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert target.read_text(encoding="utf-8") == (
        "def test_agent_contract():\n    assert True\n"
    )


def test_apply_workspace_patch_parse_error_explains_prefixed_end_marker(
    workspace,
) -> None:
    ctx, _ws_dir, ws_id = workspace

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: README.md\n"
            "+hello\n"
            "+*** End Patch"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "patch_parse_error"
    assert payload["end_marker_prefixed"] is True
    assert "no leading `+`" in payload["next_step"]
    assert "file content lines" in payload["next_step"]


def test_apply_workspace_patch_blocks_active_success_test_edit_after_repeated_failure(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(
        ctx.drive_root,
        success_test="pytest tests/test_state.py -v --exitfirst",
    )
    command = ["pytest", "tests/test_state.py", "-v", "--exitfirst"]
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-19T06:14:1{idx}+00:00",
            command=command,
            exit_code=1,
            output=(
                "FAILED tests/test_state.py::TestWorld::test_game_state_summary "
                "- AttributeError: 'World' object has no attribute "
                "'get_state_summary'"
            ),
        )
    _append_watcher_review(
        ctx.drive_root,
        ts="2026-05-19T06:15:37+00:00",
        reason=(
            "Captured phase_web_f463e26e: repeated state-model failures; "
            "repair implementation instead of changing tests/test_state.py."
        ),
        success_test="pytest tests/test_state.py -v --exitfirst",
        command=command,
    )
    tests_dir = ws_dir / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_state.py"
    original = (
        "class TestWorld:\n"
        "    def test_game_state_summary(self):\n"
        "        world = World()\n"
        "        summary = world.get_state_summary()\n"
        "        assert \"current_turn\" in summary\n"
    )
    target.write_text(original, encoding="utf-8")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_state.py\n"
            "@@\n"
            "-        summary = world.get_state_summary()\n"
            "+        summary = world.to_dict()\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "active_success_test_edit_after_failure"
    assert payload["file_path"] == "tests/test_state.py"
    assert "Repair the implementation" in payload["next_step"]
    assert target.read_text(encoding="utf-8") == original


def test_apply_workspace_patch_blocks_active_success_test_edit_after_single_failure(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_success_test(
        ctx.drive_root,
        success_test="pytest tests/test_models.py -v --tb=short",
    )
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-19T06:35:32+00:00",
        command=["pytest", "tests/test_models.py", "-v", "--tb=short"],
        exit_code=1,
        output=(
            "FAILED tests/test_models.py::TestGameLogic::test_move_unit_out_of_range "
            "- AssertionError: error text did not match"
        ),
    )
    tests_dir = ws_dir / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_models.py"
    original = (
        "class TestGameLogic:\n"
        "    def test_move_unit_out_of_range(self):\n"
        "        result = logic.move_unit(unit.id, 10, 10)\n"
        "        assert not result[\"success\"]\n"
        "        assert \"out of range\" in result[\"error\"].lower()\n"
    )
    target.write_text(original, encoding="utf-8")
    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="tests/test_models.py"
    )

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_models.py\n"
            "@@\n"
            "-        assert \"out of range\" in result[\"error\"].lower()\n"
            "+        error_lower = result[\"error\"].lower()\n"
            "+        assert \"range\" in error_lower or \"position\" in error_lower\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "active_success_test_edit_after_failure"
    assert payload["failed_attempts"] == 1
    assert "mutate_phase_plan" in payload["next_step"]
    assert "contract_migration_reason" in payload["next_step"]
    assert target.read_text(encoding="utf-8") == original


def test_apply_workspace_patch_allows_generated_test_contract_migration_after_plan_mutation(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = "pytest tests/test_game_model.py -v -x"
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-19T07:44:03+00:00",
        command=["python", "-m", "pytest", "tests/test_game_model.py", "-v", "-x"],
        exit_code=1,
        output=(
            "FAILED tests/test_game_model.py::TestGameState::test_respond_to_trade_accept "
            "- AssertionError: assert 60 == 70"
        ),
    )
    tests_dir = ws_dir / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_game_model.py"
    original = (
        "class TestGameState:\n"
        "    def test_respond_to_trade_accept(self):\n"
        "        player2_gold = 50 + 30 - 20\n"
        "        assert player2_gold == 70\n"
    )
    target.write_text(original, encoding="utf-8")
    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="tests/test_game_model.py"
    )

    blocked_raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_game_model.py\n"
            "@@\n"
            "-        assert player2_gold == 70\n"
            "+        assert player2_gold == 60\n"
            "*** End Patch\n"
        ),
    )
    assert json.loads(blocked_raw)["reason"] == "active_success_test_edit_after_failure"

    mutation = phase_control._mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "1.2",
                    "contract_migration_reason": (
                        "Captured generated test contradiction: player2 starts "
                        "with 50, receives 30, pays 20, so the behavioral "
                        "assertion must be 60 rather than 70."
                    ),
                    "contract_migration_files": ["tests/test_game_model.py"],
                }
            ]
        },
    )
    assert mutation.startswith("PhasePlan mutated")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_game_model.py\n"
            "@@\n"
            "-        assert player2_gold == 70\n"
            "+        assert player2_gold == 60\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert target.read_text(encoding="utf-8") == (
        "class TestGameState:\n"
        "    def test_respond_to_trade_accept(self):\n"
        "        player2_gold = 50 + 30 - 20\n"
        "        assert player2_gold == 60\n"
    )


def test_mutate_phase_plan_accepts_watcher_proven_bad_generated_success_test_contract(
    workspace,
) -> None:
    ctx, _ws_dir, _ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = "python -m pytest tests/test_config.py -q"
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    command = ["python", "-m", "pytest", "tests/test_config.py", "-q"]
    for idx in range(3):
        _append_shell_result(
            ctx.drive_root,
            ts=f"2026-05-20T07:37:0{idx}+00:00",
            command=command,
            exit_code=1,
            output=(
                "FAILED tests/test_config.py::TestConfigResolution::"
                "test_model_alias_not_ourobotus_llm_model - ValueError: "
                "Missing LLM configuration. Set environment variables: "
                "OUROBOROS_LLM_API_KEY or LLM_API_KEY"
            ),
        )
    phase_control._request_watcher_review(
        ctx,
        reason=(
            "test_model_alias_not_ourobotus_llm_model fails because it calls "
            "get_llm_config() with validate=True but only sets model env vars, "
            "not api_key. Test needs validation disabled because it only "
            "verifies model alias priority."
        ),
    )

    mutation = phase_control._mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "1.2",
                    "contract_migration_reason": (
                        "Migrate the generated alias-priority test while "
                        "preserving LLM alias coverage."
                    ),
                    "contract_migration_files": ["tests/test_config.py"],
                }
            ]
        },
    )

    assert mutation.startswith("PhasePlan mutated")
    plan = json.loads(
        (ctx.drive_root / "state" / "phase_plan.json").read_text(encoding="utf-8")
    )
    subtask = plan["nodes"][1]["subtasks"][1]
    assert subtask["contract_migration_files"] == ["tests/test_config.py"]


def test_apply_workspace_patch_contract_migration_allows_exact_update_after_repeated_hunk_mismatches(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = "python -m pytest tests/test_simulation.py -v"
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-19T08:08:19+00:00",
        command=["python", "-m", "pytest", "tests/test_simulation.py", "-v"],
        exit_code=1,
        output=(
            "FAILED tests/test_simulation.py::TestGameState::"
            "test_diplomatic_relation - AttributeError: CHELLY"
        ),
    )
    logs = ctx.drive_root / "logs"
    captured_rows = [
        {
            "ts": "2026-05-19T08:09:16+00:00",
            "task_id": "phase_web_e3137dc0:execute",
            "tool": "apply_workspace_patch",
            "result_preview": json.dumps(
                {
                    "status": "blocked",
                    "reason": "patch_hunk_mismatch",
                    "file_path": "tests/test_simulation.py",
                    "error": (
                        "failed to match patch hunk in tests/test_simulation.py"
                    ),
                }
            ),
        },
        {
            "ts": "2026-05-19T08:10:16+00:00",
            "task_id": "phase_web_e3137dc0:execute",
            "tool": "apply_workspace_patch",
            "result_preview": json.dumps(
                {
                    "status": "blocked",
                    "reason": "patch_hunk_mismatch",
                    "file_path": "tests/test_simulation.py",
                    "error": (
                        "failed to match patch hunk in tests/test_simulation.py"
                    ),
                }
            ),
        },
    ]
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as fh:
        for row in captured_rows:
            fh.write(json.dumps(row) + "\n")

    tests_dir = ws_dir / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_simulation.py"
    original = (
        "class TestGameState:\n"
        "    def test_diplomatic_relation(self, game_state):\n"
        "        game_state.add_player(Player(player_id=\"p1\", name=\"P1\"))\n"
        "        game_state.add_player(Player(player_id=\"p2\", name=\"P2\"))\n"
        "        relation = DiplomaticRelation(\n"
        "            player_1_id=\"p1\",\n"
        "            player_2_id=\"p2\",\n"
        "            status=DiplomaticStatus.CHELLY,\n"
        "        )\n"
    )
    target.write_text(original, encoding="utf-8")
    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="tests/test_simulation.py"
    )

    mutation = phase_control._mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "1.2",
                    "contract_migration_reason": (
                        "Captured generated test typo from phase_web_eb6b24c7: "
                        "DiplomaticStatus.CHELLY should be CHILLY."
                    ),
                    "contract_migration_files": ["tests/test_simulation.py"],
                }
            ]
        },
    )
    assert mutation.startswith("PhasePlan mutated")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_simulation.py\n"
            "@@\n"
            "         relation = DiplomaticRelation(\n"
            "             player_1_id=\"p1\",\n"
            "             player_2_id=\"p2\",\n"
            "-            status=DiplomaticStatus.CHELLY,\n"
            "+            status=DiplomaticStatus.CHILLY,\n"
            "         )\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert "CHILLY" in target.read_text(encoding="utf-8")
    assert "CHELLY" not in target.read_text(encoding="utf-8")


def test_apply_workspace_patch_contract_migration_mismatch_explains_json_escapes(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    success_test = "python -m pytest tests/test_game_state.py -q"
    _write_execute_phase_with_success_test(ctx.drive_root, success_test=success_test)
    _append_shell_result(
        ctx.drive_root,
        ts="2026-05-19T20:56:00+00:00",
        command=["python", "-m", "pytest", "tests/test_game_state.py", "-q"],
        exit_code=1,
        output=(
            "FAILED tests/test_game_state.py::TestGameState::test_contract\n"
            "tests/test_game_state.py:4: IndexError: list index out of range"
        ),
    )
    tests_dir = ws_dir / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_game_state.py"
    original = (
        "class TestGameState:\n"
        "    def test_contract(self):\n"
        "        bot_cities = []\n"
        "        bot_city = bot_cities[0]\n"
        "        assert bot_city.owner_id == \"bot_1\"\n"
    )
    target.write_text(original, encoding="utf-8")
    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="tests/test_game_state.py"
    )
    mutation = phase_control._mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "1.2",
                    "contract_migration_reason": (
                        "Captured generated test contradiction: the test indexes "
                        "an empty city list instead of constructing the bot city "
                        "state required by the intended ownership assertion."
                    ),
                    "contract_migration_files": ["tests/test_game_state.py"],
                }
            ]
        },
    )
    assert mutation.startswith("PhasePlan mutated")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_game_state.py\n"
            "@@\n"
            "        bot_cities = []\\r\n"
            "-        bot_city = bot_cities[0]\\r\n"
            "+        bot_city = make_bot_city()\\r\n"
            "        assert bot_city.owner_id == \"bot_1\"\\r\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "patch_hunk_mismatch"
    assert payload["escaped_line_endings_detected"] is True
    assert "JSON-rendered line endings" in payload["next_step"]
    assert "`\\r` or `\\n`" in payload["next_step"]
    assert "line_range_complete=true" in payload["next_step"]
    assert payload["read_file_hint"] == (
        'read_file(file_path="tests/test_game_state.py", line_start=1, '
        "line_count=32)"
    )
    assert any(item["line"] == 4 for item in payload["current_context"])
    assert target.read_text(encoding="utf-8") == original


def test_apply_workspace_patch_same_path_replacement_allows_protected_readme(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    (ws_dir / "README.md").write_text("# Old\n", encoding="utf-8")

    umbrella_tools.read_workspace_file(ctx, workspace_id=ws_id, file_path="README.md")
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: README.md\n"
            "*** Add File: README.md\n"
            "+# New\n"
            "+\n"
            "+Updated setup notes.\n"
            "*** End Patch\n"
        ),
        validation_summary="Full README replacement with preserved project contract.",
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (ws_dir / "README.md").read_text(encoding="utf-8") == (
        "# New\n\nUpdated setup notes.\n"
    )


def test_apply_workspace_patch_blocks_new_sidecar_replacement_artifact(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    tests = ws_dir / "tests"
    tests.mkdir()
    (tests / "test_setup.py").write_text("def test_old():\n    assert True\n", encoding="utf-8")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: tests/test_setup.py.new\n"
            "+def test_new():\n"
            "+    assert True\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "replacement_artifact_blocked"
    assert payload["existing_file"] == "tests/test_setup.py"
    assert "paired" in payload["next_step"]
    assert not (tests / "test_setup.py.new").exists()


def test_apply_workspace_patch_blocks_corrected_sidecar_replacement_artifact(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    tests = ws_dir / "tests"
    tests.mkdir()
    (tests / "test_setup.py").write_text("def test_old():\n    assert True\n", encoding="utf-8")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: tests/test_setup_corrected.py\n"
            "+def test_new():\n"
            "+    assert True\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "replacement_artifact_blocked"
    assert payload["existing_file"] == "tests/test_setup.py"
    assert not (tests / "test_setup_corrected.py").exists()


def test_apply_workspace_patch_blocks_extra_sidecar_after_replacement_required(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    ctx.task_id = "phase_web_9ae3673a:execute"
    logs = ctx.drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    captured_replacement_required = {
        "ts": "2026-05-19T07:02:53.155707+00:00",
        "task_id": "phase_web_9ae3673a:execute",
        "tool": "apply_workspace_patch",
        "result_preview": json.dumps(
            {
                "status": "blocked",
                "reason": "patch_hunk_mismatch_replacement_required",
                "file_path": "src/game/models.py",
                "recent_mismatches": 2,
                "message": (
                    "This task has already hit repeated Update hunk mismatches "
                    "for this file since the last successful patch."
                ),
                "required_patch_shape": (
                    "*** Begin Patch\n"
                    "*** Delete File: src/game/models.py\n"
                    "*** Add File: src/game/models.py\n"
                    "+<full replacement file content, every line prefixed with +>\n"
                    "*** End Patch"
                ),
                "forbidden_next_write": "*** Update File: src/game/models.py",
            }
        ),
    }
    (logs / "tools.jsonl").write_text(
        json.dumps(captured_replacement_required) + "\n",
        encoding="utf-8",
    )
    src_game = ws_dir / "src" / "game"
    src_game.mkdir(parents=True)
    (src_game / "models.py").write_text(
        "class AIAction:\n    pass\n",
        encoding="utf-8",
    )

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/game/models_extra.py\n"
            "+class AIAction:\n"
            "+    pass\n"
            "*** End Patch\n"
        ),
        validation_summary="Captured phase_web_9ae3673a attempted auxiliary model file.",
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "replacement_required_sidecar_blocked"
    assert payload["file_path"] == "src/game/models_extra.py"
    assert payload["existing_file"] == "src/game/models.py"
    assert "*** Delete File: src/game/models.py" in payload["required_patch_shape"]
    assert not (src_game / "models_extra.py").exists()


def test_apply_workspace_patch_allows_extra_named_file_without_replacement_required(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    src_game = ws_dir / "src" / "game"
    src_game.mkdir(parents=True)
    (src_game / "models.py").write_text("class Model:\n    pass\n", encoding="utf-8")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/game/models_extra.py\n"
            "+EXTRA_MODEL_COUNT = 1\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (src_game / "models_extra.py").read_text(encoding="utf-8") == (
        "EXTRA_MODEL_COUNT = 1\n"
    )


def test_apply_workspace_patch_hunk_mismatch_points_to_same_path_replacement(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    (ws_dir / "tests").mkdir()
    (ws_dir / "tests" / "test_setup.py").write_text(
        "def test_project_structure():\n    assert True\n",
        encoding="utf-8",
    )

    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="tests/test_setup.py"
    )
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_setup.py\n"
            "@@\n"
            "-def test_project_structure():\n"
            "-    assert (project_root / \"frontend\").exists(), \"frontend directory missing\"\n"
            "+def test_project_structure():\n"
            "+    assert True\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "patch_hunk_mismatch"
    assert "paired same-path" in payload["next_step"]
    assert ".new" in payload["next_step"]


def test_repo_read_workspace_file_satisfies_patch_preread(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    ctx.active_workspace_id = ws_id
    (ws_dir / "app.py").write_text("value = 'old'\n", encoding="utf-8")

    content = core._repo_read(ctx, f"workspaces/{ws_id}/app.py")
    assert "old" in content

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            "-value = 'old'\n"
            "+value = 'new'\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (ws_dir / "app.py").read_text(encoding="utf-8") == "value = 'new'\n"
    assert ctx.loop_state_view["files_read"][ws_id] == ["app.py"]


def test_repo_read_supports_offset_and_max_chars(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    ctx.active_workspace_id = ws_id
    (ws_dir / "long.txt").write_text("0123456789abcdef", encoding="utf-8")

    content = core._repo_read(
        ctx,
        f"workspaces/{ws_id}/long.txt",
        offset=10,
        max_chars=3,
    )

    assert content == "abc"
    assert ctx.loop_state_view["files_read"][ws_id] == ["long.txt"]


def test_repo_read_supports_line_start(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    ctx.active_workspace_id = ws_id
    (ws_dir / "long.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

    content = core._repo_read(
        ctx,
        f"workspaces/{ws_id}/long.txt",
        line_start=2,
        line_count=1,
    )

    assert content == "line2\n"
    assert ctx.loop_state_view["files_read"][ws_id] == ["long.txt"]


def test_apply_workspace_patch_tolerates_extra_blank_context_lines(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    (ws_dir / "app.py").write_text(
        "from typing import Any\n"
        "from gmas.tools import tool\n"
        "\n"
        "\n"
        "@tool\n"
        "def run() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )

    umbrella_tools.read_workspace_file(ctx, workspace_id=ws_id, file_path="app.py")
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            " from typing import Any\n"
            " from gmas.tools import tool\n"
            " \n"
            "+__all__ = ['run']\n"
            "+\n"
            " @tool\n"
            " def run() -> None:\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert "__all__ = ['run']" in (ws_dir / "app.py").read_text(encoding="utf-8")


def test_apply_workspace_patch_blocks_test_contract_weakening(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    test_file = ws_dir / "tests" / "test_api.py"
    test_file.parent.mkdir(parents=True)
    original = (
        "def test_one():\n"
        "    result = {'type': 'success'}\n"
        "    assert result['type'] == 'success'\n"
        "def test_two():\n"
        "    result = {'type': 'success'}\n"
        "    assert result['type'] == 'success'\n"
        "def test_three():\n"
        "    result = {'type': 'success'}\n"
        "    assert result['type'] == 'success'\n"
    )
    test_file.write_text(original, encoding="utf-8")

    umbrella_tools.read_workspace_file(ctx, workspace_id=ws_id, file_path="tests/test_api.py")
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_api.py\n"
            "@@\n"
            "-    result = {'type': 'success'}\n"
            "-    assert result['type'] == 'success'\n"
            "+    result = 'success'\n"
            "+    assert isinstance(result, str)\n"
            "@@\n"
            "-    result = {'type': 'success'}\n"
            "-    assert result['type'] == 'success'\n"
            "+    result = 'success'\n"
            "+    assert isinstance(result, str)\n"
            "@@\n"
            "-    result = {'type': 'success'}\n"
            "-    assert result['type'] == 'success'\n"
            "+    result = 'success'\n"
            "+    assert isinstance(result, str)\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "test_weakening_guard"
    assert test_file.read_text(encoding="utf-8") == original


def test_apply_workspace_patch_adds_new_file_without_read(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/demo/new_module.py\n"
            "+VALUE = 1\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (ws_dir / "src" / "demo" / "new_module.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert (
        ctx.loop_state_view["subtask_diff"]["src/demo/new_module.py"]["added_file"]
        is True
    )


def test_apply_workspace_patch_blocks_empty_non_init_python_file(workspace) -> None:
    ctx, ws_dir, ws_id = workspace

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/demo/gmas_bridge.py\n"
            "*** End Patch"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "empty_workspace_file"
    assert not (ws_dir / "src" / "demo" / "gmas_bridge.py").exists()


def test_apply_workspace_patch_allows_empty_init_file(workspace) -> None:
    ctx, ws_dir, ws_id = workspace

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/demo/__init__.py\n"
            "*** End Patch"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (ws_dir / "src" / "demo" / "__init__.py").read_text(
        encoding="utf-8"
    ) == ""


def test_apply_workspace_patch_blocks_placeholder_gmas_bridge(workspace) -> None:
    ctx, ws_dir, ws_id = workspace

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/demo/ai/gmas_bridge.py\n"
            "+\"\"\"GMAS integration bridge - placeholder for "
            "skill_compatibility check. GMAS will be fully integrated in "
            "subtask st_005.\"\"\"\n"
            "+import gmas\n"
            "*** End Patch"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "placeholder_integration_bridge"
    assert not (ws_dir / "src" / "demo" / "ai" / "gmas_bridge.py").exists()


def test_apply_workspace_patch_blocks_compliance_only_gmas_import(workspace) -> None:
    ctx, ws_dir, ws_id = workspace

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/demo/agents/tools.py\n"
            "+\"\"\"Game interaction tools for AI agents.\"\"\"\n"
            "+# Import gmas to satisfy the GMAS skill requirement\n"
            "+from gmas.builder import GraphBuilder\n"
            "+from gmas.execution import MACPRunner\n"
            "+__all__ = [\"GraphBuilder\", \"MACPRunner\"]\n"
            "*** End Patch"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "placeholder_integration_bridge"
    assert not (ws_dir / "src" / "demo" / "agents" / "tools.py").exists()


def test_apply_workspace_patch_blocks_quoted_python_source_lines(workspace) -> None:
    ctx, ws_dir, ws_id = workspace

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/demo/models/player.py\n"
            "+\"from dataclasses import dataclass\"\n"
            "+\"\"\n"
            "+\"@dataclass\"\n"
            "+\"class Player:\"\n"
            "+\"    id: str\"\n"
            "+\"    name: str\"\n"
            "+\"\"\n"
            "+\"    def label(self) -> str:\"\n"
            "+\"        return self.name\"\n"
            "*** End Patch"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "quoted_source_lines"
    assert not (ws_dir / "src" / "demo" / "models" / "player.py").exists()


def test_apply_workspace_patch_blocks_hardcoded_llm_runtime_defaults(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: game_bots/agents.py\n"
            "+import os\n"
            "+\n"
            "+def get_llm_caller():\n"
            "+    base_url = os.environ.get('OUROBOROS_LLM_BASE_URL') or os.environ.get('LLM_BASE_URL') or 'https://api.openai.com/v1'\n"
            "+    model = os.environ.get('OUROBOROS_MODEL') or os.environ.get('LLM_MODEL') or 'gpt-4o-mini'\n"
            "+    return base_url, model\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "llm_runtime_contract"
    assert "https://api.openai.com/v1" in payload["issues"][0]
    assert "OUROBOROS_LLM_MODEL" not in payload["next_step"]
    assert not (ws_dir / "game_bots" / "agents.py").exists()


def test_apply_workspace_patch_allows_protective_llm_runtime_docs(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: docs/architecture.md\n"
            "+# Runtime Contract\n"
            "+Resolve LLM runtime through OUROBOROS_LLM_API_KEY/LLM_API_KEY, "
            "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL, and OUROBOROS_MODEL/LLM_MODEL.\n"
            "+Forbidden provider defaults:\n"
            "+- Hardcoded https://api.openai.com/v1\n"
            "+- Hardcoded gpt-* model names\n"
            "+- Requiring OPENAI_API_KEY as the universal credential\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "applied"
    assert (ws_dir / "docs" / "architecture.md").is_file()


def test_apply_workspace_patch_blocks_hardcoded_llm_runtime_env_defaults(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: .env.local\n"
            "+LLM_API_KEY=your-key-here\n"
            "+LLM_BASE_URL=https://api.openai.com/v1\n"
            "+LLM_MODEL=gpt-4\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "llm_runtime_contract"
    assert "https://api.openai.com/v1" in payload["issues"][0]
    assert not (ws_dir / ".env.local").exists()


def test_apply_workspace_patch_blocks_unsupported_ouroboros_model_alias(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: README.md\n"
            "+# LLM setup\n"
            "+Set OUROBOROS_LLM_MODEL and LLM_MODEL before starting GMAS bots.\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "llm_runtime_contract"
    assert "OUROBOROS_LLM_MODEL" in payload["issues"][0]
    assert not (ws_dir / "README.md").exists()


def test_apply_workspace_patch_blocks_protective_unsupported_model_alias_docs(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: docs/llm_runtime_contract.md\n"
            "+# LLM runtime contract\n"
            "+Use OUROBOROS_MODEL/LLM_MODEL for model selection.\n"
            "+Do not use OUROBOROS_LLM_MODEL; that alias is unsupported.\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "llm_runtime_contract"
    assert "OUROBOROS_LLM_MODEL" in payload["issues"][0]
    assert not (ws_dir / "docs" / "llm_runtime_contract.md").exists()


def test_apply_workspace_patch_blocks_obsolete_ouroboros_alias_only_env(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: .env.example\n"
            "+LLM_API_KEY=\n"
            "+LLM_BASE_URL=\n"
            "+LLM_MODEL=\n"
            "+OUROBOROS_API_KEY=${LLM_API_KEY}\n"
            "+OUROBOROS_BASE_URL=${LLM_BASE_URL}\n"
            "+OUROBOROS_MODEL=${LLM_MODEL}\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "llm_runtime_contract"
    assert "OUROBOROS_LLM_API_KEY" in " ".join(payload["issues"])
    assert "OUROBOROS_LLM_BASE_URL" in " ".join(payload["issues"])
    assert not (ws_dir / ".env.example").exists()


def test_apply_workspace_patch_blocks_obsolete_ouroboros_alias_only_settings(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/civgame/settings.py\n"
            "+from pydantic_settings import BaseSettings\n"
            "+\n"
            "+class Settings(BaseSettings):\n"
            "+    llm_api_key: str | None = None\n"
            "+    llm_base_url: str | None = None\n"
            "+    llm_model: str | None = None\n"
            "+    ouroboros_api_key: str | None = None\n"
            "+    ouroboros_base_url: str | None = None\n"
            "+    ouroboros_model: str | None = None\n"
            "+\n"
            "+    @property\n"
            "+    def effective_api_key(self) -> str | None:\n"
            "+        return self.llm_api_key or self.ouroboros_api_key\n"
            "+\n"
            "+    @property\n"
            "+    def effective_base_url(self) -> str | None:\n"
            "+        return self.llm_base_url or self.ouroboros_base_url\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "llm_runtime_contract"
    assert "OUROBOROS_LLM_API_KEY" in " ".join(payload["issues"])
    assert "OUROBOROS_LLM_BASE_URL" in " ".join(payload["issues"])
    assert not (ws_dir / "src" / "civgame" / "settings.py").exists()


def test_apply_workspace_patch_allows_obsolete_aliases_with_required_llm_aliases(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/civgame/settings.py\n"
            "+import os\n"
            "+\n"
            "+def resolve_llm_runtime():\n"
            "+    api_key = os.getenv('OUROBOROS_LLM_API_KEY') or os.getenv('LLM_API_KEY') or os.getenv('OUROBOROS_API_KEY')\n"
            "+    base_url = os.getenv('OUROBOROS_LLM_BASE_URL') or os.getenv('LLM_BASE_URL') or os.getenv('OUROBOROS_BASE_URL')\n"
            "+    model = os.getenv('OUROBOROS_MODEL') or os.getenv('LLM_MODEL')\n"
            "+    if not api_key or not base_url or not model:\n"
            "+        raise RuntimeError('Missing inherited LLM runtime configuration')\n"
            "+    return api_key, base_url, model\n"
            "*** End Patch\n"
        ),
    )

    assert json.loads(raw.split("\n\n", 1)[0])["status"] == "applied"
    assert (ws_dir / "src" / "civgame" / "settings.py").exists()


def test_apply_workspace_patch_blocks_captured_llm_sentiment_fallback(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/civgame/agents/diplomacy.py\n"
            "+def _parse_decision_from_llm(llm_response: str):\n"
            "+    response_lower = llm_response.lower()\n"
            "+    if 'accept' in response_lower[:100]:\n"
            "+        return True, llm_response[:200]\n"
            "+    elif 'reject' in response_lower[:100]:\n"
            "+        return False, llm_response[:200]\n"
            "+    else:\n"
            "+        # Fallback: count positive/negative sentiment\n"
            "+        positive_words = ['accept', 'agree', 'fair', 'good']\n"
            "+        negative_words = ['reject', 'refuse', 'bad', 'unfair']\n"
            "+        positive_count = sum(1 for word in positive_words if word in response_lower)\n"
            "+        negative_count = sum(1 for word in negative_words if word in response_lower)\n"
            "+        return positive_count > negative_count, llm_response[:200]\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "llm_behavior_fallback_contract"
    assert "positive_words" in payload["issues"][0]
    assert not (ws_dir / "src" / "civgame" / "agents" / "diplomacy.py").exists()


def test_apply_workspace_patch_blocks_plural_heuristics_llm_fallback(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/civgame/agents/economy.py\n"
            "+def decide_economy_action(llm_response: str):\n"
            "+    if llm_response.strip():\n"
            "+        return {'source': 'llm', 'action': llm_response}\n"
            "+    # Fallback to heuristics for a safe economic decision action.\n"
            "+    return {'source': 'heuristic', 'action': 'build_farm'}\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["reason"] == "llm_behavior_fallback_contract"
    assert "heuristics" in payload["issues"][0]
    assert not (ws_dir / "src" / "civgame" / "agents" / "economy.py").exists()


def test_apply_workspace_patch_allows_protective_no_llm_fallback_text(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/civgame/agents/strict_parser.py\n"
            "+def _parse_decision_from_llm(llm_response: str):\n"
            "+    if not llm_response.strip():\n"
            "+        raise ValueError('LLM decision missing; do not fallback to heuristic sentiment')\n"
            "+    return llm_response\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert (ws_dir / "src" / "civgame" / "agents" / "strict_parser.py").exists()


def test_apply_workspace_patch_can_delete_layout_noise_after_read(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    noise = ws_dir / "check_markers.py"
    noise.write_text("print('temporary probe')\n", encoding="utf-8")

    umbrella_tools.read_workspace_file(ctx, workspace_id=ws_id, file_path="check_markers.py")
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: check_markers.py\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw.split("\n\n", 1)[0])
    assert payload["status"] == "applied"
    assert payload["applied"] == ["deleted check_markers.py"]
    assert not noise.exists()
    assert ctx.loop_state_view["subtask_diff"]["check_markers.py"]["deleted_file"] is True


def test_apply_workspace_patch_delete_keeps_protected_workspace_files(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    protected = ws_dir / "workspace.toml"
    protected.write_text("[workspace]\n", encoding="utf-8")

    umbrella_tools.read_workspace_file(ctx, workspace_id=ws_id, file_path="workspace.toml")
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: workspace.toml\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "protected_file"
    assert protected.exists()


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
