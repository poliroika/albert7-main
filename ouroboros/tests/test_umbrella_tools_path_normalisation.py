"""Regression tests for workspace-relative path normalisation.

The agent occasionally passes a repo-relative ``workspaces/<id>/foo/bar``
to tools that expect a workspace-relative path. Without normalisation we
end up creating ``workspaces/<id>/workspaces/<id>/foo/bar`` on disk.
"""

from pathlib import Path

from ouroboros.tools.umbrella_tools import (
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


def test_strip_workspace_prefix_ignores_other_workspace_id() -> None:
    assert (
        _strip_workspace_prefix("demo_ws", "workspaces/other_ws/main.py")
        == "workspaces/other_ws/main.py"
    )


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
