"""Tests for the github_discovery tools."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch


from ouroboros.tools import github_discovery as gd


@dataclass
class _Ctx:
    repo_dir: Path
    drive_root: Path
    host_repo_root: Path | None = None
    task_id: str = "task_gh"
    pending_events: list[Any] = field(default_factory=list)


def _ctx(tmp_path: Path, task_id: str = "task_gh") -> _Ctx:
    repo = tmp_path / "ws"
    (repo / ".memory" / "drive" / "memory").mkdir(parents=True)
    return _Ctx(
        repo_dir=repo,
        drive_root=repo / ".memory" / "drive",
        host_repo_root=tmp_path,
        task_id=task_id,
    )


def test_github_project_search_persists_index_md(tmp_path: Path) -> None:
    gd.reset_budget("task_gh1")
    ctx = _ctx(tmp_path, "task_gh1")
    fake_repos = [
        {
            "name": "flask",
            "full_name": "pallets/flask",
            "html_url": "https://github.com/pallets/flask",
            "description": "Web microframework",
            "stargazers_count": 65000,
            "forks_count": 16000,
            "topics": ["python", "web"],
            "language": "Python",
            "license": {"spdx_id": "BSD-3-Clause", "key": "bsd-3-clause"},
        }
    ]
    with patch.object(
        gd, "_github_search_repositories", return_value=(fake_repos, None)
    ):
        out = gd._github_project_search(
            ctx, query="web framework", language="python", max_repos=1
        )
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["results"][0]["license"] == "bsd-3-clause"
    assert payload["results"][0]["license_permissive"] is True
    index = (
        ctx.repo_dir
        / ".memory"
        / "drive"
        / "memory"
        / "knowledge"
        / "inspiration"
        / "pallets"
        / "flask"
        / "index.md"
    )
    assert index.exists()
    assert "pallets/flask" in index.read_text(encoding="utf-8")


def test_github_project_search_uses_workspace_drive_not_repo_dir_memory(
    tmp_path: Path,
) -> None:
    gd.reset_budget("task_gh_drive")
    repo_dir = tmp_path / "repo" / "ouroboros"
    workspace = tmp_path / "repo" / "workspaces" / "demo"
    drive = workspace / ".memory" / "drive"
    (drive / "memory").mkdir(parents=True)
    repo_dir.mkdir(parents=True)
    ctx = _Ctx(
        repo_dir=repo_dir,
        drive_root=drive,
        host_repo_root=tmp_path / "repo",
        task_id="task_gh_drive",
    )
    fake_repos = [
        {
            "name": "pptemp",
            "full_name": "Ameyanagi/pptemp",
            "html_url": "https://github.com/Ameyanagi/pptemp",
            "description": "PowerPoint automation",
            "stargazers_count": 1,
            "forks_count": 2,
            "topics": ["python-pptx"],
            "language": "Python",
            "license": {"spdx_id": "MIT", "key": "mit"},
        }
    ]

    with patch.object(
        gd, "_github_search_repositories", return_value=(fake_repos, None)
    ):
        payload = json.loads(gd._github_project_search(ctx, query="pptx", max_repos=1))

    expected = (
        drive
        / "memory"
        / "knowledge"
        / "inspiration"
        / "Ameyanagi"
        / "pptemp"
        / "index.md"
    )
    wrong = repo_dir / ".memory" / "drive" / "memory" / "knowledge" / "inspiration"
    assert expected.exists()
    assert not wrong.exists()
    assert (
        payload["results"][0]["index_md"]
        .replace("\\", "/")
        .endswith(
            "workspaces/demo/.memory/drive/memory/knowledge/inspiration/Ameyanagi/pptemp/index.md"
        )
    )


def test_github_project_search_accepts_max_results_alias(tmp_path: Path) -> None:
    gd.reset_budget("task_gh_alias")
    ctx = _ctx(tmp_path, "task_gh_alias")
    requested: list[int] = []

    def fake_search(query: str, *, max_repos: int):
        requested.append(max_repos)
        return [], None

    with patch.object(
        gd, "_github_search_repositories", side_effect=fake_search
    ):
        payload = json.loads(
            gd._github_project_search(
                ctx,
                query="civilization strategy game AI bot python",
                max_results=3,
            )
        )

    assert payload["status"] == "ok"
    assert requested == [3]


def test_github_extract_snippets_blocks_non_permissive_body(tmp_path: Path) -> None:
    gd.reset_budget("task_gh_lic")
    ctx = _ctx(tmp_path, "task_gh_lic")
    with (
        patch.object(
            gd,
            "_fetch_repo_meta",
            return_value={"license": {"spdx_id": "GPL-3.0", "key": "gpl-3.0"}},
        ),
        patch.object(gd, "_fetch_file_raw", return_value="def main():\n    pass\n"),
    ):
        out = gd._github_extract_snippets(
            ctx, repo_full_name="some/gpl-repo", paths=["src/main.py"]
        )
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["license_permissive"] is False
    assert payload["extracted"][0]["full_body_included"] is False
    assert payload["extracted"][0]["licence_blocked_reason"] == "non_permissive_licence"
    md = (
        ctx.repo_dir
        / ".memory"
        / "drive"
        / "memory"
        / "knowledge"
        / "inspiration"
        / "some"
        / "gpl-repo"
        / "src/main.py.md"
    )
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "Body suppressed" in text
    assert "Snippet" not in text or "Body suppressed" in text


def test_github_extract_snippets_includes_body_for_permissive(tmp_path: Path) -> None:
    gd.reset_budget("task_gh_per")
    ctx = _ctx(tmp_path, "task_gh_per")
    with (
        patch.object(
            gd,
            "_fetch_repo_meta",
            return_value={"license": {"spdx_id": "MIT", "key": "mit"}},
        ),
        patch.object(
            gd, "_fetch_file_raw", return_value="def hello():\n    return 'hi'\n"
        ),
    ):
        out = gd._github_extract_snippets(
            ctx, repo_full_name="some/mit-repo", paths=["hello.py"]
        )
    payload = json.loads(out)
    assert payload["license_permissive"] is True
    assert payload["repo_source_id"] == "github:some/mit-repo"
    assert payload["extracted"][0]["research_source_id"] == "github:some/mit-repo"
    assert payload["extracted"][0]["source_id"] == "github:some/mit-repo/hello.py"
    assert "research_finding records with source_id=github:some/mit-repo" in payload[
        "next_step"
    ]
    assert "extracted[].source_id only for codeptr" in payload["next_step"]
    assert payload["extracted"][0]["full_body_included"] is True
    md = (
        ctx.repo_dir
        / ".memory"
        / "drive"
        / "memory"
        / "knowledge"
        / "inspiration"
        / "some"
        / "mit-repo"
        / "hello.py.md"
    )
    text = md.read_text(encoding="utf-8")
    assert "def hello" in text


def test_github_extract_snippets_accepts_query_alias(tmp_path: Path) -> None:
    gd.reset_budget("task_gh_query_alias")
    ctx = _ctx(tmp_path, "task_gh_query_alias")
    with (
        patch.object(
            gd,
            "_fetch_repo_meta",
            return_value={"license": {"spdx_id": "MIT", "key": "mit"}},
        ),
        patch.object(
            gd,
            "_github_search_code_in_repo",
            return_value=[{"path": "src/server.py"}, {"path": "src/extra.py"}],
        ),
        patch.object(gd, "_fetch_file_raw", return_value="def serve():\n    pass\n"),
    ):
        out = gd._github_extract_snippets(
            ctx,
            repo_full_name="some/mit-repo",
            query="websocket",
            intent="planner_research",
            max_results=1,
        )
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["intent"] == "planner_research"
    assert len(payload["extracted"]) == 1
    assert payload["extracted"][0]["path"] == "src/extra.py"


def test_github_extract_snippets_expands_dirs_and_globs_preferring_code(
    tmp_path: Path,
) -> None:
    gd.reset_budget("task_gh_expand")
    ctx = _ctx(tmp_path, "task_gh_expand")

    def fake_raw(_repo: str, path: str, ref: str = "HEAD") -> str:
        return {
            "examples/build_deck.py": "def build_deck():\n    return 'deck'\n",
            "pptemp/core.py": "class TemplateEngine:\n    pass\n",
            "README.md": "# Docs only\n",
        }.get(path, "")

    with (
        patch.object(
            gd,
            "_fetch_repo_meta",
            return_value={"license": {"spdx_id": "MIT", "key": "mit"}},
        ),
        patch.object(
            gd,
            "_github_contents",
            return_value=[
                {"type": "file", "path": "examples/build_deck.py"},
                {"type": "file", "path": "README.md"},
            ],
        ),
        patch.object(
            gd,
            "_github_tree_paths",
            return_value=["pptemp/core.py", "README.md"],
        ),
        patch.object(gd, "_fetch_file_raw", side_effect=fake_raw),
    ):
        out = gd._github_extract_snippets(
            ctx,
            repo_full_name="some/mit-repo",
            paths=["examples/", "pptemp/*.py", "README.md"],
        )

    payload = json.loads(out)
    paths = [item["path"] for item in payload["extracted"]]
    assert paths[:2] == ["examples/build_deck.py", "pptemp/core.py"]
    assert "README.md" in paths
    code_md = (
        ctx.repo_dir
        / ".memory"
        / "drive"
        / "memory"
        / "knowledge"
        / "inspiration"
        / "some"
        / "mit-repo"
        / "examples"
        / "build_deck.py.md"
    )
    assert code_md.exists()
    assert "def build_deck" in code_md.read_text(encoding="utf-8")


def test_github_extract_snippets_tags_code_as_implementation(tmp_path: Path) -> None:
    gd.reset_budget("task_gh_tags")
    ctx = _ctx(tmp_path, "task_gh_tags")
    captured: list[dict[str, Any]] = []

    def fake_mirror(_ctx: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"mirrored": True}

    with (
        patch.object(
            gd,
            "_fetch_repo_meta",
            return_value={"license": {"spdx_id": "MIT", "key": "mit"}},
        ),
        patch.object(
            gd, "_fetch_file_raw", return_value="def implement():\n    pass\n"
        ),
        patch(
            "umbrella.memory.external_findings.mirror_external_finding_to_memory",
            side_effect=fake_mirror,
        ),
    ):
        payload = json.loads(
            gd._github_extract_snippets(
                ctx,
                repo_full_name="some/mit-repo",
                paths=["src/impl.py"],
            )
        )

    assert payload["memory_mirrored_count"] == 1
    assert captured
    tags = captured[0]["tags"]
    assert "implementation" in tags
    assert "code_pattern" in tags


def test_github_project_search_budget_exhausted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_GITHUB_DISCOVERY_BUDGET", "1")
    gd.reset_budget("task_gh_b")
    ctx = _ctx(tmp_path, "task_gh_b")
    with patch.object(gd, "_github_search_repositories", return_value=([], None)):
        a = json.loads(gd._github_project_search(ctx, query="q1"))
        b = json.loads(gd._github_project_search(ctx, query="q2"))
    assert a["status"] == "ok"
    assert b["status"] == "BUDGET_EXHAUSTED"


def test_github_project_search_attempts_memory_mirror(tmp_path: Path) -> None:
    """Regression: search results must flow into workspace memory.

    Before the fix ``github_project_search`` only wrote markdown to
    ``knowledge/inspiration/`` and was invisible to ``get_umbrella_memory``.
    The response now exposes ``memory_mirrored_count`` so callers (and
    integration tests) can see whether the mirror leg fired.
    """
    gd.reset_budget("task_gh_mirror")
    ctx = _ctx(tmp_path, "task_gh_mirror")
    fake_repos = [
        {
            "name": "demo",
            "full_name": "acme/demo",
            "html_url": "https://github.com/acme/demo",
            "description": "demo repo",
            "stargazers_count": 1,
            "forks_count": 0,
            "topics": [],
            "language": "Python",
            "license": {"spdx_id": "MIT", "key": "mit"},
        }
    ]
    with patch.object(
        gd, "_github_search_repositories", return_value=(fake_repos, None)
    ):
        out = gd._github_project_search(ctx, query="demo", max_repos=1)
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert "github_extract_snippets" in payload.get("next_step", "")
    assert "memory_mirrored_count" in payload, (
        "github_project_search must report memory mirror outcome so the "
        "discovery gate / future recall can rely on it"
    )


def test_default_github_discovery_budgets() -> None:
    assert gd.DEFAULT_BUDGET == 10
    assert gd.DEFAULT_EXTRACT_BUDGET == 12
    assert gd._budget_search() == 10
    assert gd._budget_extract() == 12


def test_github_project_search_rate_limited(tmp_path: Path) -> None:
    gd.reset_budget("task_gh_rl")
    ctx = _ctx(tmp_path, "task_gh_rl")
    with patch.object(
        gd, "_github_search_repositories", return_value=([], "rate_limited")
    ):
        payload = json.loads(gd._github_project_search(ctx, query="civ game"))
    assert payload["status"] == "rate_limited"
    assert payload["results"] == []
