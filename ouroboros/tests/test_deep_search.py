"""Tests for the intent-aware deep_search tool."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch


from ouroboros.tools import deep_search as ds


@dataclass
class _Ctx:
    repo_dir: Path
    drive_root: Path
    host_repo_root: Path | None = None
    task_id: str = "task_ds"
    pending_events: list[Any] = field(default_factory=list)


def _make_ctx(tmp_path: Path, task_id: str = "task_ds") -> _Ctx:
    repo = tmp_path / "ws"
    repo.mkdir()
    drive = repo / ".memory" / "drive"
    (drive / "memory").mkdir(parents=True)
    return _Ctx(
        repo_dir=repo,
        drive_root=drive,
        host_repo_root=tmp_path,
        task_id=task_id,
    )


def test_deep_search_requires_intent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OUROBOROS_DEEP_SEARCH_BUDGET", raising=False)
    ds.reset_budget_for_task("task_ds_intent")
    ctx = _make_ctx(tmp_path, "task_ds_intent")
    out = ds._deep_search(ctx, query="hello world", intent="")
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "intent" in payload["reason"].lower()


def test_deep_search_rejects_unknown_intent(tmp_path: Path) -> None:
    ds.reset_budget_for_task("task_ds_unk")
    ctx = _make_ctx(tmp_path, "task_ds_unk")
    out = ds._deep_search(ctx, query="hello", intent="bogus")
    payload = json.loads(out)
    assert payload["status"] == "error"


def test_deep_search_falls_back_when_gmas_missing(tmp_path: Path) -> None:
    with patch.dict(
        "os.environ",
        {"OUROBOROS_DEEP_SEARCH_ALLOW_SLOW_FALLBACK": "1"},
        clear=False,
    ):
        _assert_deep_search_falls_back_when_gmas_missing(tmp_path)


def _assert_deep_search_falls_back_when_gmas_missing(tmp_path: Path) -> None:
    ds.reset_budget_for_task("task_ds_fb")
    ctx = _make_ctx(tmp_path, "task_ds_fb")
    fake_results = [
        {"title": "X", "url": "https://x.example", "snippet": "hi"},
    ]
    with (
        patch.object(ds, "_gmas_search", return_value=None) as gmas,
        patch.object(ds, "_fallback_search", return_value=fake_results) as fb,
    ):
        out = ds._deep_search(ctx, query="how to do X", intent="planner_research")
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["provider"] == "duckduckgo_fallback"
    assert payload["results"] == fake_results
    gmas.assert_called_once()
    fb.assert_called_once()


def test_parse_formatted_results_handles_indented_urls() -> None:
    text = """Found 1 result(s):

[1] WebSockets - FastAPI
    URL: https://fastapi.tiangolo.com/advanced/websockets/
    Install websockets before using the endpoint.
"""

    parsed = ds._parse_formatted_results(text)

    assert parsed == [
        {
            "title": "WebSockets - FastAPI",
            "url": "https://fastapi.tiangolo.com/advanced/websockets/",
            "snippet": "Install websockets before using the endpoint.",
            "content": "",
        }
    ]


def test_deep_search_provider_unavailable_without_fast_provider(
    tmp_path: Path, monkeypatch
) -> None:
    for name in ds._FAST_SEARCH_PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OUROBOROS_DEEP_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("OUROBOROS_DEEP_SEARCH_ALLOW_SLOW_FALLBACK", raising=False)
    monkeypatch.delenv("OUROBOROS_WEB_SEARCH_ALLOW_DUCKDUCKGO", raising=False)
    ctx = _make_ctx(tmp_path, "task_ds_no_provider")

    out = ds._deep_search(ctx, query="slow web", intent="planner_research")

    payload = json.loads(out)
    assert payload["status"] == "provider_unavailable"


def test_deep_search_writes_to_knowledge_md(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    ds.reset_budget_for_task("task_ds_persist")
    ctx = _make_ctx(tmp_path, "task_ds_persist")
    fake_results = [{"title": "T1", "url": "https://t1.example", "snippet": "s1"}]
    with patch.object(ds, "_gmas_search", return_value=fake_results):
        out = ds._deep_search(
            ctx, query="how to import flask", intent="subtask_evidence"
        )
    payload = json.loads(out)
    assert payload["status"] == "ok"
    md_path = (
        ctx.repo_dir
        / ".memory"
        / "drive"
        / "memory"
        / "knowledge"
        / ds.KNOWLEDGE_FILENAME
    )
    assert md_path.exists()
    text = md_path.read_text(encoding="utf-8")
    assert "subtask_evidence" in text
    assert "flask" in text
    assert "https://t1.example" in text


def test_deep_search_uses_drive_root_workspace_memory_when_repo_dir_is_ouroboros(
    tmp_path: Path,
) -> None:
    host = tmp_path / "repo"
    ouroboros_repo = host / "ouroboros"
    workspace = host / "workspaces" / "ws_search"
    drive = workspace / ".memory" / "drive"
    ouroboros_repo.mkdir(parents=True)
    (drive / "memory").mkdir(parents=True)
    ctx = _Ctx(
        repo_dir=ouroboros_repo,
        drive_root=drive,
        host_repo_root=host,
        task_id="task_ds_workspace",
    )
    fake_results = [{"title": "T1", "url": "https://t1.example", "snippet": "s1"}]

    knowledge_path, ideas_path = ds._persist(
        ctx,
        query="mcp registry examples",
        intent="mcp_discovery",
        results=fake_results,
    )

    assert (drive / "memory" / "knowledge" / ds.KNOWLEDGE_FILENAME).exists()
    assert (workspace / ".memory" / "ideas.jsonl").exists()
    assert not (ouroboros_repo / "ideas.jsonl").exists()
    assert knowledge_path.startswith(
        "workspaces/ws_search/.memory/drive/memory/knowledge"
    )
    assert ideas_path == "workspaces/ws_search/.memory/ideas.jsonl"


def test_deep_search_budget_exhausted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_DEEP_SEARCH_BUDGET", "2")
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    ds.reset_budget_for_task("task_ds_budget")
    ctx = _make_ctx(tmp_path, "task_ds_budget")
    fake_results = [{"title": "T", "url": "https://t.example", "snippet": "s"}]
    with patch.object(ds, "_gmas_search", return_value=fake_results):
        a = json.loads(ds._deep_search(ctx, query="q1", intent="planner_research"))
        b = json.loads(ds._deep_search(ctx, query="q2", intent="planner_research"))
        c = json.loads(ds._deep_search(ctx, query="q3", intent="planner_research"))
    assert a["status"] == "ok"
    assert b["status"] == "ok"
    assert c["status"] == "BUDGET_EXHAUSTED"
    assert c["limit"] == 2


def test_deep_search_disabled_when_env_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_DEEP_SEARCH_ENABLED", "0")
    ctx = _make_ctx(tmp_path, "task_ds_off")
    out = ds._deep_search(ctx, query="q", intent="planner_research")
    payload = json.loads(out)
    assert payload["status"] == "disabled"


def test_planner_focus_block_mentions_deep_search() -> None:
    from ouroboros.task_planner import (
        Subtask,
        TaskPlan,
        focus_block,
    )

    plan = TaskPlan(
        task_id="t1",
        workspace_id="ws_x",
        objective_digest="x",
        subtasks=[
            Subtask(
                id="s1",
                title="Implement X",
                description="Do the thing",
                success_check="acceptance_command: pytest -q",
            ),
        ],
        cursor=0,
    )
    block = focus_block(plan)
    assert "deep_search" in block
    assert "subtask_evidence" in block


def test_planner_system_prompt_mentions_deep_search() -> None:
    from ouroboros.task_planner import planner_system_prompt

    prompt = planner_system_prompt("Build a thing.")
    assert "deep_search" in prompt
    assert "planner_research" in prompt


def test_remediation_prompt_mentions_deep_search() -> None:
    from umbrella.orchestration.ouroboros_task import (
        render_verification_remediation_prompt,
    )

    prompt = render_verification_remediation_prompt(
        original_task="Build a thing.",
        verification_report={
            "summary": "X failed",
            "results": [{"name": "x", "status": "failed", "kind": "test"}],
        },
        attempt=1,
        max_attempts=3,
        previous_final_message="prev",
    )
    assert "deep_search" in prompt
    assert "verification_repair" in prompt
