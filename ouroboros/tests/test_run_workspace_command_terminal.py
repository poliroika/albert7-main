"""Tests for the persistent-terminal wrapping of ``run_workspace_command``.

Focus: the wrapper must (a) delegate to the per-workspace
``TerminalSession`` it gets from ``get_or_create_session``, and (b) append
the result to ``<drive_root>/memory/terminal_scrollback.md`` so the
``## Recent terminal`` context section can show it on the next round.

These tests stub the session backend with a recording fake so they are
fully cross-platform (no tmux/bash dependency).
"""

import json
import os
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


class _EnvRecordingBackend(_RecordingBackend):
    def run(
        self, cmd: Sequence[str] | str, *, cwd: str | None, timeout: int
    ) -> RunResult:
        now = time.time()
        env = {
            "LLM_API_KEY": os.environ.get("LLM_API_KEY"),
            "LLM_BASE_URL": os.environ.get("LLM_BASE_URL"),
            "LLM_MODEL": os.environ.get("LLM_MODEL"),
        }
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout, "env": env})
        body = json.dumps(env, sort_keys=True)
        return RunResult(
            exit_code=0,
            output=body,
            marker="00112233",
            started_at=now - 0.1,
            finished_at=now,
            raw_output=body,
        )


class _FilesystemMutatingBackend(_RecordingBackend):
    def __init__(self, rel_path: str, content: str = "{}") -> None:
        super().__init__()
        self.rel_path = rel_path
        self.content = content

    def run(
        self, cmd: Sequence[str] | str, *, cwd: str | None, timeout: int
    ) -> RunResult:
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        target = Path(str(cwd or ".")) / self.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.content, encoding="utf-8")
        now = time.time()
        body = f"mutated: {self.rel_path}"
        return RunResult(
            exit_code=0,
            output=body,
            marker="00112233",
            started_at=now - 0.1,
            finished_at=now,
            raw_output=body,
        )


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


def test_run_workspace_command_bridges_host_llm_env_to_public_aliases(
    workspace, monkeypatch
) -> None:
    ctx, _ws_dir, ws_id = workspace
    backend = _EnvRecordingBackend()
    ctx._terminal_sessions = {
        ws_id: TerminalSession(workspace_id=ws_id, backend=backend)
    }
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("OUROBOROS_LLM_API_KEY", "host-key")
    monkeypatch.setenv("OUROBOROS_LLM_BASE_URL", "https://host.example/v1")
    monkeypatch.setenv("OUROBOROS_MODEL", "host/model")

    out = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-c", "print('env')"],
        timeout_seconds=5,
    )

    payload = json.loads(out)
    env = json.loads(payload["output"])
    assert env == {
        "LLM_API_KEY": "host-key",
        "LLM_BASE_URL": "https://host.example/v1",
        "LLM_MODEL": "host/model",
    }
    assert os.environ.get("LLM_API_KEY") is None
    assert os.environ.get("LLM_BASE_URL") is None
    assert os.environ.get("LLM_MODEL") is None


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


def test_run_workspace_command_does_not_treat_snapshot_content_as_mutation(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.drive_root = ws_dir / ".memory" / "drive"
    (ctx.drive_root / "logs").mkdir(parents=True)
    (ctx.drive_root / "logs" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (ws_dir / "TASK_MAIN.md").write_text("build it\n", encoding="utf-8")
    (ws_dir / "src" / "civilization").mkdir(parents=True)
    (ws_dir / "src" / "civilization" / "__init__.py").write_text(
        "__version__ = '0.1.0'\n", encoding="utf-8"
    )
    backend = _install_recording_session(ctx, ws_id)

    raw = umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=ws_id,
        argv=["python", "-c", "print('ok')"],
        timeout_seconds=5,
    )

    payload = json.loads(raw)
    assert payload["exit_code"] == 0
    assert payload["output"] == "recorded: python -c print('ok')"
    assert payload.get("reason") != "umbrella_enforcement_kernel"
    assert backend.calls


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


def test_apply_workspace_patch_blocks_workspace_toml_out_of_scope(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    _write_execute_phase_with_declared_subtasks(ctx.drive_root)
    (ws_dir / "workspace.toml").write_text(
        """[[verification.steps]]
name = "smoke"
kind = "shell"
command = ["python", "-c", "print(1)"]
""",
        encoding="utf-8",
    )
    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="workspace.toml"
    )
    new_toml = (
        "[[verification.steps]]\n"
        'name = "smoke"\n'
        'kind = "shell"\n'
        'command = ["python", "-c", "print(1)"]\n'
        "\n"
        "[[verification.steps]]\n"
        'name = "pkg"\n'
        'kind = "import_check"\n'
        'command = ["python", "-c", "import civgame"]\n'
    )
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: workspace.toml\n"
            "*** Add File: workspace.toml\n"
            + "".join(f"+{line}\n" for line in new_toml.splitlines())
            + "*** End Patch\n"
        ),
    )
    payload = json.loads(raw)
    assert payload["status"] == "blocked", payload
    assert payload["issues"][0]["code"] == "verifier_policy_write_requires_supervisor_approval"
    text = (ws_dir / "workspace.toml").read_text(encoding="utf-8")
    assert 'name = "pkg"' not in text
    assert 'name = "smoke"' in text


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


def test_apply_workspace_patch_blocks_same_path_replacement_contract_loss(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    ctx.loop_state_view = {}
    target = ws_dir / "src" / "civilization" / "game" / "models.py"
    target.parent.mkdir(parents=True)
    old_content = (
        '"""Models."""\n\n'
        + "\n\n".join(
            f"class Model{i}:\n    def method_{i}(self):\n        return {i}\n"
            for i in range(40)
        )
    )
    target.write_text(old_content, encoding="utf-8")

    umbrella_tools.read_workspace_file(
        ctx,
        workspace_id=ws_id,
        file_path="src/civilization/game/models.py",
    )
    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: src/civilization/game/models.py\n"
            "*** Add File: src/civilization/game/models.py\n"
            "+\"\"\"Models.\"\"\"\n"
            "+\n"
            "+class Model0:\n"
            "+    def method_0(self):\n"
            "+        return 0\n"
            "*** End Patch\n"
        ),
        validation_summary="Captured hunk-mismatch loop requires same-path replacement.",
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "source_truncation_guard"
    assert "Model10" in payload["missing_symbols"]
    assert "Model39" in target.read_text(encoding="utf-8")


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
            "+Resolve LLM runtime through LLM_API_KEY, "
            "LLM_BASE_URL, and LLM_MODEL.\n"
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


def test_apply_workspace_patch_requires_subtask_context_reads(workspace) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    state = ctx.drive_root / "state"
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
                                "title": "Edit engine module",
                                "files_to_change": ["tests/test_engine.py"],
                                "success_test": {
                                    "kind": "cmd",
                                    "value": "python -m pytest tests/test_engine.py -q",
                                },
                            },
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    target = ws_dir / "tests" / "test_engine.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 1\n", encoding="utf-8")

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_engine.py\n"
            "@@\n"
            "-value = 1\n"
            "+value = 2\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "subtask_context_read_required"
    assert "tests/test_engine.py" in payload["missing_reads"]


def test_apply_workspace_patch_requires_fresh_read_after_hunk_mismatch(
    workspace,
) -> None:
    ctx, ws_dir, ws_id = workspace
    _phase_run_ctx(ctx)
    target = ws_dir / "tests" / "test_engine.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 1\n", encoding="utf-8")
    umbrella_tools.read_workspace_file(
        ctx, workspace_id=ws_id, file_path="tests/test_engine.py"
    )
    logs = ctx.drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": "2099-05-20T08:00:00+00:00",
                    "task_id": "phase_web_e3137dc0:execute",
                    "tool": "apply_workspace_patch",
                    "result_preview": json.dumps(
                        {
                            "status": "blocked",
                            "reason": "patch_hunk_mismatch",
                            "file_path": "tests/test_engine.py",
                            "error": "failed to match patch hunk in tests/test_engine.py",
                        }
                    ),
                }
            )
            + "\n"
        )

    raw = umbrella_tools.apply_workspace_patch(
        ctx,
        workspace_id=ws_id,
        patch=(
            "*** Begin Patch\n"
            "*** Update File: tests/test_engine.py\n"
            "@@\n"
            "-value = 1\n"
            "+value = 2\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "fresh_read_after_hunk_mismatch_required"
    assert payload["file_path"] == "tests/test_engine.py"
