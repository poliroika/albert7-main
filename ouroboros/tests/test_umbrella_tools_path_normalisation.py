"""Regression tests for workspace-relative path normalisation.

The agent occasionally passes a repo-relative ``workspaces/<id>/foo/bar``
to tools that expect a workspace-relative path. Without normalisation we
end up creating ``workspaces/<id>/workspaces/<id>/foo/bar`` on disk.
"""

import json
from pathlib import Path

from ouroboros.tools.umbrella_tools import (
    apply_workspace_patch,
    delete_workspace_file,
    _gmas_context_before_write_block,
    _maybe_rewrite_workspace_command,
    _strip_workspace_prefix,
    list_workspace_files,
    read_workspace_file,
    update_workspace_seed,
)


class _FakeCtx:
    def __init__(self, repo_root: Path, drive_root: Path) -> None:
        self.repo_dir = repo_root
        self.host_repo_root = repo_root
        self.drive_root = drive_root


def _make_workspace(tmp_path: Path, workspace_id: str) -> Path:
    (tmp_path / "umbrella").mkdir(exist_ok=True)
    workspace = tmp_path / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "TASK_MAIN.md").write_text("# task", encoding="utf-8")
    return workspace


def test_strip_workspace_prefix_removes_repo_relative_prefix() -> None:
    assert (
        _strip_workspace_prefix("demo_ws", "workspaces/demo_ws/foo/bar.py")
        == "foo/bar.py"
    )


def test_strip_workspace_prefix_handles_double_prefix() -> None:
    assert (
        _strip_workspace_prefix(
            "demo_ws",
            "workspaces/demo_ws/workspaces/demo_ws/main.py",
        )
        == "main.py"
    )


def test_strip_workspace_prefix_handles_workspace_root_path() -> None:
    assert _strip_workspace_prefix("demo_ws", "workspaces/demo_ws") == ""
    assert _strip_workspace_prefix("demo_ws", "demo_ws") == ""


def test_strip_workspace_prefix_removes_bare_workspace_prefix() -> None:
    assert _strip_workspace_prefix("demo_ws", "demo_ws/TASK_MAIN.md") == "TASK_MAIN.md"
    assert (
        _strip_workspace_prefix("demo_ws", "demo_ws/src/app.py")
        == "src/app.py"
    )


def test_strip_workspace_prefix_keeps_already_relative_path() -> None:
    assert _strip_workspace_prefix("demo_ws", "main.py") == "main.py"
    assert (
        _strip_workspace_prefix("demo_ws", "weather_ui/data_provider.py")
        == "weather_ui/data_provider.py"
    )


def test_strip_workspace_prefix_handles_leading_slash_and_backslash() -> None:
    assert (
        _strip_workspace_prefix("demo_ws", "/workspaces/demo_ws/main.py") == "main.py"
    )
    assert (
        _strip_workspace_prefix("demo_ws", "workspaces\\demo_ws\\main.py") == "main.py"
    )


def test_strip_workspace_prefix_handles_dot_workspaces_typo() -> None:
    assert (
        _strip_workspace_prefix(
            "demo_ws",
            ".workspaces/demo_ws/.memory/drive/memory/knowledge/index.md",
        )
        == ".memory/drive/memory/knowledge/index.md"
    )


def test_strip_workspace_prefix_is_case_insensitive_for_prefix_only() -> None:
    assert (
        _strip_workspace_prefix("demo_ws", "WORKSPACES/demo_ws/TASK_MAIN.md")
        == "TASK_MAIN.md"
    )
    assert (
        _strip_workspace_prefix("demo_ws", "Demo_WS/src/app.py")
        == "src/app.py"
    )


def test_strip_workspace_prefix_ignores_other_workspace_id() -> None:
    assert (
        _strip_workspace_prefix("demo_ws", "workspaces/other_ws/main.py")
        == "workspaces/other_ws/main.py"
    )


def test_delete_workspace_file_blocks_source_repair_delete(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    target = workspace / "backend" / "models" / "game_state.py"
    target.parent.mkdir(parents=True)
    target.write_text("class GameState:\n    pass\n", encoding="utf-8")
    ctx = _FakeCtx(tmp_path, tmp_path / ".umbrella" / "drive")

    payload = json.loads(
        delete_workspace_file(
            ctx,
            workspace_id="demo_ws",
            file_path="backend/models/game_state.py",
            reason="Corrupted/truncated file needs to be recreated cleanly",
        )
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "source_repair_delete_blocked"
    assert "sanctioned full-file write" not in payload["next_step"]
    assert "apply_workspace_patch" in payload["next_step"]
    assert target.exists()


def test_delete_workspace_file_blocks_source_fix_delete_reason(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    target = workspace / "src" / "demo" / "__init__.py"
    target.parent.mkdir(parents=True)
    target.write_text("from .missing import Missing\n", encoding="utf-8")
    ctx = _FakeCtx(tmp_path, tmp_path / ".umbrella" / "drive")

    payload = json.loads(
        delete_workspace_file(
            ctx,
            workspace_id="demo_ws",
            file_path="src/demo/__init__.py",
            reason="Fix import by recreating clean file",
        )
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "source_repair_delete_blocked"
    assert target.exists()


def test_delete_workspace_file_blocks_captured_test_replacement_reason(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    target = workspace / "tests" / "test_game_engine.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_contract():\n    assert True\n", encoding="utf-8")
    ctx = _FakeCtx(tmp_path, tmp_path / ".umbrella" / "drive")

    payload = json.loads(
        delete_workspace_file(
            ctx,
            workspace_id="demo_ws",
            file_path="tests/test_game_engine.py",
            reason="Replacing with tests that match actual implementation",
        )
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "source_repair_delete_blocked"
    assert "apply_workspace_patch" in payload["next_step"]
    assert target.exists()


def test_delete_workspace_file_blocks_managed_source_delete_without_repair_words(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    target = workspace / "src" / "demo" / "engine.py"
    target.parent.mkdir(parents=True)
    target.write_text("class Engine:\n    pass\n", encoding="utf-8")
    ctx = _FakeCtx(tmp_path, tmp_path / ".umbrella" / "drive")

    payload = json.loads(
        delete_workspace_file(
            ctx,
            workspace_id="demo_ws",
            file_path="src/demo/engine.py",
            reason="obsolete after refactor",
        )
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "source_repair_delete_blocked"
    assert target.exists()


def test_delete_workspace_file_allows_cleanup_probe_delete(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    target = workspace / "check_markers.py"
    target.write_text("print('probe')\n", encoding="utf-8")
    ctx = _FakeCtx(tmp_path, tmp_path / ".umbrella" / "drive")

    payload = json.loads(
        delete_workspace_file(
            ctx,
            workspace_id="demo_ws",
            file_path="check_markers.py",
            reason="ad-hoc probe script left over from verification",
        )
    )

    assert payload["status"] == "deleted"
    assert not target.exists()


def test_gmas_gate_ignores_blocked_write_attempt_round(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )
    drive_root = tmp_path / ".umbrella" / "drive"
    (drive_root / "logs").mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)
    ctx.task_id = "run-1:execute"
    ctx.loop_state_view = {"last_write_round": 3}
    (drive_root / "logs" / "tools.jsonl").write_text(
        (
            '{"task_id":"run-1:execute","tool":"apply_workspace_patch",'
            '"result_preview":"{\\"status\\": \\"blocked\\", '
            '\\"reason\\": \\"gmas_context_before_first_write\\"}"}\n'
        ),
        encoding="utf-8",
    )

    block = _gmas_context_before_write_block(ctx, "demo_ws", workspace)

    assert block is not None
    assert block["reason"] == "gmas_context_before_first_write"


def test_gmas_gate_accepts_explicit_context_tool_call(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n",
        encoding="utf-8",
    )
    drive_root = tmp_path / ".umbrella" / "drive"
    (drive_root / "logs").mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)
    ctx.task_id = "run-1:execute"
    ctx.loop_state_view = {}
    (drive_root / "logs" / "tools.jsonl").write_text(
        '{"task_id":"run-1:execute","tool":"get_gmas_context","result_preview":"{}"}\n',
        encoding="utf-8",
    )

    assert _gmas_context_before_write_block(ctx, "demo_ws", workspace) is None


def test_update_workspace_seed_does_not_double_nest_path(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="workspaces/demo_ws/weather_ui/data_provider.py",
        new_content="VALUE = 1\n",
        create_backup=False,
    )

    assert "Updated" in result, result
    expected = workspace / "weather_ui" / "data_provider.py"
    nested = workspace / "workspaces" / "demo_ws" / "weather_ui" / "data_provider.py"
    assert expected.exists()
    assert not nested.exists()


def test_update_workspace_seed_blocks_root_diagnostic_scripts(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="check_docx.py",
        new_content="print('diagnose')\n",
        create_backup=False,
    )

    assert "workspace_layout_policy" in result
    assert not (workspace / "check_docx.py").exists()


def test_update_workspace_seed_blocks_root_pytest_modules(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="test_generate.py",
        new_content="def test_generate():\n    assert True\n",
        create_backup=False,
    )

    assert "workspace_layout_policy" in result
    assert not (workspace / "test_generate.py").exists()


def test_update_workspace_seed_blocks_diagnostic_scripts_under_src_scripts(
    tmp_path: Path,
) -> None:
    """Repeat-offender protection: the production run had the agent
    move root-level ``check_docx.py`` into ``src/scripts/check_docx.py``
    to dodge the root guard and proceed. The layout policy now matches
    the diagnostic-name pattern under ``src/scripts/`` too — real CLI
    entrypoints keep functional names (``cli.py``, ``pipeline.py``),
    diagnostic probes don't get checked in.
    """
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="src/scripts/check_docx.py",
        new_content="print('diagnose')\n",
        create_backup=False,
    )

    assert "workspace_layout_policy" in result
    assert not (workspace / "src" / "scripts" / "check_docx.py").exists()


def test_update_workspace_seed_allows_real_cli_entrypoint_under_src_scripts(
    tmp_path: Path,
) -> None:
    """Reusable CLI entrypoints with functional names stay legal under
    ``src/scripts/``.
    """
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="src/scripts/cli.py",
        new_content="def main() -> None:\n    return None\n",
        create_backup=False,
    )

    assert "Updated" in result, result
    assert (workspace / "src" / "scripts" / "cli.py").exists()


def test_update_workspace_seed_blocks_python_under_docs(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="docs/extract_requirements.py",
        new_content="print('extract')\n",
        create_backup=False,
    )

    assert "workspace_layout_policy" in result
    assert not (workspace / "docs" / "extract_requirements.py").exists()


def test_update_workspace_seed_blocks_raw_artifact_in_docs(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="docs/requirements_raw.txt",
        new_content="bullet 1\nbullet 2\n",
        create_backup=False,
    )

    assert "workspace_layout_policy" in result
    assert not (workspace / "docs" / "requirements_raw.txt").exists()


def test_update_workspace_seed_blocks_pytest_files_under_src(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="src/test_app.py",
        new_content="def test_x():\n    assert 1 == 1\n",
        create_backup=False,
    )

    assert "workspace_layout_policy" in result
    assert not (workspace / "src" / "test_app.py").exists()


def test_update_workspace_seed_blocks_greenfield_python_package_outside_src(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="game_engine/hex_grid.py",
        new_content="class HexGrid:\n    pass\n",
        create_backup=False,
    )

    assert "greenfield_python_src_layout_policy" in result
    assert not (workspace / "game_engine" / "hex_grid.py").exists()


def test_apply_workspace_patch_blocks_greenfield_python_package_outside_src(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = apply_workspace_patch(
        ctx,
        workspace_id="demo_ws",
        patch=(
            "*** Begin Patch\n"
            "*** Add File: agents/civ_agents.py\n"
            "+class CivilizationAgents:\n"
            "+    pass\n"
            "*** End Patch\n"
        ),
    )

    assert "greenfield_python_src_layout_policy" in result
    assert not (workspace / "agents" / "civ_agents.py").exists()


def test_apply_workspace_patch_blocks_bare_src_python_module(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = apply_workspace_patch(
        ctx,
        workspace_id="demo_ws",
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/game_engine.py\n"
            "+class GameEngine:\n"
            "+    pass\n"
            "*** End Patch\n"
        ),
    )

    assert "greenfield_python_src_layout_policy" in result
    assert "src/<package>" in result
    assert not (workspace / "src" / "game_engine.py").exists()


def test_apply_workspace_patch_blocks_parallel_src_python_roots(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = apply_workspace_patch(
        ctx,
        workspace_id="demo_ws",
        patch=(
            "*** Begin Patch\n"
            "*** Add File: src/api/app.py\n"
            "+class ApiApp:\n"
            "+    pass\n"
            "*** Add File: src/agents/runner.py\n"
            "+class AgentRunner:\n"
            "+    pass\n"
            "*** End Patch\n"
        ),
    )

    assert "greenfield_python_src_layout_policy" in result
    assert "one canonical package root" in result
    assert not (workspace / "src" / "api" / "app.py").exists()
    assert not (workspace / "src" / "agents" / "runner.py").exists()


def test_update_workspace_seed_allows_existing_non_src_package_repair(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "game_engine").mkdir()
    (workspace / "game_engine" / "__init__.py").write_text("", encoding="utf-8")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="game_engine/hex_grid.py",
        new_content="class HexGrid:\n    pass\n",
        create_backup=False,
    )

    assert "Updated" in result, result
    assert (workspace / "game_engine" / "hex_grid.py").exists()


def test_update_workspace_seed_allows_greenfield_src_package(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="src/civilization_game/game_engine/hex_grid.py",
        new_content="class HexGrid:\n    pass\n",
        create_backup=False,
    )

    assert "Updated" in result, result
    assert (
        workspace / "src" / "civilization_game" / "game_engine" / "hex_grid.py"
    ).exists()


def test_read_workspace_file_strips_repo_prefix(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    raw = read_workspace_file(
        ctx,
        workspace_id="demo_ws",
        file_path="workspaces/demo_ws/main.py",
    )

    assert "VALUE = 1" in raw


def test_list_workspace_files_strips_repo_prefix(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    (workspace / "weather_ui").mkdir()
    (workspace / "weather_ui" / "charts.py").write_text("X = 1\n", encoding="utf-8")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    raw = list_workspace_files(
        ctx,
        workspace_id="demo_ws",
        subdir="workspaces/demo_ws/weather_ui",
    )

    assert "charts.py" in raw


def test_maybe_rewrite_workspace_command_strips_redundant_cd() -> None:
    cmd = ["bash", "-lc", "cd workspaces/demo_ws && echo hi"]
    out = _maybe_rewrite_workspace_command(cmd, "demo_ws")
    assert out == ["bash", "-lc", "echo hi"]


def test_maybe_rewrite_workspace_command_rewrites_pip_install() -> None:
    cmd = ["bash", "-lc", "pip install -r requirements.txt"]
    out = _maybe_rewrite_workspace_command(cmd, "demo_ws")
    assert out[2].strip().startswith("python -m pip install")


def test_update_workspace_seed_unwraps_nested_new_content_dict(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = update_workspace_seed(
        ctx,
        workspace_id="demo_ws",
        file_path="mod.py",
        new_content={"new_content": "x = 1\n"},
        create_backup=False,
        validation_summary="nested payload repair",
    )

    assert "Updated" in result, result
    assert (workspace / "mod.py").read_text(encoding="utf-8") == "x = 1\n"


def test_apply_workspace_patch_blocks_top_level_use_before_definition(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = apply_workspace_patch(
        ctx,
        workspace_id="demo_ws",
        patch="""*** Begin Patch
*** Add File: main.py
from fastapi import FastAPI

app = FastAPI(lifespan=lifespan)

async def lifespan(app):
    yield
*** End Patch""",
    )

    assert "python_top_level_name_order" in result
    assert not (workspace / "main.py").exists()


def test_apply_workspace_patch_blocks_missing_local_package_import(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = apply_workspace_patch(
        ctx,
        workspace_id="demo_ws",
        patch="""*** Begin Patch
*** Add File: backend/bots/__init__.py
from backend.bots.bot_decision_engine import BotDecisionEngine

__all__ = ["BotDecisionEngine"]
*** End Patch""",
    )

    assert "python_missing_local_import" in result
    assert not (workspace / "backend" / "bots" / "__init__.py").exists()


def test_apply_workspace_patch_allows_local_import_created_in_same_patch(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path, "demo_ws")
    drive_root = tmp_path / ".umbrella" / "drive"
    drive_root.mkdir(parents=True)
    ctx = _FakeCtx(tmp_path, drive_root)

    result = apply_workspace_patch(
        ctx,
        workspace_id="demo_ws",
        patch="""*** Begin Patch
*** Add File: backend/bots/bot_decision_engine.py
class BotDecisionEngine:
    pass
*** Add File: backend/bots/__init__.py
from backend.bots.bot_decision_engine import BotDecisionEngine

__all__ = ["BotDecisionEngine"]
*** End Patch""",
    )

    assert "applied" in result, result
    assert (workspace / "backend" / "bots" / "bot_decision_engine.py").exists()
    assert (workspace / "backend" / "bots" / "__init__.py").exists()
