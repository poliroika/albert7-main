import json
from pathlib import Path


def test_switch_model_sets_runtime_overrides(monkeypatch, tmp_path: Path):
    from ouroboros.llm import LLMClient
    from ouroboros.tools.control import _switch_model
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setattr(LLMClient, "available_models", lambda self: ["demo-model"])
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    result = _switch_model(
        ctx,
        model="demo-model",
        effort="high",
        max_tokens=4096,
        temperature=0.4,
        tool_choice="required",
    )

    assert "model=demo-model" in result
    assert ctx.active_model_override == "demo-model"
    assert ctx.active_effort_override == "high"
    assert ctx.active_max_tokens_override == 4096
    assert ctx.active_temperature_override == 0.4
    assert ctx.active_tool_choice_override == "required"


def test_propose_discovery_plan_accepts_workspace_files_alias(tmp_path: Path):
    from ouroboros.tools.control import _propose_discovery_plan
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    result = _propose_discovery_plan(
        ctx,
        phases=[
            {
                "phase": "planner",
                "sources": ["workspace_files", "github_search", "mcp_discover"],
                "max_calls": 3,
            }
        ],
    )

    assert result.startswith("OK")
    assert ctx.loop_state_view["discovery_plan"]["phases"][0]["sources"] == [
        "workspace",
        "github",
        "mcp",
    ]


def test_call_llm_with_retry_passes_runtime_overrides(tmp_path: Path):
    from ouroboros.loop import _call_llm_with_retry

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = []

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            return (
                {"content": "done", "tool_calls": []},
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost": 0.01,
                },
            )

    llm = FakeLLM()
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True, exist_ok=True)

    msg, cost = _call_llm_with_retry(
        llm=llm,
        messages=[{"role": "user", "content": "hello"}],
        model="demo-model",
        tools=[{"type": "function", "function": {"name": "demo"}}],
        effort="high",
        max_tokens=4096,
        temperature=0.3,
        tool_choice="required",
        max_retries=1,
        drive_logs=drive_logs,
        task_id="task-1",
        round_idx=1,
        event_queue=None,
        accumulated_usage={},
        task_type="task",
    )

    assert msg is not None
    assert cost == 0.01
    assert llm.calls[0]["max_tokens"] == 4096
    assert llm.calls[0]["temperature"] == 0.3
    assert llm.calls[0]["tool_choice"] == "required"


def test_llm_error_classification_marks_non_retryable() -> None:
    from ouroboros.loop import _classify_llm_error

    assert _classify_llm_error(RuntimeError("404 model not found")) == "model_not_found"
    assert (
        _classify_llm_error(RuntimeError("maximum context length exceeded"))
        == "context_limit"
    )


def test_web_search_uses_duckduckgo_fallback(monkeypatch, tmp_path: Path):
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools import search as search_mod

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    requested_limits = []
    monkeypatch.setattr(
        search_mod,
        "_duckduckgo_search_results",
        lambda query, max_results=5: (
            requested_limits.append(max_results)
            or [
                {
                    "title": "Cursor",
                    "url": "https://cursor.com",
                    "snippet": f"Result for {query}",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        search_mod,
        "_summarize_results_with_llm",
        lambda query, results: "Summarized answer",
    )

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    payload = json.loads(search_mod._web_search(ctx, "cursor", max_results=7))

    assert payload["provider"] == "duckduckgo_plus_llm_summary"
    assert payload["answer"] == "Summarized answer"
    assert payload["max_results"] == 7
    assert requested_limits == [7]
    assert payload["sources"][0]["url"] == "https://cursor.com"


def test_web_tools_are_core_and_schema_is_unified(tmp_path: Path):
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    core_schemas = registry.schemas(core_only=True)
    core_names = [schema["function"]["name"] for schema in core_schemas]

    assert "web_search" not in core_names
    assert "web_fetch" in core_names
    assert "deep_search" in core_names
    assert "github_project_search" in core_names
    assert "github_extract_snippets" in core_names
    assert "mcp_discover" in core_names
    assert "mcp_install" in core_names

    assert registry.get_schema_by_name("web_search") is None
    assert "TOOL_DISABLED" in registry.execute("web_search", {"query": "cursor"})


def test_loop_stop_request_matches_task_id(tmp_path: Path):
    from ouroboros.loop import _check_stop_requested

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "stop_requested.json").write_text(
        json.dumps({"task_id": "task-1", "reason": "operator"}),
        encoding="utf-8",
    )
    usage = {}
    trace = {"assistant_notes": []}

    ignored = _check_stop_requested(tmp_path, "other-task", usage, trace)
    result = _check_stop_requested(tmp_path, "task-1", usage, trace, "visible note")

    assert ignored is None
    assert result is not None
    assert result[0] == "Stop requested by dashboard: operator"
    assert trace["assistant_notes"] == ["visible note"]


def test_loop_stop_request_matches_remediation_child_task(tmp_path: Path):
    from ouroboros.loop import _check_stop_requested

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "stop_requested.json").write_text(
        json.dumps(
            {"run_id": "run-1", "attempt_task_ids": ["run-1"], "reason": "operator"}
        ),
        encoding="utf-8",
    )

    result = _check_stop_requested(
        tmp_path,
        "run-1__remediation_1",
        {},
        {"assistant_notes": []},
    )

    assert result is not None
    assert result[0] == "Stop requested by dashboard: operator"
