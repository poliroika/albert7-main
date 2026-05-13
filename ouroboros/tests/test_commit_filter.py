"""Tests for the commit_workspace_changes filter (P0-3).

The filter prevents Ouroboros from accidentally committing
runtime artifacts produced by a workspace (MemPalace stores,
``__pycache__``, ``.pyc``, generated ``data/`` payloads, virtualenvs,
``node_modules``).  We exercise both the pure path classifier and the
collection helper that walks the workspace tree.
"""

from pathlib import Path

from ouroboros.tools.umbrella_tools import (
    _collect_filtered_workspace_paths,
    _excluded_workspace_rel,
)


# ---------- pure classifier ----------------------------------------------------


class TestExcludedWorkspaceRel:
    def test_memory_dir_excluded(self) -> None:
        assert _excluded_workspace_rel(
            ".memory/palace/chroma.sqlite3", include_data=False
        )
        assert _excluded_workspace_rel(".memory", include_data=False)

    def test_pycache_excluded(self) -> None:
        assert _excluded_workspace_rel(
            "__pycache__/x.cpython-312.pyc", include_data=False
        )
        assert _excluded_workspace_rel("sub/pkg/__pycache__/x.pyc", include_data=False)

    def test_pyc_pyo_excluded(self) -> None:
        assert _excluded_workspace_rel("module.pyc", include_data=False)
        assert _excluded_workspace_rel("module.pyo", include_data=False)

    def test_data_excluded_unless_opted_in(self) -> None:
        assert _excluded_workspace_rel("data/last_news.json", include_data=False)
        assert not _excluded_workspace_rel("data/last_news.json", include_data=True)

    def test_venv_and_node_modules_excluded(self) -> None:
        assert _excluded_workspace_rel(".venv/bin/python", include_data=False)
        assert _excluded_workspace_rel("node_modules/foo/index.js", include_data=False)
        assert _excluded_workspace_rel("vendor/mcp/index.js", include_data=False)

    def test_normal_files_kept(self) -> None:
        assert not _excluded_workspace_rel("web_server.py", include_data=False)
        assert not _excluded_workspace_rel("README.md", include_data=False)
        assert not _excluded_workspace_rel("tests/test_smoke.py", include_data=False)

    def test_backslash_normalised(self) -> None:
        assert _excluded_workspace_rel(".memory\\palace\\x.sqlite3", include_data=False)
        assert _excluded_workspace_rel("__pycache__\\x.pyc", include_data=False)


# ---------- _collect_filtered_workspace_paths ---------------------------------


def _make_tree(repo_root: Path, ws_id: str) -> Path:
    workspace_root = repo_root / "workspaces" / ws_id
    workspace_root.mkdir(parents=True)
    (workspace_root / "web_server.py").write_text("# server", encoding="utf-8")
    (workspace_root / "README.md").write_text("# readme", encoding="utf-8")
    (workspace_root / "tests").mkdir()
    (workspace_root / "tests" / "test_smoke.py").write_text(
        "def test_a(): assert add()==1\ndef add(): return 1\n", encoding="utf-8"
    )
    (workspace_root / "__pycache__").mkdir()
    (workspace_root / "__pycache__" / "web_server.cpython-312.pyc").write_bytes(b"\x00")
    (workspace_root / ".memory" / "palace").mkdir(parents=True)
    (workspace_root / ".memory" / "palace" / "chroma.sqlite3").write_bytes(b"\x00")
    (workspace_root / "data").mkdir()
    (workspace_root / "data" / "cache.json").write_text("{}", encoding="utf-8")
    return workspace_root


class TestCollectFilteredWorkspacePaths:
    def test_filters_runtime_noise_from_full_tree(self, tmp_path: Path) -> None:
        ws = _make_tree(tmp_path, "wid")
        paths = _collect_filtered_workspace_paths(
            tmp_path, ws, "workspaces/wid", paths=None, include_data=False
        )
        assert "workspaces/wid/web_server.py" in paths
        assert "workspaces/wid/README.md" in paths
        assert "workspaces/wid/tests/test_smoke.py" in paths

        for p in paths:
            assert "/.memory/" not in p, p
            assert "/__pycache__/" not in p, p
            assert not p.endswith(".pyc"), p
            assert "/data/" not in p, p

    def test_include_data_true_keeps_data(self, tmp_path: Path) -> None:
        ws = _make_tree(tmp_path, "wid")
        paths = _collect_filtered_workspace_paths(
            tmp_path, ws, "workspaces/wid", paths=None, include_data=True
        )
        assert any(p.endswith("data/cache.json") for p in paths)

    def test_returns_empty_for_pure_runtime_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspaces" / "noise"
        (ws / ".memory" / "palace").mkdir(parents=True)
        (ws / ".memory" / "palace" / "x.sqlite3").write_bytes(b"\x00")
        (ws / "__pycache__").mkdir()
        (ws / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        paths = _collect_filtered_workspace_paths(
            tmp_path, ws, "workspaces/noise", paths=None, include_data=False
        )
        assert paths == [], paths

    def test_explicit_path_list_filtered(self, tmp_path: Path) -> None:
        ws = _make_tree(tmp_path, "wid")
        paths = _collect_filtered_workspace_paths(
            tmp_path,
            ws,
            "workspaces/wid",
            paths=["web_server.py", ".memory/palace/chroma.sqlite3"],
            include_data=False,
        )
        assert paths == ["workspaces/wid/web_server.py"]

    def test_explicit_subdir_recurses_and_filters(self, tmp_path: Path) -> None:
        ws = _make_tree(tmp_path, "wid")
        paths = _collect_filtered_workspace_paths(
            tmp_path, ws, "workspaces/wid", paths=["tests"], include_data=False
        )
        assert paths == ["workspaces/wid/tests/test_smoke.py"]
