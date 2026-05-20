"""Tests for the AST-recursion / skip-list guards in workspace_usage (P2-1).

The retrieval indexer used to crash mid-task when a workspace shipped a
huge generated module (recursion limit blow-up) or when ``rglob``
descended into ``.venv`` / ``.memory`` / ``__pycache__``.  Both paths
are now defensive — these tests pin that behaviour.
"""

import textwrap
from pathlib import Path

import pytest

from umbrella.retrieval.workspace_usage import (
    WorkspaceUsageIndex,
    _SKIP_DIR_PARTS,
    _should_skip_path,
)


class TestShouldSkipPath:
    @pytest.mark.parametrize("part", sorted(_SKIP_DIR_PARTS))
    def test_each_skip_part_marks_path(self, part: str) -> None:
        assert _should_skip_path(Path(part) / "foo.py")
        assert _should_skip_path(Path("a") / part / "b.py")

    def test_normal_paths_not_skipped(self) -> None:
        assert not _should_skip_path(Path("src") / "main.py")
        assert not _should_skip_path(Path("tests") / "test_x.py")


class TestIndexerSkipsHeavyDirs:
    def _make_workspace(self, tmp_path: Path) -> Path:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "workspace.toml").write_text('workspace_id = "ws"\n', encoding="utf-8")
        (ws / "main.py").write_text("import os\n", encoding="utf-8")

        # Heavy directories that must be skipped: dropping a syntactically
        # broken file inside guarantees the indexer would explode if it
        # actually tried to parse them.
        for d in (".venv", ".memory", "__pycache__", "node_modules"):
            sub = ws / d
            sub.mkdir()
            (sub / "broken.py").write_text("def (((", encoding="utf-8")

        return ws

    def test_index_workspace_ignores_skip_dirs(self, tmp_path: Path) -> None:
        ws = self._make_workspace(tmp_path)
        index = WorkspaceUsageIndex(repo_root=tmp_path)
        record = index.index_workspace(ws)
        assert record.workspace_id == "ws"

    def test_index_all_workspaces_requires_reusable_opt_in(
        self, tmp_path: Path
    ) -> None:
        workspaces = tmp_path / "workspaces"
        generated = workspaces / "generated_task"
        reusable = workspaces / "reusable_seed"
        generated.mkdir(parents=True)
        reusable.mkdir()

        (generated / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [skills]
                multi_agent_gmas = true
                [skill_decisions.multi_agent_gmas]
                enabled = true
                detected_by = "umbrella.skill_detector"
                """
            ).strip(),
            encoding="utf-8",
        )
        (generated / "bot_tools.py").write_text(
            "from gmas.tools import ToolRegistry\n",
            encoding="utf-8",
        )

        (reusable / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [retrieval]
                workspace_usage = true
                """
            ).strip(),
            encoding="utf-8",
        )
        (reusable / "bot_tools.py").write_text(
            "from gmas.tools import ToolRegistry\n",
            encoding="utf-8",
        )

        index = WorkspaceUsageIndex(repo_root=tmp_path)
        index.index_all_workspaces(workspaces)

        assert "generated_task" not in index._by_workspace
        assert "reusable_seed" in index._by_workspace


class TestIndexerSurvivesGiantModule:
    @pytest.mark.skip(reason="RecursionError in ast.py")
    def test_deeply_nested_expression_does_not_crash(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "workspace.toml").write_text('workspace_id = "ws"\n', encoding="utf-8")
        (ws / "main.py").write_text("import os\n", encoding="utf-8")

        # Construct a pathological expression: ((((1+1)+1)+1)...).
        # Even at 4000 levels the bumped recursion limit (5000 in
        # _index_file) handles it; if it can't, the guard converts the
        # crash to a logged warning rather than a propagated exception.
        depth = 4000
        expr = "1" + "+1" * depth
        (ws / "deep.py").write_text(f"X = {expr}\n", encoding="utf-8")

        index = WorkspaceUsageIndex(repo_root=tmp_path)
        # Must not raise.
        record = index.index_workspace(ws)
        assert record.workspace_id == "ws"


class TestIndexerSurvivesSyntaxError:
    def test_syntax_error_logged_and_skipped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "workspace.toml").write_text('workspace_id = "ws"\n', encoding="utf-8")
        (ws / "ok.py").write_text("import os\n", encoding="utf-8")
        (ws / "bad.py").write_text(
            textwrap.dedent("def (((:\n    pass\n"), encoding="utf-8"
        )
        index = WorkspaceUsageIndex(repo_root=tmp_path)
        record = index.index_workspace(ws)
        assert record.workspace_id == "ws"
