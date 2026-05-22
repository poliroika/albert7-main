"""Tests for the intent-aware deep_search tool."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
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


def _search_payload(results: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok" if results else "no_results",
        "provider": "gmas_deep_search",
        "browser_backend": "playwright",
        "answer": "answer",
        "results": results,
        "sources": [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("snippet", ""),
            }
            for item in results
        ],
        "attempts": [{"provider": "duckduckgo", "status": "success", "result_count": len(results)}],
    }
    payload.update(extra)
    return payload


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


def test_deep_search_uses_gmas_playwright_by_default(tmp_path: Path) -> None:
    ds.reset_budget_for_task("task_ds_gmas_playwright")
    ctx = _make_ctx(tmp_path, "task_ds_gmas_playwright")
    fake_results = [
        {"title": "X", "url": "https://x.example", "snippet": "hi"},
    ]
    with patch.object(ds, "_gmas_search", return_value=_search_payload(fake_results)) as gmas:
        out = ds._deep_search(ctx, query="how to do X", intent="planner_research")
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["provider"] == "gmas_deep_search"
    assert payload["browser_backend"] == "playwright"
    assert payload["results"][0]["title"] == fake_results[0]["title"]
    assert payload["results"][0]["url"] == fake_results[0]["url"]
    assert payload["results"][0]["snippet"] == fake_results[0]["snippet"]
    assert "content" in payload["results"][0]
    gmas.assert_called_once_with(
        "how to do X",
        max_results=5,
        fetch_content=True,
        deep=True,
        provider="",
        intent="planner_research",
    )


def test_deep_search_no_key_provider_uses_gmas_duckduckgo(
    tmp_path: Path, monkeypatch
) -> None:
    for name in (
        "SERPER_API_KEY",
        "TAVILY_API_KEY",
        "BRAVE_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OUROBOROS_DEEP_SEARCH_PROVIDER", raising=False)
    ctx = _make_ctx(tmp_path, "task_ds_no_provider")
    fake_results = [{"title": "No key", "url": "https://example.com", "snippet": "ok"}]

    with patch.object(ds, "_gmas_search", return_value=_search_payload(fake_results)) as gmas:
        out = ds._deep_search(ctx, query="slow web", intent="planner_research")

    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["provider"] == "gmas_deep_search"
    assert payload["results"][0]["title"] == fake_results[0]["title"]
    assert payload["results"][0]["url"] == fake_results[0]["url"]
    assert payload["results"][0]["snippet"] == fake_results[0]["snippet"]
    assert "content" in payload["results"][0]
    gmas.assert_called_once()


def test_gmas_search_playwright_error_falls_back_to_http(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeHttpTool:
        def _search_with_fallback(self, query, max_results, **kwargs):
            assert query == "dynamic docs"
            assert kwargs["intent"] == "planner_research"
            return (
                [
                    {
                        "title": "Docs",
                        "url": "https://example.com/docs",
                        "snippet": "short",
                    }
                ],
                [
                    SimpleNamespace(
                        provider="duckduckgo",
                        status="success",
                        result_count=1,
                        error=None,
                    )
                ],
            )

        def _fetch_content_for_results(self, results, *_args, **_kwargs):
            results[0]["content"] = "rendered documentation content"

        def _prepare_results_for_output(self, results, **_kwargs):
            return results

        def _format_search_results(self, results, **_kwargs):
            return "formatted docs"

        def close(self):
            pass

    def create_fake_tool(**kwargs):
        calls.append(kwargs)
        if kwargs.get("deep_search") == "playwright":
            raise RuntimeError("Playwright browser missing")
        return FakeHttpTool()

    monkeypatch.setattr(ds, "_create_gmas_web_search_tool", create_fake_tool)

    payload = ds._gmas_search(
        "dynamic docs",
        max_results=3,
        fetch_content=True,
        deep=True,
        intent="planner_research",
    )

    assert payload["status"] == "ok"
    assert payload["provider"] == "gmas_deep_search"
    assert payload["browser_backend"] == "http_fetch_fallback"
    assert "Playwright browser missing" in payload["browser_fallback_from"]
    assert payload["results"][0]["content"] == "rendered documentation content"
    assert calls[0]["deep_search"] == "playwright"
    assert calls[1]["deep_search"] is None


def test_deep_search_auto_uses_firecrawl_when_key_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.delenv("OUROBOROS_DEEP_SEARCH_ENGINE", raising=False)
    ds.reset_budget_for_task("task_ds_firecrawl_auto")
    ctx = _make_ctx(tmp_path, "task_ds_firecrawl_auto")
    fake_results = [
        {
            "title": "Firecrawl",
            "url": "https://firecrawl.dev",
            "snippet": "search and scrape",
            "content": "markdown content",
        }
    ]

    with patch.object(
        ds,
        "_external_deep_search",
        return_value=_search_payload(
            fake_results,
            provider="firecrawl_deep_search",
            browser_backend="firecrawl_search_scrape",
        ),
    ) as external:
        payload = json.loads(
            ds._deep_search(ctx, query="web scraping", intent="planner_research")
        )

    assert payload["status"] == "ok"
    assert payload["engine"] == "firecrawl"
    assert payload["provider"] == "firecrawl_deep_search"
    assert payload["results"][0]["content"] == "markdown content"
    external.assert_called_once_with(
        "web scraping",
        max_results=5,
        fetch_content=True,
        engine="firecrawl",
    )


def test_deep_search_external_auto_error_falls_back_to_gmas(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "jina-test")
    monkeypatch.delenv("OUROBOROS_DEEP_SEARCH_ENGINE", raising=False)
    ds.reset_budget_for_task("task_ds_jina_auto_error")
    ctx = _make_ctx(tmp_path, "task_ds_jina_auto_error")
    gmas_results = [{"title": "GMAS", "url": "https://example.com", "snippet": "ok"}]

    with (
        patch.object(
            ds,
            "_external_deep_search",
            return_value={
                "status": "provider_error",
                "provider": "jina_reader_search",
                "error": "blocked",
                "results": [],
                "sources": [],
                "attempts": [],
            },
        ) as external,
        patch.object(ds, "_gmas_search", return_value=_search_payload(gmas_results)) as gmas,
    ):
        payload = json.loads(
            ds._deep_search(ctx, query="docs", intent="planner_research")
        )

    assert payload["status"] == "ok"
    assert payload["engine"] == "jina"
    assert payload["provider"] == "gmas_deep_search"
    assert payload["external_engine_fallback_from"] == "jina_reader_search"
    external.assert_called_once()
    gmas.assert_called_once()


def test_firecrawl_search_normalizes_scraped_markdown(monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    def fake_post(url, body, *, headers, timeout):
        assert url == "https://api.firecrawl.dev/v2/search"
        assert headers["Authorization"] == "Bearer fc-test"
        assert body["scrapeOptions"]["formats"] == [{"type": "markdown"}]
        return {
            "success": True,
            "creditsUsed": 1,
            "data": {
                "web": [
                    {
                        "title": "Example",
                        "description": "A page",
                        "url": "https://example.com",
                        "markdown": "# Example\nBody",
                    }
                ]
            },
        }

    monkeypatch.setattr(ds, "_post_json", fake_post)

    payload = ds._firecrawl_search("example", max_results=3, fetch_content=True)

    assert payload["status"] == "ok"
    assert payload["provider"] == "firecrawl_deep_search"
    assert payload["results"][0]["content"] == "# Example\nBody"
    assert payload["attempts"][0]["credits_used"] == 1


def test_jina_search_parses_reader_urls(monkeypatch) -> None:
    monkeypatch.delenv("JINA_API_KEY", raising=False)

    def fake_get(url, *, headers=None, timeout=45):
        assert url.startswith("https://s.jina.ai/")
        return "Title\nURL Source: https://example.com/page\nUseful extracted content"

    monkeypatch.setattr(ds, "_get_text", fake_get)

    payload = ds._jina_search("example query", max_results=2)

    assert payload["status"] == "ok"
    assert payload["provider"] == "jina_reader_search"
    assert payload["sources"][0]["url"] == "https://example.com/page"
    assert payload["results"][0]["content"].startswith("Title")


def test_deep_search_schema_does_not_mention_openai() -> None:
    schema = ds.get_tools()[0].schema
    text = json.dumps(schema, ensure_ascii=False)

    assert "OPENAI_API_KEY" not in text
    assert "OpenAI" not in text
    assert "Playwright" in text
    assert "DuckDuckGo" in text
    assert "Firecrawl" in text
    assert "Jina" in text


def test_deep_search_writes_to_knowledge_md(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    ds.reset_budget_for_task("task_ds_persist")
    ctx = _make_ctx(tmp_path, "task_ds_persist")
    fake_results = [{"title": "T1", "url": "https://t1.example", "snippet": "s1"}]
    with patch.object(ds, "_gmas_search", return_value=_search_payload(fake_results)):
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
    with patch.object(ds, "_gmas_search", return_value=_search_payload(fake_results)):
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
