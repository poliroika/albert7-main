import json
from pathlib import Path

from ouroboros.tools.git import _repo_write_commit, get_tools
from ouroboros.tools.registry import ToolContext


def _write_execute_plan(drive_root: Path) -> None:
    state = drive_root / "state"
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
                            {"id": "1.1", "status": "done"},
                            {
                                "id": "1.2",
                                "status": "pending",
                                "success_test": {
                                    "kind": "cmd",
                                    "value": (
                                        "pytest "
                                        "tests/test_game_engine.py::test_turn_processing -v"
                                    ),
                                },
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_failed_success_test_log(drive_root: Path) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    command = ["pytest", "tests/test_game_engine.py::test_turn_processing", "-v"]
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as f:
        for idx in range(3):
            f.write(
                json.dumps(
                    {
                        "ts": f"2026-05-18T09:2{idx}:00+00:00",
                        "task_id": "phase_web_e3137dc0:execute",
                        "tool": "shell",
                        "args": {},
                        "result_preview": json.dumps(
                            {
                                "workspace_id": "example",
                                "command": command,
                                "exit_code": 1,
                                "output": (
                                    "FAILED "
                                    "tests/test_game_engine.py::test_turn_processing"
                                ),
                            }
                        ),
                    }
                )
                + "\n"
            )


def _write_active_ai_turn_execute_plan(drive_root: Path) -> None:
    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_3c82757e",
                "nodes": [
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {"id": "subtask_03", "status": "done"},
                            {
                                "id": "subtask_04",
                                "status": "pending",
                                "success_test": (
                                    "pytest tests/test_ai_turn_executor.py "
                                    "-v --tb=short"
                                ),
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_failed_ai_turn_success_test_log(drive_root: Path) -> None:
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    result_preview = {
        "workspace_id": "civilization",
        "cwd": "C:\\Users\\poliroika\\Documents\\albert7\\workspaces\\civilization",
        "command": [
            "C:\\Users\\poliroika\\Documents\\albert7\\.venv\\Scripts\\python.exe",
            "-m",
            "pytest",
            "tests/test_ai_turn_executor.py",
            "-v",
            "--tb=short",
        ],
        "exit_code": 1,
        "output": (
            "FAILED "
            "tests/test_ai_turn_executor.py::"
            "TestExecuteAITurn::test_execute_ai_turn_returns_decision_log"
        ),
    }
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-18T14:54:26.597645+00:00",
                "task_id": "phase_web_3c82757e:execute",
                "tool": "terminal_session",
                "args": {},
                "result_preview": json.dumps(result_preview),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _ai_turn_executor_contract_tests() -> str:
    return "\n".join(
        [
            "class TestBuildCivAIGraph:",
            "    def test_build_graph_returns_rolegraph(self):",
            "        assert True",
            "    def test_build_graph_has_five_nodes(self):",
            "        assert True",
            "",
            "class TestExecuteAITurn:",
            "    def test_execute_ai_turn_returns_decision_log(self):",
            "        assert True",
            "    def test_execute_ai_without_llm_config_raises_error(self):",
            "        assert True",
            "    def test_execute_ai_raises_after_max_retries(self):",
            "        assert True",
        ]
    ) + "\n"


def test_repo_write_commit_writes_file_when_local_commits_are_disabled(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    drive_root = tmp_path / ".memory" / "drive"
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/app.py",
        content="print('hello')\n",
        commit_message="",
    )

    assert result.startswith("OK: wrote workspaces/example/app.py")
    assert "GIT_COMMIT_DISABLED_BY_POLICY" in result
    assert (tmp_path / "workspaces" / "example" / "app.py").read_text(
        encoding="utf-8"
    ) == "print('hello')\n"


def test_repo_write_commit_accepts_content_wrapper_when_commits_are_disabled(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/app.py",
        content={"content": "print('wrapped')\n", "content_truncated": True},
    )

    assert result.startswith("OK: wrote workspaces/example/app.py")
    assert (tmp_path / "workspaces" / "example" / "app.py").read_text(
        encoding="utf-8"
    ) == "print('wrapped')\n"


def test_repo_write_commit_scopes_relative_path_to_current_workspace(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    drive_root = tmp_path / "workspaces" / "example" / ".memory" / "drive"
    drive_root.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
    )

    result = _repo_write_commit(
        ctx,
        path="tests/test_scope.py",
        content="def test_scope():\n    assert True\n",
    )

    assert result.startswith("OK: wrote workspaces/example/tests/test_scope.py")
    assert not (tmp_path / "tests" / "test_scope.py").exists()
    assert (
        tmp_path / "workspaces" / "example" / "tests" / "test_scope.py"
    ).read_text(encoding="utf-8") == "def test_scope():\n    assert True\n"


def test_repo_write_commit_strips_active_workspace_prefix_in_workspace_context(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "civilization"
    drive_root = workspace / ".memory" / "drive"
    drive_root.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
    )

    result = _repo_write_commit(
        ctx,
        path="civilization/src/civilization_engine/models.py",
        content="VALUE = 1\n",
    )

    assert result.startswith(
        "OK: wrote workspaces/civilization/src/civilization_engine/models.py"
    )
    assert not (workspace / "civilization" / "src").exists()
    assert (workspace / "src" / "civilization_engine" / "models.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"


def test_repo_write_commit_collapses_duplicate_active_workspace_prefix_from_live_payload(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "civilization"
    drive_root = workspace / ".memory" / "drive"
    drive_root.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/civilization/civilization/src/civilization_engine/serializers.py",
        content="VALUE = 2\n",
    )

    assert result.startswith(
        "OK: wrote workspaces/civilization/src/civilization_engine/serializers.py"
    )
    assert not (workspace / "civilization" / "src").exists()
    assert (
        workspace / "src" / "civilization_engine" / "serializers.py"
    ).read_text(encoding="utf-8") == "VALUE = 2\n"


def test_repo_write_commit_keeps_explicit_host_repo_prefix_in_workspace_context(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    drive_root = tmp_path / "workspaces" / "example" / ".memory" / "drive"
    drive_root.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
    )

    result = _repo_write_commit(
        ctx,
        path="ouroboros/notes.py",
        content="print('host edit')\n",
    )

    assert result.startswith("OK: wrote ouroboros/notes.py")
    assert (tmp_path / "ouroboros" / "notes.py").read_text(
        encoding="utf-8"
    ) == "print('host edit')\n"
    assert not (tmp_path / "workspaces" / "example" / "ouroboros").exists()


def test_repo_write_commit_blocks_captured_phase_workspace_write_bypass(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "civilization"
    target = workspace / "src" / "civilization" / "game_logic" / "__init__.py"
    drive_root = workspace / ".memory" / "drive"
    (drive_root / "state").mkdir(parents=True)
    (drive_root / "state" / "state.json").write_text(
        json.dumps({"current_task": {"workspace_id": "civilization"}}),
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
        task_id="phase_web_5b7db028:execute",
    )
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _repo_write_commit(
        ctx,
        path="workspaces/civilization/src/civilization/game_logic/__init__.py",
        content="from .board import HexGrid\n",
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_tool_bypass"
    assert payload["tool"] == "repo_write_commit"
    assert payload["workspace_id"] == "civilization"
    assert payload["path"] == "src/civilization/game_logic/__init__.py"
    assert "apply_workspace_patch" in payload["next_step"]
    assert ".memory logs" in payload["message"]
    assert not target.exists()


def test_repo_write_commit_requires_gmas_context_before_first_workspace_write(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()
    (workspace / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )
    drive_root = tmp_path / ".memory" / "drive"
    (drive_root / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
        task_id="run-1:execute",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/app.py",
        content="print('hello')\n",
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "gmas_context_before_first_write"
    assert not (workspace / "app.py").exists()


def test_repo_write_commit_accepts_gmas_context_tool_before_workspace_write(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()
    (workspace / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )
    drive_root = tmp_path / ".memory" / "drive"
    logs_dir = drive_root / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "tools.jsonl").write_text(
        '{"task_id":"run-1:execute","tool":"get_gmas_context","result_preview":"{}"}\n',
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
        task_id="run-1:execute",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/app.py",
        content="print('hello')\n",
    )

    assert result.startswith("OK: wrote workspaces/example/app.py")
    assert (workspace / "app.py").read_text(encoding="utf-8") == "print('hello')\n"


def test_repo_write_commit_blocks_accidental_source_truncation(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    target = workspace / "src" / "civsim" / "models.py"
    target.parent.mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()
    target.write_text(
        '"""Models."""\n\n'
        + "\n\n".join(
            f"class Model{i}:\n    def method_{i}(self):\n        return {i}\n"
            for i in range(60)
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=workspace / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/src/civsim/models.py",
        content='"""Models."""\n\nfrom dataclasses import dataclass\n',
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "source_truncation_guard"
    assert "Model59" in target.read_text(encoding="utf-8")


def test_repo_write_commit_respects_web_ui_stop_request(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    drive_root = workspace / ".memory" / "drive"
    state = drive_root / "state"
    state.mkdir(parents=True)
    (state / "stop_requested.json").write_text(
        json.dumps({"run_id": "run-stop", "task_id": "run-stop:execute"}),
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
        task_id="run-stop:execute",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/app.py",
        content="print('should not write')\n",
    )

    payload = json.loads(result)
    assert payload["reason"] == "stop_requested"
    assert not (workspace / "app.py").exists()


def test_repo_write_commit_requires_watcher_after_repeated_success_test_failures(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    drive_root = workspace / ".memory" / "drive"
    _write_execute_plan(drive_root)
    _write_failed_success_test_log(drive_root)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
        task_id="phase_web_e3137dc0:execute",
    )
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {"phase_label": "execute"}

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/src/civgame/game_engine.py",
        content="print('still flailing')\n",
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "phase_subtask_retry_escalation_required"
    assert payload["tool"] == "repo_write_commit"
    assert not (workspace / "src" / "civgame" / "game_engine.py").exists()


def test_repo_write_commit_blocks_erasing_existing_test_suite(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    target = tmp_path / "workspaces" / "example" / "tests" / "test_bot_tools.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "def test_one(): pass",
                "def test_two(): pass",
                "def test_three(): pass",
                "class TestMore:",
                "    def test_four(self): pass",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / "workspaces" / "example" / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/tests/test_bot_tools.py",
        content="import pytest\n",
    )

    payload = json.loads(result)
    assert payload["reason"] == "test_weakening_guard"
    assert "test_one" in target.read_text(encoding="utf-8")


def test_repo_write_commit_blocks_removing_asserted_mapping_contracts(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    target = tmp_path / "workspaces" / "example" / "tests" / "test_api.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "def test_one():",
                "    result = {'type': 'success'}",
                "    assert result['type'] == 'success'",
                "def test_two():",
                "    result = {'type': 'success'}",
                "    assert result['type'] == 'success'",
                "def test_three():",
                "    result = {'type': 'success'}",
                "    assert result['type'] == 'success'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / "workspaces" / "example" / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/tests/test_api.py",
        content="\n".join(
            [
                "def test_one():",
                "    result = 'success'",
                "    assert isinstance(result, str)",
                "def test_two():",
                "    result = 'success'",
                "    assert isinstance(result, str)",
                "def test_three():",
                "    result = 'success'",
                "    assert isinstance(result, str)",
            ]
        )
        + "\n",
    )

    payload = json.loads(result)
    assert payload["reason"] == "test_weakening_guard"
    assert payload["removed_assertion_keys"] == {"type": 3}
    assert "result['type']" in target.read_text(encoding="utf-8")


def test_repo_write_commit_blocks_replacing_active_failed_success_test_items(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    target = workspace / "tests" / "test_ai_turn_executor.py"
    target.parent.mkdir(parents=True)
    old_tests = _ai_turn_executor_contract_tests()
    target.write_text(old_tests, encoding="utf-8")
    drive_root = workspace / ".memory" / "drive"
    _write_active_ai_turn_execute_plan(drive_root)
    _write_failed_ai_turn_success_test_log(drive_root)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
        task_id="phase_web_3c82757e:execute",
    )
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {"phase_label": "execute"}

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/tests/test_ai_turn_executor.py",
        content="\n".join(
            [
                "class TestBuildAIGraph:",
                "    def test_build_graph_returns_graph_builder(self):",
                "        assert True",
                "    def test_build_graph_creates_task_node(self):",
                "        assert True",
                "    def test_build_graph_creates_agent_nodes(self):",
                "        assert True",
                "",
                "class TestAgentExecutionError:",
                "    def test_agent_execution_error_contains_civ_id(self):",
                "        assert True",
                "",
                "class TestGraphStructure:",
                "    def test_agent_addition_to_graph(self):",
                "        assert True",
                "    def test_graph_build_smoke(self):",
                "        assert True",
            ]
        )
        + "\n",
    )

    payload = json.loads(result)
    assert payload["reason"] == "test_weakening_guard"
    assert payload["subreason"] == "declared_success_test_item_removal"
    assert "test_execute_ai_turn_returns_decision_log" in payload["removed_test_items"]
    assert target.read_text(encoding="utf-8") == old_tests


def test_repo_write_commit_allows_non_phase_active_failed_success_test_additive_coverage(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    target = workspace / "tests" / "test_ai_turn_executor.py"
    target.parent.mkdir(parents=True)
    old_tests = _ai_turn_executor_contract_tests()
    target.write_text(old_tests, encoding="utf-8")
    drive_root = workspace / ".memory" / "drive"
    _write_active_ai_turn_execute_plan(drive_root)
    _write_failed_ai_turn_success_test_log(drive_root)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive_root,
        task_id="phase_web_3c82757e:execute",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/tests/test_ai_turn_executor.py",
        content=old_tests
        + "\n"
        + "def test_execute_ai_turn_records_retry_metadata():\n"
        + "    assert True\n",
    )

    assert result.startswith("OK: wrote workspaces/example/tests/test_ai_turn_executor.py")
    assert "test_execute_ai_turn_records_retry_metadata" in target.read_text(
        encoding="utf-8"
    )


def test_repo_write_commit_blocks_workspace_layout_policy_violations(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/check_markers.py",
        content="print('probe')\n",
    )

    payload = json.loads(result)
    assert payload["reason"] == "workspace_layout_policy"
    assert not (workspace / "check_markers.py").exists()

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/test_generate.py",
        content="def test_generate():\n    assert True\n",
    )

    payload = json.loads(result)
    assert payload["reason"] == "workspace_layout_policy"
    assert not (workspace / "test_generate.py").exists()


def test_repo_write_commit_blocks_python_syntax_errors_in_workspace(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/backend/app.py",
        content="def broken(:\n",
    )

    payload = json.loads(result)
    assert payload["reason"] == "python_syntax_error"
    assert not (workspace / "backend" / "app.py").exists()


def test_repo_write_commit_blocks_hardcoded_llm_runtime_defaults(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/game_bots/agents.py",
        content=(
            "import os\n"
            "def get_llm_caller():\n"
            "    api_key = os.environ.get('OUROBOROS_LLM_API_KEY') or os.environ.get('LLM_API_KEY') or os.environ.get('OPENAI_API_KEY', '')\n"
            "    base_url = os.environ.get('OUROBOROS_LLM_BASE_URL') or os.environ.get('LLM_BASE_URL') or 'https://api.openai.com/v1'\n"
            "    model = os.environ.get('OUROBOROS_MODEL') or os.environ.get('LLM_MODEL') or 'gpt-4o-mini'\n"
            "    return api_key, base_url, model\n"
        ),
    )

    payload = json.loads(result)
    assert payload["reason"] == "llm_runtime_contract"
    assert not (workspace / "game_bots" / "agents.py").exists()


def test_repo_write_commit_blocks_hardcoded_llm_runtime_env_defaults(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/.env.local",
        content=(
            "LLM_API_KEY=your-key-here\n"
            "LLM_BASE_URL=https://api.openai.com/v1\n"
            "LLM_MODEL=gpt-4\n"
        ),
    )

    payload = json.loads(result)
    assert payload["reason"] == "llm_runtime_contract"
    assert not (workspace / ".env.local").exists()


def test_repo_write_commit_blocks_unsupported_ouroboros_model_alias(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/README.md",
        content=(
            "# LLM setup\n"
            "Set OUROBOROS_LLM_MODEL and LLM_MODEL before starting GMAS bots.\n"
        ),
    )

    payload = json.loads(result)
    assert payload["reason"] == "llm_runtime_contract"
    assert not (workspace / "README.md").exists()


def test_repo_write_commit_blocks_captured_llm_sentiment_fallback(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    workspace = tmp_path / "workspaces" / "example"
    workspace.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/src/civgame/agents/diplomacy.py",
        content=(
            "def _parse_decision_from_llm(llm_response: str):\n"
            "    response_lower = llm_response.lower()\n"
            "    if 'accept' in response_lower[:100]:\n"
            "        return True, llm_response[:200]\n"
            "    elif 'reject' in response_lower[:100]:\n"
            "        return False, llm_response[:200]\n"
            "    else:\n"
            "        # Fallback: count positive/negative sentiment\n"
            "        positive_words = ['accept', 'agree', 'fair', 'good', 'benefit', 'deal']\n"
            "        negative_words = ['reject', 'refuse', 'bad', 'unfair', 'terrible', 'no']\n"
            "        positive_count = sum(1 for word in positive_words if word in response_lower)\n"
            "        negative_count = sum(1 for word in negative_words if word in response_lower)\n"
            "        if positive_count > negative_count:\n"
            "            return True, llm_response[:200]\n"
            "        return False, llm_response[:200]\n"
        ),
    )

    payload = json.loads(result)
    assert payload["reason"] == "llm_behavior_fallback_contract"
    assert "positive_words" in payload["issues"][0]
    assert not (workspace / "src" / "civgame" / "agents" / "diplomacy.py").exists()


def test_repo_write_commit_rejects_truncated_preview_metadata(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    target = tmp_path / "workspaces" / "example" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('keep')\n", encoding="utf-8")
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/app.py",
        content={"_truncated": True},
    )

    assert result.startswith("ERROR: content_must_be_string")
    assert target.read_text(encoding="utf-8") == "print('keep')\n"


def test_repo_write_commit_rejects_truncated_preview_copied_as_string(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OUROBOROS_ALLOW_GIT_COMMIT", raising=False)
    target = tmp_path / "workspaces" / "example" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('keep')\n", encoding="utf-8")
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=tmp_path / ".memory" / "drive",
    )

    result = _repo_write_commit(
        ctx,
        path="workspaces/example/app.py",
        content='"""Generated file"""\n{"_truncated": true}',
    )

    assert result.startswith("ERROR: content_must_be_source")
    assert target.read_text(encoding="utf-8") == "print('keep')\n"


def test_repo_write_commit_schema_does_not_require_commit_message():
    schema = next(
        tool.schema for tool in get_tools() if tool.name == "repo_write_commit"
    )

    assert schema["parameters"]["required"] == ["path", "content"]
