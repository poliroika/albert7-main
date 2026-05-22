import json

from ouroboros.tools.phase_contract import (
    _palace_add,
    _palace_search,
    _propose_phase_plan,
    _request_extra_subtask,
    _read_file,
)
from ouroboros.tools import phase_control
from ouroboros.tools.registry import ToolContext
from umbrella.deep_agent_tools.research_provenance import (
    SOURCE_ID_DESCRIPTION,
    next_finding_source_hint,
    research_finding_source_provenance_issue,
)
from umbrella.deep_agent_tools.phase_contract_tools import get_tools
from umbrella.memory.palace.facade import MemPalace


def _append_phase_tool_row(
    drive,
    *,
    task_id="run-123:research",
    tool,
    result,
    args=None,
):
    logs = drive / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "tools.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "task_id": task_id,
                    "tool": tool,
                    "args": args or {},
                    "result_preview": json.dumps(result),
                }
            )
            + "\n"
        )


def test_research_provenance_contract_drives_schema_description():
    palace_add = next(tool for tool in get_tools() if tool.name == "palace_add")

    assert (
        palace_add.schema["parameters"]["properties"]["source_id"]["description"]
        == SOURCE_ID_DESCRIPTION
    )


def test_research_provenance_contract_rejects_truncated_fallback_gmas_handle():
    query = "LLM agent decision making game AI economic diplomacy tools streaming"
    rows = [
        {
            "task_id": "phase_web_6b78e406:research",
            "tool": "get_gmas_context",
            "args": {"query": query, "max_results": 5},
            "result_preview": (
                '{\n'
                f'  "query": "{query}",\n'
                '  "confidence": 0.78,\n'
                '  "contexts": [\n'
                "    {\n"
                '      "source": "gmas/examples/streaming_example.py",\n'
                "      ...\n"
                '      "metadata": {"fallback": true}\n'
                "    }\n"
                "  ],\n"
                '  "status": "ok"\n'
                "}"
            ),
        }
    ]

    issue = research_finding_source_provenance_issue(
        rows,
        source_id=f"get_gmas_context:{query}",
    )
    hint = next_finding_source_hint(rows)

    assert "fallback or low-confidence GMAS retrieval" in issue
    assert f"get_gmas_context:{query}" not in hint
    assert "run an allowed discovery tool" in hint


def test_palace_search_works_during_phase_review_with_empty_memory(tmp_path):
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "run-123:research_review"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _palace_search(
        ctx,
        query="civilization llm architecture",
        workspace_id="test_ws",
    )

    assert not result.startswith("WARNING: memory error")
    payload = json.loads(result)
    assert payload["palace_memory"] == []
    assert payload["include_unverified"] is False


def test_palace_search_returns_canonical_mempalace_node_by_uuid(tmp_path):
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    node_id = MemPalace(tmp_path, "test_ws").add(
        store="palace.run",
        tier="hot",
        scope="run_scoped",
        phase="research",
        run_id="run-123",
        tags=["research_finding"],
        content="Canonical GMAS topology finding for the current run.",
        verified=True,
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:research_review"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "research_review",
        "active_workspace_id": "test_ws",
    }

    result = _palace_search(ctx, query=f"Please inspect finding {node_id}")

    payload = json.loads(result)
    assert payload["exact_lookup"]["source"] == "canonical_mempalace"
    assert payload["palace_memory"][0]["id"] == node_id
    assert payload["palace_memory"][0]["store"] == "palace.run"
    assert "Canonical GMAS topology" in payload["palace_memory"][0]["content"]


def test_palace_search_excludes_unverified_canonical_uuid_by_default(tmp_path):
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    node_id = MemPalace(tmp_path, "test_ws").add(
        store="palace.run",
        tier="hot",
        scope="run_scoped",
        phase="research",
        run_id="run-123",
        tags=["research_finding"],
        content="Unverified GMAS topology lead for the current run.",
        verified=False,
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:research_review"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "research_review",
        "active_workspace_id": "test_ws",
    }

    result = _palace_search(ctx, query=f"Please inspect finding {node_id}")

    payload = json.loads(result)
    assert payload["exact_lookup"]["missing_ids"] == [node_id]
    assert payload["palace_memory"] == []


def test_palace_search_uuid_miss_does_not_return_semantic_legacy_neighbors(tmp_path):
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    MemPalace(tmp_path, "test_ws").add(
        store="palace.run",
        tier="hot",
        scope="run_scoped",
        phase="research",
        run_id="run-123",
        tags=["research_finding"],
        content="Civilization frontend finding that should not appear for a missing UUID.",
    )
    missing_id = "11111111-2222-3333-4444-555555555555"
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:research_review"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "research_review",
        "active_workspace_id": "test_ws",
    }

    result = _palace_search(
        ctx,
        query=f"{missing_id} civilization frontend",
    )

    payload = json.loads(result)
    assert payload["exact_lookup"]["missing_ids"] == [missing_id]
    assert payload["palace_memory"] == []


def test_palace_add_rejects_ambiguous_research_finding_tag_without_kind(tmp_path):
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    (drive / "logs").mkdir()
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_19764f9b:research"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    saved = _palace_add(
        ctx,
        title="Finding: Strategy Game Simulation Architecture Patterns",
        content=(
            "# Finding: Strategy Game Simulation Architecture Patterns\n\n"
            "Hex grid coordinates, turn-based state snapshots, economic "
            "simulation, diplomacy state machines, and tool-filtered GMAS bot "
            "actions are useful architecture patterns for this project."
        ),
        workspace_id="civilization",
        tags="research_finding,research",
        source_id="research_strategy_finding",
        evidence_kind="architecture_pattern",
    )

    assert saved.startswith("ERROR:")
    assert "kind=\"research_finding\"" in saved
    assert "would be stored as `observation`" in saved


def test_palace_add_rejects_research_finding_without_current_source(tmp_path):
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_3dde17c1:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        content=(
            "First finding stored: real-time backend architecture pattern "
            "from GitHub projects."
        ),
    )

    assert result.startswith("ERROR:")
    assert "requires a source_id tied to current research-phase evidence" in result
    assert "exact tool source such as" not in result
    assert "`github_project_search:<exact query>`" in result
    assert "kind=observation" in result


def test_palace_add_rejects_unmatched_github_namespace_source(tmp_path):
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_3dde17c1:research",
        tool="github_project_search",
        result={
            "status": "ok",
            "results": [
                {
                    "full_name": "clxrityy/daily-set",
                    "html_url": "https://github.com/clxrityy/daily-set",
                }
            ],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_3dde17c1:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        content="GitHub search supports FastAPI WebSocket game architecture.",
        kind="research_finding",
        tags="research_finding,github",
        source_id="github:missing/repo",
    )

    assert result.startswith("ERROR:")
    assert "does not match any repository returned" in result


def test_palace_add_accepts_matched_github_namespace_source(tmp_path, monkeypatch):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_3dde17c1:research",
        tool="github_project_search",
        result={
            "status": "ok",
            "results": [
                {
                    "full_name": "clxrityy/daily-set",
                    "html_url": "https://github.com/clxrityy/daily-set",
                }
            ],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_3dde17c1:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        content="daily-set demonstrates FastAPI plus realtime TypeScript UI.",
        kind="research_finding",
        tags="research_finding,github",
        source_id="github:clxrityy/daily-set",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["verified"] is True
    assert payload["source_path"] == "github:clxrityy/daily-set"
    assert called is True


def test_palace_add_rejects_tool_qualified_github_source_with_empty_results(tmp_path):
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_01a37983:research",
        tool="github_project_search",
        args={
            "query": "civilization game python typescript llm bots economy diplomacy",
            "max_repos": 5,
        },
        result={
            "status": "ok",
            "query": "civilization game python typescript llm bots economy diplomacy",
            "results": [],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_01a37983:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        content=(
            "GitHub discovery for civ-like LLM games yielded several relevant "
            "implementations: civil-ai/civilization-game and Strategy-LLM."
        ),
        kind="research_finding",
        tags="research_finding,github",
        source_id=(
            "github_project_search:"
            "civilization game python typescript llm bots economy diplomacy"
        ),
    )

    assert result.startswith("ERROR:")
    assert "not a verifiable current discovery source" in result


def test_palace_add_rejects_captured_bare_github_project_search_source(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_96995622:research",
        tool="github_project_search",
        args={"query": "python game web server websocket multiplayer"},
        result={
            "status": "ok",
            "query": "python game web server websocket multiplayer",
            "results": [
                {
                    "full_name": "kochj23/Web-Pennmush",
                    "html_url": "https://github.com/kochj23/Web-Pennmush",
                }
            ],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_96995622:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="GitHub Repository Patterns for Python Web Games",
        content=(
            "Found several GitHub repositories demonstrating Python web game "
            "architecture with WebSocket updates."
        ),
        kind="research_finding",
        tags="research_finding,github",
        source_id="github_project_search",
    )

    assert result.startswith("ERROR:")
    assert "too broad for result-bearing discovery" in result
    assert "github:owner/repo" in result
    assert called is False


def test_palace_add_rejects_captured_fallback_gmas_context_as_verified_finding(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_96995622:research",
        tool="get_gmas_context",
        args={"query": "multi-agent game AI bot opponent economy diplomacy"},
        result={
            "status": "ok",
            "query": "multi-agent game AI bot opponent economy diplomacy",
            "confidence": 0.16,
            "contexts": [
                {
                    "source": "gmas/examples/multi_agent_tools_example.py",
                    "metadata": {"fallback": True},
                }
            ],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_96995622:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    for source_id in (
        "gmas:multi-agent game AI bot opponent economy diplomacy",
        "get_gmas_context",
        "get_gmas_context:multi-agent game AI bot opponent economy diplomacy",
    ):
        result = _palace_add(
            ctx,
            title="GMAS Multi-Agent Context",
            content="GMAS framework provides the required multi-agent infrastructure.",
            kind="research_finding",
            tags="research_finding,gmas",
            source_id=source_id,
        )

        assert result.startswith("ERROR:")
        assert "fallback or low-confidence GMAS retrieval" in result
        assert "kind=observation" in result
    assert called is False


def test_palace_add_rejects_captured_tool_qualified_low_confidence_gmas_source(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_30ea3d17:research",
        tool="get_gmas_context",
        args={"query": "multi-agent game simulation economy diplomacy negotiation strategy"},
        result={
            "status": "ok",
            "query": "multi-agent game simulation economy diplomacy negotiation strategy",
            "confidence": 0.21,
            "contexts": [
                {
                    "source": "gmas/patterns/agent_orchestration.md",
                    "metadata": {"fallback": False},
                }
            ],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_30ea3d17:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="GMAS Multi-Agent Context",
        content="GMAS context supports multi-agent strategy-game bots.",
        kind="research_finding",
        tags="research_finding,gmas",
        source_id=(
            "get_gmas_context:"
            "multi-agent game simulation economy diplomacy negotiation strategy"
        ),
    )

    assert result.startswith("ERROR:")
    assert "fallback or low-confidence GMAS retrieval" in result
    assert "kind=observation" in result
    assert called is False


def test_palace_add_rejects_truncated_fallback_gmas_preview_source(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    query = "LLM agent decision making game AI economic diplomacy tools streaming"
    logs.joinpath("tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_6b78e406:research",
                "tool": "get_gmas_context",
                "args": {"query": query, "max_results": 5},
                "result_preview": (
                    '{\n'
                    f'  "query": "{query}",\n'
                    '  "confidence": 0.78,\n'
                    '  "contexts": [\n'
                    "    {\n"
                    '      "source": "gmas/examples/streaming_example.py",\n'
                    "      ...\n"
                    '      "metadata": {\n'
                    '        "fallback": true\n'
                    "      }\n"
                    "    }\n"
                    "  ],\n"
                    '  "status": "ok"\n'
                    "}"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_6b78e406:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="GMAS Framework Pattern",
        content="GMAS framework provides a multi-agent runtime for bot decisions.",
        kind="research_finding",
        tags="research_finding,gmas",
        source_id=f"get_gmas_context:{query}",
    )

    assert result.startswith("ERROR:")
    assert "fallback or low-confidence GMAS retrieval" in result
    assert "kind=observation" in result
    assert called is False


def test_palace_add_rejects_captured_empty_mcp_tool_qualified_source(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_30ea3d17:research",
        tool="mcp_discover",
        args={"query": "file data analysis web requests"},
        result={
            "status": "ok",
            "query": "file data analysis web requests",
            "results": [],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_30ea3d17:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="MCP Discovery Finding",
        content="MCP discovery found useful file and web-request servers.",
        kind="research_finding",
        tags="research_finding,mcp",
        source_id="mcp_discover:file data analysis web requests",
    )

    assert result.startswith("ERROR:")
    assert "not a verifiable current discovery source" in result
    assert called is False


def test_palace_add_keeps_explicit_research_observation_as_untrusted_note(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_694128fb:research",
        tool="github_project_search",
        args={"query": "civilization game strategy AI LLM bot python"},
        result={
            "status": "ok",
            "query": "civilization game strategy AI LLM bot python",
            "results": [],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_694128fb:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        content="GitHub project search returned 0 results.",
        kind="observation",
        tags="research",
        source_id="github_project_search:civilization game strategy AI LLM bot python",
        evidence_kind="observation_from_log",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "observation"
    assert payload["verified"] is False
    assert "research_finding" not in payload["tags"]
    assert called is True


def test_palace_add_accepts_tool_qualified_deep_search_source(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_3b5618a6:research",
        tool="deep_search",
        result={
            "status": "ok",
            "intent": "planner_research",
            "query": "LLM powered Civ game AI economy diplomacy",
            "results": [
                {
                    "title": "CivRealm",
                    "url": "https://bigai-ai.github.io/civrealm/",
                }
            ],
        },
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_3b5618a6:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        content=(
            "CivRealm shows language-model agents can operate in a "
            "Civilization-inspired environment."
        ),
        kind="research_finding",
        tags="research_finding,deep_search",
        source_id="deep_search:planner_research",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["source_path"] == "deep_search:planner_research"
    assert called is True


def test_palace_add_accepts_tool_qualified_source_from_logged_args_when_preview_truncated(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _append_phase_tool_row(
        drive,
        task_id="phase_web_252f4329:research",
        tool="deep_search",
        args={
            "intent": "github_discovery",
            "query": "LLM-driven civilization strategy game architecture multi-agent",
        },
        result='{"status": "ok", "query": "LLM-driven civilization strategy ...',
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_252f4329:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        content=(
            "Vox Deorum supports hybrid LLM strategist plus algorithmic "
            "tactical execution for 4X strategy games."
        ),
        kind="research_finding",
        tags="research_finding,deep_search",
        source_id="deep_search:github_discovery",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["source_path"] == "deep_search:github_discovery"
    assert called is True


def test_palace_add_rejects_explicit_research_finding_progress_ledger(tmp_path):
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    (drive / "logs").mkdir()
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_e4cde249:research"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "test_ws",
    }

    result = _palace_add(
        ctx,
        kind="research_finding",
        content=(
            "Research evidence ledger - Current finding attempts: 0/3 accepted. "
            "Continue gathering evidence."
        ),
    )

    assert result.startswith("ERROR:")
    assert "progress ledger" in result


def test_palace_add_rejects_direct_plan_phase_plan_memory(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    (drive / "logs").mkdir()
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_98172342:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "test_ws",
    }

    result = _palace_add(
        ctx,
        title="Phase Plan - LLM-Powered Civilization Game",
        content="Rejected plan draft should not become hot execute memory.",
        kind="phase_plan",
        tags="phase_plan,plan",
        evidence_kind="plan_artifact",
    )

    assert result.startswith("ERROR:")
    assert "propose_phase_plan" in result
    assert "submit_phase_plan" in result


def test_phase_read_file_alias_supports_line_start_from_execute_capture(tmp_path):
    (tmp_path / "umbrella").mkdir()
    ws_dir = tmp_path / "workspaces" / "test_ws"
    drive = ws_dir / ".memory" / "drive"
    drive.mkdir(parents=True)
    (ws_dir / "docs").mkdir()
    (ws_dir / "docs" / "architecture.md").write_text(
        "line1\nline2\nline3\nline4\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "phase_web_468af5e0:execute"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "test_ws",
    }

    result = _read_file(
        ctx,
        file_path="docs/architecture.md",
        line_start=2,
        line_count=2,
    )

    payload = json.loads(result)
    assert payload["file_path"] == "docs/architecture.md"
    assert payload["line_start"] == 2
    assert payload["line_count"] == 2
    assert payload["content"].replace("\r\n", "\n") == "line2\nline3\n"


def test_phase_plan_proposal_memory_is_tagged_as_candidate(tmp_path, monkeypatch):
    from umbrella.deep_agent_tools import phase_contract_base as base

    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "test_ws",
    }

    captured_hot = {}
    captured_save = {}

    def fake_hot(ctx, **kwargs):
        captured_hot.update(kwargs)

    def fake_save(ctx, **kwargs):
        captured_save.update(kwargs)
        return "OK: memory saved"

    monkeypatch.setattr(base, "_persist_run_hot_memory", fake_hot)
    monkeypatch.setattr(base, "_save_umbrella_memory", fake_save)

    plan_id = base._record_phase_plan_artifact(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "build",
                    "title": "Build",
                    "goal": "Create the project skeleton.",
                    "files_to_create": ["src/demo/app.py", "tests/test_app.py"],
                    "success_test": "python -m pytest tests -q",
                }
            ]
        },
        notes="Captured proposal from a real planning run.",
    )

    assert plan_id.startswith("phase_plan:")
    assert captured_hot["tags"] == [
        "phase_plan_proposal",
        "umbrella_plan_candidate",
    ]
    saved_tags = set(captured_save["tags"].split(","))
    assert "phase_plan_proposal" in saved_tags
    assert "umbrella_plan_candidate" in saved_tags
    assert "phase_plan" not in saved_tags
    assert captured_save["kind"] == "phase_plan_proposal"
    assert captured_save["palace_path"].endswith("/phase_plan/proposals")


def test_submit_phase_plan_persists_selected_plan_not_latest(tmp_path):
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "test_ws",
    }

    first = {
        "plan_id": "selected-plan",
        "subtasks": [
            {
                "id": "selected",
                "title": "Selected plan",
                "goal": "Build selected path.",
                "files_to_create": ["src/demo/app.py", "tests/test_selected.py"],
                "success_test": "python -m pytest tests/test_selected.py -q",
            }
        ],
    }
    second = {
        "plan_id": "latest-unsubmitted",
        "subtasks": [
            {
                "id": "latest",
                "title": "Latest unsubmitted plan",
                "goal": "Build a different path.",
                "files_to_create": ["src/demo/other.py", "tests/test_latest.py"],
                "success_test": "python -m pytest tests/test_latest.py -q",
            }
        ],
    }

    _propose_phase_plan(ctx, plan=first)
    _propose_phase_plan(ctx, plan=second)
    result = phase_control._submit_phase_plan(ctx, plan_id="selected-plan")

    assert result.startswith("OK: Phase plan submitted")
    submitted = json.loads(
        (drive / "state" / "phase_plan_submitted_latest.json").read_text(
            encoding="utf-8"
        )
    )
    latest = json.loads(
        (drive / "state" / "phase_plan_proposal_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["plan_id"] == "latest-unsubmitted"
    assert submitted["plan_id"] == "selected-plan"
    assert submitted["plan"]["subtasks"][0]["id"] == "selected"
    from umbrella.memory.palace.facade import MemPalace

    palace = MemPalace(tmp_path, "test_ws")
    try:
        recall = palace.recall(
            "execute",
            run_id="run-123",
            hot_rules=[{"store": "palace.run", "tags": []}],
            n=10,
        )
    finally:
        palace.close()
    hot_text = "\n".join(str(node.get("content") or "") for node in recall.hot)
    assert "phase_plan_submitted" in hot_text
    assert "selected-plan" in hot_text
    assert "latest-unsubmitted" not in hot_text


def test_request_extra_subtask_rejects_captured_control_plane_workaround(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_651c6791:execute"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _request_extra_subtask(
        ctx,
        reason=(
            "Verification requires skill_compliance to pass, but it is a false "
            "positive for st_004. Need to mark the multi_agent_gmas skill as "
            "optional for this subtask or add a documented blocker."
        ),
        proposed_subtask={
            "id": "st_004_skill_check_blocker",
            "title": "Address false-positive skill_compliance check for st_004",
            "goal": (
                "Mark the multi_agent_gmas skill optional or add blocker_note "
                "because GMAS integration is in st_005."
            ),
            "success_test": {
                "kind": "cmd",
                "value": (
                    "python -c \"import yaml; conf = yaml.safe_load(open("
                    "'.umbrella/workspace.toml')); assert conf\""
                ),
            },
            "files_to_change": [
                ".umbrella/workspace.toml",
                "workspaces/civilization/.memory/blockers.md",
            ],
        },
    )

    assert result.startswith("ERROR: request_extra_subtask rejected")
    assert "workspace policy" in result or "skill/verification gates" in result
    assert not (drive / "state" / "phase_control_signals.jsonl").exists()


def test_request_extra_subtask_accepts_product_subtask(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:execute"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "test_ws",
    }

    result = _request_extra_subtask(
        ctx,
        reason="Add a missing scoring unit needed by the current product.",
        proposed_subtask={
            "id": "score_summary",
            "title": "Implement score summary",
            "goal": "Compute visible score totals for the game UI.",
            "files_to_create": [
                "src/test_ws/game/scoring.py",
                "tests/test_scoring.py",
            ],
            "success_test": "python -m pytest tests/test_scoring.py -q",
        },
    )

    assert result.startswith("OK: extra subtask requested")
    assert (drive / "state" / "phase_control_signals.jsonl").exists()


def test_propose_phase_plan_rejects_over_granular_greenfield_plan(tmp_path):
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "test_ws",
    }
    (tmp_path / "workspaces" / "test_ws").mkdir(parents=True, exist_ok=True)

    subtasks = []
    for idx in range(17):
        subtasks.append(
            {
                "id": f"slice_{idx}",
                "title": f"Build game slice {idx}",
                "goal": "Implement one vertical backend/frontend LLM game slice.",
                "files_to_create": (
                    ["docs/architecture.md"]
                    if idx == 0
                    else [f"src/civ/slice_{idx}.py", f"tests/test_slice_{idx}.py"]
                ),
                "success_test": f"python -m pytest tests/test_slice_{idx}.py -q",
            }
        )

    result = _propose_phase_plan(
        ctx,
        plan={
            "summary": "Large FastAPI React GMAS civilization game.",
            "subtasks": subtasks,
        },
    )

    assert result.startswith("ERROR: phase plan violates workspace policy")
    assert "17 executable leaves" in result
    assert "8-16" in result
    assert "[PHASE_PLAN_REPAIR_SCAFFOLD]" in result
    assert "12-14 leaves" in result
    assert "Do not oscillate" in result


def test_propose_phase_plan_accepts_thirteen_narrow_greenfield_leaves(tmp_path):
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "test_ws",
    }
    (tmp_path / "workspaces" / "test_ws").mkdir(parents=True, exist_ok=True)

    subtasks = [
        {
            "id": "setup",
            "title": "Initialize project structure",
            "goal": (
                "Create package, docs, and tests. Generated LLM code must resolve "
                "LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL, then fail or skip clearly when real "
                "credentials are absent."
            ),
            "files_to_create": [
                "pyproject.toml",
                "src/civgame/__init__.py",
                "docs/architecture.md",
                "tests/test_setup.py",
            ],
            "success_test": "python -m pytest tests/test_setup.py -q",
        }
    ]
    for idx in range(1, 12):
        subtasks.append(
            {
                "id": f"slice_{idx:02d}",
                "title": f"Build narrow GMAS game slice {idx}",
                "goal": "Implement one bounded FastAPI React GMAS game behavior.",
                "files_to_create": [
                    f"src/civgame/slice_{idx:02d}.py",
                    f"tests/test_slice_{idx:02d}.py",
                ],
                "success_test": f"python -m pytest tests/test_slice_{idx:02d}.py -q",
            }
        )
    subtasks.append(
        {
            "id": "final_smoke",
            "title": "Verify localhost smoke",
            "goal": "Run the integrated HTTP/WebSocket smoke gate.",
            "files_to_create": ["tests/integration/test_localhost_smoke.py"],
            "success_test": "python -m pytest tests/integration/test_localhost_smoke.py -q",
        }
    )

    result = _propose_phase_plan(
        ctx,
        plan={
            "summary": (
                "Large FastAPI React GMAS civilization game using the Umbrella "
                "LLM runtime env contract."
            ),
            "subtasks": subtasks,
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_captured_broad_leaf_before_submit(tmp_path):
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_3bbfc06b:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "test_ws",
    }
    (tmp_path / "workspaces" / "test_ws").mkdir(parents=True, exist_ok=True)

    plan = {
        "summary": (
            "FastAPI React TypeScript civilization game with GMAS/LLM bots. "
            "Generated code must resolve LLM_API_KEY, "
            "LLM_BASE_URL, and LLM_MODEL."
        ),
        "subtasks": [
            {
                "id": "setup",
                "title": "Initialize project structure",
                "goal": "Create package config, docs, and env contract test.",
                "files_to_create": [
                    "pyproject.toml",
                    "src/civgame/__init__.py",
                    "docs/architecture.md",
                    "tests/test_env_contract.py",
                ],
                "success_test": "python -m pytest tests/test_env_contract.py -q",
            },
            {
                "id": "turn_engine",
                "title": "Turn Engine",
                "goal": "Captured broad leaf from phase_web_3bbfc06b.",
                "files_to_create": [
                    "src/civgame/turns/state.py",
                    "src/civgame/turns/actions.py",
                    "src/civgame/turns/resolution.py",
                    "src/civgame/api/turns.py",
                    "frontend/src/components/TurnPanel.tsx",
                    "tests/test_turn_engine.py",
                ],
                "success_test": "python -m pytest tests/test_turn_engine.py -q",
            },
            {
                "id": "economy_state",
                "title": "Economy State",
                "goal": "Add resource ledger behavior.",
                "files_to_create": [
                    "src/civgame/economy/state.py",
                    "tests/test_economy_state.py",
                ],
                "success_test": "python -m pytest tests/test_economy_state.py -q",
            },
            {
                "id": "economy_agent",
                "title": "Economy Agent",
                "goal": "Add real LLM economy decision contract.",
                "files_to_create": [
                    "src/civgame/bot/economy.py",
                    "tests/test_economy_agent.py",
                ],
                "success_test": "python -m pytest tests/test_economy_agent.py -q",
            },
            {
                "id": "diplomacy_state",
                "title": "Diplomacy State",
                "goal": "Track relationships and offers.",
                "files_to_create": [
                    "src/civgame/diplomacy/state.py",
                    "tests/test_diplomacy_state.py",
                ],
                "success_test": "python -m pytest tests/test_diplomacy_state.py -q",
            },
            {
                "id": "diplomacy_agent",
                "title": "Diplomacy Agent",
                "goal": "Add real LLM diplomacy proposal contract.",
                "files_to_create": [
                    "src/civgame/bot/diplomacy.py",
                    "tests/test_diplomacy_agent.py",
                ],
                "success_test": "python -m pytest tests/test_diplomacy_agent.py -q",
            },
            {
                "id": "api_state",
                "title": "API State",
                "goal": "Expose current game state over HTTP.",
                "files_to_create": [
                    "src/civgame/api/state.py",
                    "tests/test_api_state.py",
                ],
                "success_test": "python -m pytest tests/test_api_state.py -q",
            },
            {
                "id": "final_smoke",
                "title": "Final localhost smoke",
                "goal": "Verify the integrated localhost game path.",
                "files_to_create": ["tests/integration/test_localhost_smoke.py"],
                "success_test": (
                    "python -m pytest tests/integration/test_localhost_smoke.py -q"
                ),
            },
        ],
    }

    result = _propose_phase_plan(ctx, plan=plan)

    assert result.startswith("ERROR: phase plan violates workspace policy")
    assert "too broad" in result
    assert "turn_engine (6 files)" in result
    assert "[PHASE_PLAN_REPAIR_SCAFFOLD]" in result
    assert "future/optional files" in result


def test_propose_phase_plan_accepts_split_version_of_captured_broad_leaf(tmp_path):
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_3bbfc06b:plan"
    ctx.current_task_type = "phase_run"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "test_ws",
    }
    (tmp_path / "workspaces" / "test_ws").mkdir(parents=True, exist_ok=True)

    subtasks = [
        {
            "id": "setup",
            "title": "Initialize project structure",
            "goal": (
                "Create package config and validate "
                "LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL."
            ),
            "files_to_create": [
                "pyproject.toml",
                "src/civgame/__init__.py",
                "docs/architecture.md",
                "tests/test_env_contract.py",
            ],
            "success_test": "python -m pytest tests/test_env_contract.py -q",
        },
        {
            "id": "turn_state",
            "title": "Turn State",
            "goal": "Model turn state transitions.",
            "files_to_create": [
                "src/civgame/turns/state.py",
                "src/civgame/turns/actions.py",
                "tests/test_turn_state.py",
            ],
            "success_test": "python -m pytest tests/test_turn_state.py -q",
        },
        {
            "id": "turn_api",
            "title": "Turn API",
            "goal": "Expose turn advancement over HTTP.",
            "files_to_create": [
                "src/civgame/api/turns.py",
                "tests/test_turn_api.py",
            ],
            "success_test": "python -m pytest tests/test_turn_api.py -q",
        },
        {
            "id": "turn_ui",
            "title": "Turn UI",
            "goal": "Render turn controls in React.",
            "files_to_create": [
                "frontend/src/components/TurnPanel.tsx",
                "frontend/src/components/TurnPanel.test.tsx",
            ],
            "success_test": "npm test -- TurnPanel.test.tsx --run",
        },
        {
            "id": "economy_agent",
            "title": "Economy Agent",
            "goal": "Add real LLM economy decisions.",
            "files_to_create": [
                "src/civgame/bot/economy.py",
                "tests/test_economy_agent.py",
            ],
            "success_test": "python -m pytest tests/test_economy_agent.py -q",
        },
        {
            "id": "diplomacy_agent",
            "title": "Diplomacy Agent",
            "goal": "Add real LLM diplomacy decisions.",
            "files_to_create": [
                "src/civgame/bot/diplomacy.py",
                "tests/test_diplomacy_agent.py",
            ],
            "success_test": "python -m pytest tests/test_diplomacy_agent.py -q",
        },
        {
            "id": "game_state_api",
            "title": "Game State API",
            "goal": "Serve synchronized game state.",
            "files_to_create": [
                "src/civgame/api/state.py",
                "tests/test_api_state.py",
            ],
            "success_test": "python -m pytest tests/test_api_state.py -q",
        },
        {
            "id": "final_smoke",
            "title": "Final localhost smoke",
            "goal": "Verify the integrated localhost game path.",
            "files_to_create": ["tests/integration/test_localhost_smoke.py"],
            "success_test": (
                "python -m pytest tests/integration/test_localhost_smoke.py -q"
            ),
        },
    ]

    result = _propose_phase_plan(
        ctx,
        plan={
            "summary": "FastAPI React TypeScript civilization game with GMAS/LLM bots.",
            "subtasks": subtasks,
        },
    )

    assert not result.startswith("ERROR:"), result


def test_palace_add_accepts_optional_metadata(tmp_path, monkeypatch):
    captured = {}

    def fake_save(ctx, **kwargs):
        captured.update(kwargs)
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "memory": {
                    "write_rules": {
                        "research_finding": {
                            "store": "palace.run",
                            "tier": "hot",
                            "scope": "run_scoped",
                        }
                    }
                },
                "exit_criteria": {
                    "min_palace_writes": [{"store": "palace.run", "n": 3}]
                },
            }
        },
    )
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research"}
    _append_phase_tool_row(
        drive,
        tool="github_project_search",
        result={
            "status": "ok",
            "results": [
                {
                    "full_name": "example/civ-pattern",
                    "html_url": "https://github.com/example/civ-pattern",
                }
            ],
        },
    )

    result = _palace_add(
        ctx,
        title="Finding",
        content="Concrete architecture note.",
        kind="research_finding",
        workspace_id="test_ws",
        source_id="github:example/civ-pattern",
        evidence_kind="observation",
        tags="research_finding",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["store"] == "palace.run"
    assert payload["tier"] == "hot"
    assert payload["verified"] is True
    assert payload["id"]
    assert captured["title"] == "Finding"
    assert captured["palace_path"] == "workspaces/test_ws/research"
    assert "Concrete architecture note." in captured["content"]
    assert "github:example/civ-pattern" in captured["content"]
    assert "observation" in captured["content"]
    assert "research_finding" in captured["tags"]
    stored = MemPalace(tmp_path, "test_ws").get(payload["id"])
    assert stored["verified"] is True

    recall = MemPalace(tmp_path, "test_ws").recall(
        "research",
        run_id="run-123",
        hot_rules=[{"store": "palace.run", "tags": ["research_finding"]}],
    )
    assert any("Concrete architecture note." in node["content"] for node in recall.hot)


def test_palace_add_research_defaults_concrete_observation_to_research_finding(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_save(ctx, **kwargs):
        captured.update(kwargs)
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "memory": {
                    "write_rules": {
                        "research_finding": {
                            "store": "palace.run",
                            "tier": "hot",
                            "scope": "run_scoped",
                        }
                    }
                },
                "exit_criteria": {
                    "min_palace_writes": [{"store": "palace.run", "n": 3}]
                },
            }
        },
    )
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research"}
    _append_phase_tool_row(
        drive,
        tool="search_gmas_knowledge",
        result={
            "query": "multi-agent LLM game bots",
            "recommended_pattern": "Use AgentProfile and MACPRunner",
        },
    )

    result = _palace_add(
        ctx,
        title="GMAS pattern",
        content="Use AgentProfile and MACPRunner for LLM bot turns.",
        workspace_id="test_ws",
        source_id="gmas:context",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "research_finding"
    assert payload["verified"] is True
    assert "research_finding" in payload["tags"]
    assert payload["store"] == "palace.run"
    assert captured["kind"] == "research_finding"
    assert "research_finding" in captured["tags"]
    stored = MemPalace(tmp_path, "test_ws").get(payload["id"])
    assert stored["verified"] is True
    recall = MemPalace(tmp_path, "test_ws").recall(
        "research",
        run_id="run-123",
        hot_rules=[{"store": "palace.run", "tags": ["research_finding"]}],
    )
    assert any("AgentProfile" in node["content"] for node in recall.hot)


def test_palace_add_research_progress_note_is_not_research_finding(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_save(ctx, **kwargs):
        captured.update(kwargs)
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _palace_add(
        ctx,
        title="Research scratchpad",
        content="Research progress: 1/3 palace findings saved.",
        workspace_id="test_ws",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "observation"
    assert "research_finding" not in payload["tags"]
    assert captured["kind"] == "observation"
    assert "research_finding" not in captured["tags"]
    recall = MemPalace(tmp_path, "test_ws").recall(
        "research",
        run_id="run-123",
        hot_rules=[{"store": "palace.run", "tags": ["research_finding"]}],
    )
    assert not recall.hot


def test_palace_add_research_continue_note_is_not_research_finding(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_save(ctx, **kwargs):
        captured.update(kwargs)
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_e8afe5ca:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _palace_add(
        ctx,
        content=(
            "I need to continue researching and make at least 3 palace_add "
            "calls before submit_research_summary. Let me explore more "
            "specific patterns for game AI and turn-based strategy."
        ),
        workspace_id="test_ws",
        source_id="ouros",
        evidence_kind="hypothesis",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "observation"
    assert "research_finding" not in payload["tags"]
    assert captured["kind"] == "observation"
    assert "research_finding" not in captured["tags"]
    recall = MemPalace(tmp_path, "test_ws").recall(
        "research",
        run_id="phase_web_e8afe5ca",
        hot_rules=[{"store": "palace.run", "tags": ["research_finding"]}],
    )
    assert not recall.hot


def test_palace_add_research_hypothesis_is_not_research_finding(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_save(ctx, **kwargs):
        captured.update(kwargs)
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_6c2e6608:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _palace_add(
        ctx,
        content="Looking for strategy games with AI bots or game frameworks...",
        workspace_id="test_ws",
        source_id="github_search",
        evidence_kind="hypothesis",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "observation"
    assert "research_finding" not in payload["tags"]
    assert captured["kind"] == "observation"


def test_palace_add_rejects_explicit_verified_false_research_finding(tmp_path):
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_20eb1a6a:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        content=(
            "Tussel and agentic-narrative-engine provide concrete references "
            "for turn-based Python games and LLM-driven game agents."
        ),
        kind="research_finding",
        tags="research_finding,github_discovery,llm_agents",
        verified=False,
    )

    assert result.startswith("ERROR:")
    assert "verified=false" in result


def test_palace_add_routes_plan_subtask_card_to_subtask_store(tmp_path, monkeypatch):
    captured = {}

    def fake_save(ctx, **kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "saved": True,
                "id": "drawer_subtask",
                "wing": "wing_test_ws",
                "hall": "hall_events",
                "room": "plan/subtasks",
            }
        )

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "memory": {
                    "write_rules": {
                        "subtask_card": {
                            "store": "palace.subtask",
                            "tier": "hot",
                            "scope": "subtask_scoped",
                        }
                    }
                }
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _palace_add(
        ctx,
        title="Subtask: game-state",
        content=json.dumps(
            {
                "id": "game-state",
                "title": "Build game state",
                "success_test": "python -m pytest tests/test_game_state.py -q",
            }
        ),
        kind="subtask_card",
        palace_path="workspaces/test_ws/plan/subtasks",
        workspace_id="test_ws",
        tags="subtask_card",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["store"] == "palace.subtask"
    assert payload["scope"] == "subtask_scoped"
    assert payload["subtask_id"] == "game-state"
    assert captured["palace_path"] == "workspaces/test_ws/plan/subtasks"
    assert captured["kind"] == "subtask_card"

    memories = MemPalace(tmp_path, "test_ws").list_all(stores=["palace.subtask"], n=10)
    assert any(
        item.get("subtask_id") == "game-state"
        and "Build game state" in str(item.get("content") or "")
        for item in memories
    )


def test_palace_add_defaults_plan_subtask_path_from_phase(tmp_path, monkeypatch):
    captured = {}

    def fake_save(ctx, **kwargs):
        captured.update(kwargs)
        return json.dumps({"saved": True, "id": "drawer", "room": "plan/subtasks"})

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "linear", "active_workspace_id": "test_ws"}

    result = _palace_add(
        ctx,
        title="Subtask: api",
        content=json.dumps({"id": "api", "title": "Backend API"}),
        kind="subtask_card",
        workspace_id="test_ws",
        tags="subtask_card",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert captured["palace_path"] == "workspaces/test_ws/plan/subtasks"


def test_palace_add_defaults_linear_execute_task_to_execute_path(tmp_path, monkeypatch):
    captured = {}

    def fake_save(ctx, **kwargs):
        captured.update(kwargs)
        return json.dumps({"saved": True, "id": "drawer", "room": "execute"})

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    drive = tmp_path / "workspaces" / "test_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:execute"
    ctx.loop_state_view = {"phase_label": "linear", "active_workspace_id": "test_ws"}

    result = _palace_add(
        ctx,
        title="Execution note",
        content="Implementation evidence should live under execute, not linear.",
        kind="execution_artifact",
        workspace_id="test_ws",
        tags="execution_artifact",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert captured["palace_path"] == "workspaces/test_ws/execute"


def test_palace_add_does_not_treat_missing_imports_plural_as_symbol_s(tmp_path, monkeypatch):
    def fake_save(ctx, **kwargs):
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "test_ws"}
    _append_phase_tool_row(
        drive,
        tool="read_file",
        result={"content": "Pytest collection reported failures due to missing imports."},
    )

    result = _palace_add(
        ctx,
        title="Import collection lead",
        content=(
            "Pytest collection reported failures due to missing imports; "
            "no concrete symbol has been isolated yet."
        ),
        kind="research_finding",
        workspace_id="test_ws",
        tags="research_finding",
        source_id="read_file",
    )

    payload = json.loads(result)
    assert payload["saved"] is True


def test_palace_add_rejects_research_llm_fallback_finding(tmp_path, monkeypatch):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "test_ws"}

    result = _palace_add(
        ctx,
        title="LLM failure handling",
        content=(
            "For LLM bot errors, add retry policy and fall-back behavior "
            "for economy decisions."
        ),
        kind="research_finding",
        workspace_id="test_ws",
        tags="research_finding",
    )

    assert result.startswith("ERROR:")
    assert "not saved" in result
    assert called is False


def test_palace_add_rejects_captured_research_fallback_with_linear_label(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_8b680883:research"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="Frontend + Backend Architecture",
        content=(
            "Bots can take 5-30 seconds per turn for LLM reasoning. "
            "Timeout handling: Fallback to simpler heuristic if LLM takes too long."
        ),
        kind="research_finding",
        workspace_id="civilization",
        tags="fastapi,websockets,react,architecture",
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result
    assert "not saved" in result
    assert called is False


def test_palace_add_rejects_captured_graceful_degradation_mock_mode(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_2dc4819e:research"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="LLM Runtime Environment Contract",
        content=(
            "The game uses GMAS for LLM-powered bots.\n"
            "Required variables: OUROBOROS_LLM_API_KEY / LLM_API_KEY, "
            "OUROBOROS_LLM_BASE_URL / LLM_BASE_URL, and "
            "OUROBOROS_MODEL / LLM_MODEL.\n"
            "Fallback Priority: check OUROBOROS_LLM_* variables first, then "
            "fall back to LLM_* credential aliases. If both are missing, fail "
            "with a clear error message.\n"
            "Implementation Requirements: Graceful Degradation: When LLM "
            "credentials are absent, provide mock/simulation mode for testing "
            "without paying for AI.\n"
            "Testing Strategy: Unit tests without real LLM (mock responses)."
        ),
        kind="research_finding",
        workspace_id="civilization",
        tags="environment,gmas,llm-credentials,testing",
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result
    assert "not saved" in result
    assert called is False


def test_palace_add_rejects_captured_mock_llm_behavior_verification(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_c8307523:research"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="LLM Bot Test Strategy",
        content=(
            "The game uses GMAS for LLM-powered civilization bots. "
            "Testing strategy combines unit tests, integration tests "
            "(mock LLM for bot behavior verification), and runtime validation."
        ),
        kind="research_finding",
        workspace_id="civilization",
        tags="research_finding,gmas,llm,testing",
    )

    assert result.startswith("ERROR:")
    assert "mock/fake/dry-run LLM" in result
    assert "not saved" in result
    assert called is False


def test_palace_add_rejects_captured_llm_decision_caching(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_14d924fc:research"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="LLM Bot Performance Mitigation",
        content=(
            "The game uses GMAS for LLM-powered civilization bots. "
            "Performance mitigation includes parallel processing, early "
            "stopping for no-action bots, and caching stable, unchanging "
            "decisions."
        ),
        kind="research_finding",
        workspace_id="civilization",
        tags="research_finding,gmas,llm,performance",
    )

    assert result.startswith("ERROR:")
    assert "cached decision/action/response reuse" in result
    assert "not saved" in result
    assert called is False


def test_palace_add_rejects_verified_web_search_source_without_success(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_65835290:research",
                "tool": "web_search",
                "result_preview": json.dumps(
                    {
                        "status": "provider_error",
                        "provider": "gmas_web_search",
                        "error": "TimeoutError",
                    }
                ),
            }
        )
        + "\n"
        + json.dumps(
            {
                "task_id": "phase_web_65835290:research",
                "tool": "web_search",
                "result_preview": (
                    "⚠️ TOOL_ARG_ERROR (web_search): _web_search() got an "
                    "unexpected keyword argument 'intent'"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_65835290:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="Web Stack Finding",
        content="FastAPI plus React is a reasonable web stack.",
        kind="research_finding",
        workspace_id="civilization",
        tags="research_finding,web-stack",
        source_id="web_search",
        evidence_kind="verified_outcome",
    )

    assert result.startswith("ERROR:")
    assert "did not succeed" in result
    assert called is False


def test_palace_add_accepts_web_search_source_with_sources_payload(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_65835290:research",
                "tool": "web_search",
                "args": {"query": "websocket game architecture"},
                "result_preview": json.dumps(
                    {
                        "provider": "gmas_web_search",
                        "query": "websocket game architecture",
                        "sources": [
                            {
                                "title": "WebSocket game architecture",
                                "url": "https://example.test/ws-game",
                            }
                        ],
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_65835290:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="WebSocket Game Architecture",
        content="WebSocket game servers use a persistent bidirectional protocol.",
        kind="research_finding",
        workspace_id="civilization",
        tags="research_finding,websocket",
        source_id="web_search:websocket game architecture",
        evidence_kind="verified_outcome",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["source_path"] == "web_search:websocket game architecture"
    assert called is True


def test_palace_add_accepts_verified_mcp_source_after_nonempty_success(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_65835290:research",
                "tool": "mcp_discover",
                "args": {"query": "file data analysis web requests"},
                "result_preview": json.dumps(
                    {
                        "status": "ok",
                        "query": "file data analysis web requests",
                        "results": [
                            {
                                "name": "filesystem-plus-fetch",
                                "url": "https://example.test/mcp/files-web",
                            }
                        ],
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_65835290:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _palace_add(
        ctx,
        title="MCP Discovery Finding",
        content="MCP discovery completed with no required external server.",
        kind="research_finding",
        workspace_id="civilization",
        tags="research_finding,mcp",
        source_id="mcp_discover:file data analysis web requests",
        evidence_kind="verified_outcome",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["source_path"] == "mcp_discover:file data analysis web requests"
    assert called is True


def test_palace_add_accepts_protective_no_fallback_with_linear_label(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_8b680883:research"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }
    _append_phase_tool_row(
        drive,
        task_id="phase_web_8b680883:research",
        tool="search_gmas_knowledge",
        result={
            "query": "LLM bot error handling",
            "recommended_pattern": "Pause the turn and surface retry controls.",
        },
    )

    result = _palace_add(
        ctx,
        title="LLM error handling",
        content=(
            "LLM failures pause the bot turn and surface retry/skip/pause choices. "
            "No automatic fallback to heuristics or cached decisions is allowed."
        ),
        kind="research_finding",
        workspace_id="civilization",
        tags="research_finding,error-handling",
        source_id="gmas:error-handling",
    )

    assert result.startswith("{")
    assert called is True


def test_palace_add_rejects_research_llm_env_contract_without_ouroboros_alias(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "test_ws"}
    _append_phase_tool_row(
        drive,
        tool="search_gmas_knowledge",
        result={
            "query": "Umbrella LLM runtime aliases",
            "recommended_pattern": "Use OUROBOROS_* and LLM_* aliases.",
        },
    )

    result = _palace_add(
        ctx,
        title="LLM runtime",
        content=(
            "The game bots use an OpenAI/OpenRouter compatible provider via "
            "LLM_API_KEY for real LLM decisions."
        ),
        kind="research_finding",
        workspace_id="test_ws",
        tags="research_finding",
        source_id="gmas:runtime-aliases",
    )

    assert result.startswith("ERROR:")
    assert "LLM_BASE_URL" in result
    assert "LLM_MODEL" in result
    assert "not saved" in result
    assert called is False


def test_palace_add_accepts_research_llm_env_contract_with_ouroboros_alias(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "test_ws"}
    _append_phase_tool_row(
        drive,
        tool="search_gmas_knowledge",
        result={
            "query": "Umbrella LLM runtime aliases",
            "recommended_pattern": "Use OUROBOROS_* and LLM_* aliases.",
        },
    )

    result = _palace_add(
        ctx,
        title="LLM runtime",
        content=(
            "The game bots resolve LLM_API_KEY, "
            "LLM_BASE_URL, and LLM_MODEL "
            "from the inherited Umbrella runtime."
        ),
        kind="research_finding",
        workspace_id="test_ws",
        tags="research_finding",
        source_id="gmas:runtime-aliases",
    )

    assert result.startswith("{")
    assert called is True


def test_palace_add_rejects_protective_unsupported_model_alias_note(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_92978867:research"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }
    _append_phase_tool_row(
        drive,
        task_id="phase_web_92978867:research",
        tool="search_gmas_knowledge",
        result={
            "query": "Umbrella unsupported model aliases",
            "recommended_pattern": "Use OUROBOROS_MODEL rather than deprecated aliases.",
        },
    )

    result = _palace_add(
        ctx,
        title="LLM runtime alias guard",
        content=(
            "For model selection, the generated workspace must not use "
            "OUROBOROS_LLM_MODEL; that spelling is unsupported by Umbrella."
        ),
        kind="research_finding",
        workspace_id="civilization",
        tags="research_finding,llm-runtime",
        source_id="gmas:runtime-aliases",
    )

    assert result.startswith("ERROR:")
    assert "OUROBOROS_LLM_MODEL" in result
    assert called is False


def test_palace_add_accepts_domain_research_without_repeating_llm_env_contract(
    tmp_path, monkeypatch
):
    called = False

    def fake_save(ctx, **kwargs):
        nonlocal called
        called = True
        return "OK: memory saved"

    monkeypatch.setattr(
        "ouroboros.tools.phase_contract.umbrella_tools.save_umbrella_memory",
        fake_save,
    )
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_fa9a4d2c:research"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }
    _append_phase_tool_row(
        drive,
        task_id="phase_web_fa9a4d2c:research",
        tool="search_gmas_knowledge",
        result={
            "query": "simplified Civilization LLM bot tools",
            "recommended_pattern": "LLM calls typed action tools for game decisions.",
        },
    )

    result = _palace_add(
        ctx,
        title="Game Mechanics Scope for Simplified Civilization",
        content=(
            "Turn-based with explicit turn passing. Each turn has 3 action "
            "points, a 12x12 map, food/production/gold/science resources, "
            "and a single human player vs a single AI bot. Bot decision flow: "
            "the LLM analyzes game state, then LLM calls action tools such as "
            "trade_proposal, move_unit, and build_structure."
        ),
        kind="research_finding",
        workspace_id="civilization",
        tags="game-mechanics,turn-based,resources,win-conditions",
        source_id="gmas:game-mechanics",
    )

    assert result.startswith("{")
    assert "omits the standalone LLM runtime env contract" not in result
    assert called is True


def test_palace_add_rejects_contradicted_class_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "test_ws"}

    result = _palace_add(
        ctx,
        title="Bot tools",
        content=(
            "Current backend/bots/bot_tools.py contains GetGameStateTool "
            "class and get_game_state_tool instance."
        ),
        kind="research_finding",
        workspace_id="test_ws",
        tags="research_finding",
    )

    assert result.startswith("ERROR:")
    assert "does not contain that class definition" in result


def test_palace_add_rejects_unread_current_workspace_file_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "requirements.txt").write_text(
        "fastapi==0.115.0\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
    )
    ctx.task_id = "run-123:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": "test_ws"}

    result = _palace_add(
        ctx,
        title="Verified stack",
        content="requirements.txt confirms the FastAPI dependency.",
        kind="research_finding",
        workspace_id="test_ws",
        tags="research_finding",
    )

    assert result.startswith("ERROR:")
    assert "not read in this phase" in result


def test_propose_phase_plan_rejects_stale_import_error_task(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-import-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ST1",
                    "title": "Fix get_game_state_tool import error",
                    "goal": (
                        "Fix ImportError by ensuring get_game_state_tool is "
                        "properly exported from backend/bots/bot_tools.py"
                    ),
                    "files_expected_to_change": ["backend/bots/bot_tools.py"],
                    "success_test": {
                        "type": "command",
                        "command": (
                            "python -c \"from backend.bots.bot_tools import "
                            "get_game_state_tool; print('OK')\""
                        ),
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "get_game_state_tool" in result
    assert "contains that symbol" in result


def test_propose_phase_plan_rejects_resolve_import_symbol_task(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-import-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ST1",
                    "title": "Resolve bot_tools import get_game_state_tool",
                    "goal": "Resolve bot_tools import get_game_state_tool.",
                    "files_expected_to_change": ["backend/bots/bot_tools.py"],
                    "success_test": {
                        "type": "command",
                        "command": (
                            "python -c \"from backend.bots.bot_tools import "
                            "get_game_state_tool; print('OK')\""
                        ),
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "get_game_state_tool" in result
    assert "contains that symbol" in result


def test_propose_phase_plan_rejects_missing_import_or_implement_task(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-import-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ST1",
                    "title": "Fix Missing Import in bot_tools.py",
                    "goal": "Add missing get_game_state_tool import or implement the function.",
                    "files_expected_to_change": ["backend/bots/bot_tools.py"],
                    "success_test": {
                        "type": "command",
                        "command": (
                            "python -c \"from backend.bots.bot_tools import "
                            "get_game_state_tool; print('OK')\""
                        ),
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "get_game_state_tool" in result
    assert "contains that symbol" in result


def test_propose_phase_plan_rejects_implement_missing_existing_symbol_task(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-import-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ST1",
                    "title": "Fix bot tools",
                    "goal": "Implement missing get_game_state_tool and other GMAS tools.",
                    "files_expected_to_change": ["backend/bots/bot_tools.py"],
                    "success_test": {
                        "type": "command",
                        "command": (
                            "python -c \"from backend.bots.bot_tools import "
                            "get_game_state_tool; print('OK')\""
                        ),
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "get_game_state_tool" in result
    assert "contains that symbol" in result


def test_propose_phase_plan_rejects_ensure_exported_existing_symbol_task(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-import-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ST1",
                    "title": "Fix bot_tools import error",
                    "description": (
                        "Resolve ImportError in backend/bots/bot_tools.py "
                        "and ensure get_game_state_tool is properly exported"
                    ),
                    "files_expected_to_change": ["backend/bots/bot_tools.py"],
                    "success_test": {
                        "type": "command",
                        "command": (
                            "python -c \"from backend.bots.bot_tools import "
                            "get_game_state_tool; print('OK')\""
                        ),
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "get_game_state_tool" in result
    assert "contains that symbol" in result


def test_propose_phase_plan_rejects_fix_init_with_existing_param_task(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        self.ai_controller = ai_controller\n",
        encoding="utf-8",
    )
    (workspace / "main.py").write_text(
        "def create(game, ai_controller):\n"
        "    return GameEngine(game, ai_controller)\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-init-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ST1",
                    "title": "Fix GameEngine initialization with ai_controller parameter",
                    "goal": (
                        "Examine GameEngine class definition and update __init__() "
                        "to accept ai_controller parameter."
                    ),
                    "files_expected_to_change": ["game_engine.py", "main.py"],
                    "success_test": {
                        "type": "command",
                        "command": "python -c \"from game_engine import GameEngine\"",
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "already shows that parameter" in result


def test_propose_phase_plan_rejects_ensure_handles_existing_optional_param(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        self.ai_controller = ai_controller\n",
        encoding="utf-8",
    )
    (workspace / "main.py").write_text(
        "def create(game, ai_controller):\n"
        "    return GameEngine(game, ai_controller)\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-init-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ST1",
                    "title": "Fix GameEngine initialization in API",
                    "description": (
                        "Ensure GameEngine properly handles optional "
                        "ai_controller parameter in /api/game/create endpoint"
                    ),
                    "files_expected_to_change": ["game_engine.py", "main.py"],
                    "success_test": {
                        "type": "command",
                        "command": "python -c \"from game_engine import GameEngine\"",
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "already shows that parameter" in result


def test_propose_phase_plan_rejects_stale_dependency_wiring_task(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "main.py").write_text(
        "from game_engine import GameEngine\n"
        "from game_core.llm_agent import AIController\n"
        "def create(game):\n"
        "    ai_controller = AIController(game)\n"
        "    return GameEngine(game, ai_controller)\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-wiring-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ST1",
                    "title": "Fix GameEngine initialization in API endpoint",
                    "goal": (
                        "Fix /api/game/create endpoint to properly pass "
                        "AIController to GameEngine constructor"
                    ),
                    "files_expected_to_change": ["main.py"],
                    "success_test": {
                        "type": "command",
                        "command": (
                            "python -c \"from main import create; "
                            "print('OK')\""
                        ),
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "already passes" in result


def test_propose_phase_plan_rejects_nested_stale_runtime_blockers(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        self.ai_controller = ai_controller\n",
        encoding="utf-8",
    )
    (workspace / "main.py").write_text(
        "def create(game, ai_controller):\n"
        "    return GameEngine(game, ai_controller)\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-nested-plan",
            "workspace_id": "test_ws",
            "phases": [
                {
                    "name": "Critical Fixes",
                    "subtasks": [
                        {
                            "id": "fix_import_errors",
                            "title": "Fix Import Errors in bot_tools.py",
                            "goal": (
                                "Resolve missing get_game_state_tool import "
                                "causing test collection failure"
                            ),
                            "files_expected_to_change": ["backend/bots/bot_tools.py"],
                            "success_test": (
                                "python -c \"from backend.bots.bot_tools "
                                "import get_game_state_tool\""
                            ),
                        },
                        {
                            "id": "fix_gameengine_init",
                            "title": "Fix GameEngine Initialization Missing Arguments",
                            "goal": (
                                "Provide missing ai_controller argument to "
                                "GameEngine initialization causing API 500 errors"
                            ),
                            "files_expected_to_change": ["game_engine.py", "main.py"],
                            "success_test": "python -c \"from game_engine import GameEngine\"",
                        },
                    ],
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "get_game_state_tool" in result or "ai_controller" in result


def test_propose_phase_plan_rejects_stale_blocker_in_notes(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        self.ai_controller = ai_controller\n",
        encoding="utf-8",
    )
    (workspace / "main.py").write_text(
        "def create(game, ai_controller):\n"
        "    return GameEngine(game, ai_controller)\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "notes-bad",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "verify-current-engine",
                    "goal": "Verify current game engine construction path.",
                    "success_test": "python -c \"from game_engine import GameEngine\"",
                }
            ],
        },
        notes="Current issues addressed: GameEngine.__init__ missing ai_controller argument.",
    )

    assert result.startswith("ERROR:")
    assert "ai_controller" in result


def test_submit_phase_plan_rejects_stale_blocker_from_existing_proposal(tmp_path):
    from ouroboros.tools.phase_control import _submit_phase_plan

    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    (drive / "logs").mkdir(parents=True)
    state.mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "plan_id": "bad-existing-plan",
                "plan": {
                    "plan_id": "bad-existing-plan",
                    "subtasks": [
                        {
                            "id": "fix_import_errors",
                            "goal": (
                                "Resolve missing get_game_state_tool import "
                                "causing test collection failure"
                            ),
                            "success_test": (
                                "python -c \"from backend.bots.bot_tools "
                                "import get_game_state_tool\""
                            ),
                        }
                    ],
                },
                "notes": "ready",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _submit_phase_plan(ctx)

    assert result.startswith("ERROR:")
    assert "get_game_state_tool" in result


def test_submit_phase_plan_rejects_unknown_palace_id_after_review_revision(tmp_path):
    from ouroboros.tools.phase_control import _submit_phase_plan

    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    (drive / "logs").mkdir(parents=True)
    state.mkdir(parents=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "created_at": 1.0,
                "task_id": "run-123:plan",
                "workspace_id": "test_ws",
                "run_id": "run-123",
                "plan_id": "old-plan",
                "plan": {
                    "plan_id": "old-plan",
                    "subtasks": [
                        {
                            "id": "build",
                            "title": "Build",
                            "files_to_create": ["src/test_ws/app.py"],
                            "success_test": "python -m pytest tests/test_app.py -q",
                        }
                    ],
                },
                "notes": "old accepted plan",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            "Add Phase 0 Project Initialization with pyproject.toml"
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    unknown = _submit_phase_plan(ctx, plan_id="memory-palace-id")
    stale = _submit_phase_plan(ctx)

    assert unknown.startswith("ERROR:")
    assert "Unknown plan_id" in unknown
    assert stale.startswith("ERROR:")
    assert "review revision" in stale


def test_propose_phase_plan_rejects_unaddressed_review_revision(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "subtask_06: Replace 'ActionPanel has buttons for "
                                "build, move, diplomacy actions' with chat-based "
                                "player input and LLM action suggestions"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_06",
                    "title": "Build UI",
                    "goal": "Create ActionPanel controls.",
                    "success_test": "npm run build",
                    "acceptance_criteria": [
                        "ActionPanel has buttons for build, move, diplomacy actions",
                    ],
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "review revision appears unaddressed" in result
    assert "chat" in result


def test_propose_phase_plan_accepts_addressed_review_revision(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "subtask_06: Replace 'ActionPanel has buttons for "
                                "build, move, diplomacy actions' with chat-based "
                                "player input and LLM action suggestions"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "good-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_06",
                    "title": "Build natural-language game UI",
                    "goal": (
                        "Create chat-based player input with LLM action "
                        "suggestions for economy, movement, and diplomacy."
                    ),
                    "success_test": "npm run build",
                    "acceptance_criteria": [
                        "Player input is chat-based natural language.",
                        "LLM action suggestions are shown before sending actions.",
                    ],
                }
            ],
        },
    )

    assert result.startswith("OK:")


def test_propose_phase_plan_does_not_treat_react_18_as_subtask_target(tmp_path):
    from umbrella.deep_agent_tools.phase_contract_revisions import _revision_target_ids

    revision = (
        "Add 'phase_5_frontend_ui' with subtasks for: React 18 + TypeScript + "
        "Vite setup, map visualization component, stats display component, "
        "player controls, and WebSocket event handling"
    )

    assert _revision_target_ids(revision) == []

    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {"revisions": [revision]},
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "react-18-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_5",
                    "title": "phase_5_frontend_ui",
                    "goal": (
                        "Build React 18 TypeScript Vite setup with map "
                        "visualization component, stats display component, "
                        "player controls, and WebSocket event handling."
                    ),
                    "files_to_create": [
                        "frontend/package.json",
                        "frontend/src/App.tsx",
                    ],
                    "success_test": "npm --prefix frontend run build",
                },
                {
                    "id": "subtask_18",
                    "title": "phase_6_integration_docs",
                    "goal": "Write integration documentation and final test coverage.",
                    "files_to_create": [
                        "docs/architecture.md",
                        "tests/test_integration.py",
                    ],
                    "success_test": "python -m pytest tests/test_integration.py -q",
                },
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_accepts_env_revision_without_optional_wording(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Move .env.example creation to subtask_01_01 "
                                "(or create new env_config subtask in Phase 1) "
                                "with required variables: LLM_API_KEY, "
                                "LLM_BASE_URL, LLM_MODEL"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "env-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_01_01_setup",
                    "title": "Project setup and LLM environment contract",
                    "goal": (
                        "Create .env.example and config that resolves "
                        "LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL."
                    ),
                    "files_to_create": [
                        ".env.example",
                        "src/test_ws/config/llm.py",
                        "tests/test_llm_config.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_config.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_rejects_captured_real_env_file_create(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_01e792f4:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-env-file-create",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "subtask_009",
                    "title": "Frontend-backend integration and localhost deployment",
                    "goal": (
                        "Wire the frontend to the backend using inherited "
                        "Umbrella runtime configuration without writing secrets."
                    ),
                    "files_to_create": [
                        "src/civilization/main.py",
                        "frontend/.env",
                        "tests/test_009_integration.py",
                        "docs/local_run.md",
                    ],
                    "success_test": (
                        "python -m pytest tests/test_009_integration.py -q"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:"), result
    assert "protected secret/env workspace path" in result
    assert "frontend/.env" in result


def test_propose_phase_plan_matches_st_prefixed_revision_subtask_ids(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Add a new subtask 'st-01b: Core Data Model Design' "
                                "after st-01 to define game state structure, "
                                "Python dataclasses, TypeScript interfaces, and "
                                "the communication contract before other subtasks "
                                "depend on these types"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "st-revision-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "st-01",
                    "title": "Project setup",
                    "goal": "Create package structure.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/test_ws/__init__.py",
                        "tests/test_setup.py",
                    ],
                    "success_test": "python -m pytest tests/test_setup.py -v",
                },
                {
                    "id": "st-01b",
                    "title": "Core Data Model Design",
                    "goal": (
                        "Define game state structure, Python dataclasses, "
                        "TypeScript interfaces, and communication contract."
                    ),
                    "files_to_create": [
                        "src/test_ws/models/game_state.py",
                        "frontend/src/types/game.ts",
                        "docs/data_contract.md",
                        "tests/test_data_contract.py",
                    ],
                    "success_test": "python -m pytest tests/test_data_contract.py -v",
                },
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_ignores_illustrative_revision_numbers(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Subtask 5.1 still missing complete `test_strategy` "
                                "and `acceptance_criteria` fields - the JSON artifact "
                                "currently only has `goal`, `files_to_create`, and "
                                "`success_test` but lacks the detailed verification "
                                "specification that other subtasks like 1.0, 1.1, "
                                "1.2 include. The revision requires these fields to "
                                "specify exactly how verify_coverage.py validates "
                                "coverage >= 80% via pytest-cov JSON output."
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "coverage-revision-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_5_1",
                    "title": "Coverage verification",
                    "goal": "Validate coverage for src/civbackend.",
                    "files_to_create": ["tests/verify_coverage.py"],
                    "success_test": "python tests/verify_coverage.py",
                    "test_strategy": (
                        "tests/verify_coverage.py runs pytest with "
                        "--cov=src/civbackend --cov-report=json, reads "
                        ".coverage.json via Python, and fails when total "
                        "coverage is below 80%."
                    ),
                    "acceptance_criteria": [
                        "Coverage JSON output exists and is parsed by Python.",
                        "src/civbackend coverage is >= 80%.",
                    ],
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_expands_hyphenated_revision_ranges(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "subtask_11-14: Add the test files referenced "
                                "in success_test (test_map_types.py, "
                                "test_ui_types.py, test_api_client_types.py, "
                                "test_scene_types.py) to files_to_create for "
                                "each subtask"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "range-revision-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_11",
                    "title": "Map type tests",
                    "goal": "Add map type validation.",
                    "files_to_create": ["tests/test_map_types.py"],
                    "success_test": "python -m pytest tests/test_map_types.py -v",
                },
                {
                    "id": "subtask_12",
                    "title": "UI type tests",
                    "goal": "Add UI type validation.",
                    "files_to_create": ["tests/test_ui_types.py"],
                    "success_test": "python -m pytest tests/test_ui_types.py -v",
                },
                {
                    "id": "subtask_13",
                    "title": "API client type tests",
                    "goal": "Add API client type validation.",
                    "files_to_create": ["tests/test_api_client_types.py"],
                    "success_test": "python -m pytest tests/test_api_client_types.py -v",
                },
                {
                    "id": "subtask_14",
                    "title": "Scene type tests",
                    "goal": "Add scene type validation.",
                    "files_to_create": ["tests/test_scene_types.py"],
                    "success_test": "python -m pytest tests/test_scene_types.py -v",
                },
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_accepts_frontend_command_revision_without_filler_words(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Fix subtask_04_04 verification: replace "
                                "'python -m pytest tests/frontend/test_websocket_client.py' "
                                "with proper frontend test command using "
                                "'cd frontend && npm test --' or equivalent"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "frontend-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_04_04_websocket_client",
                    "title": "Frontend WebSocket client test",
                    "goal": "Test the TypeScript WebSocket client in the frontend package.",
                    "files_to_create": ["frontend/src/services/gameSocket.test.ts"],
                    "success_test": "cd frontend && npm test -- gameSocket.test.ts --run",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_does_not_require_removed_timeout_number(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Update subtask 9 verification: remove "
                                "'--timeout=30' flag to avoid pytest-timeout "
                                "requirement, use 'pytest tests/test_e2e.py -v' instead"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "remove-timeout-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_9_e2e",
                    "title": "End-to-end verification",
                    "goal": "Run the E2E game flow.",
                    "files_to_create": ["tests/test_e2e.py"],
                    "success_test": "pytest tests/test_e2e.py -v",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_handles_add_subtask_after_reference_as_global(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                            "Add environment documentation subtask after subtask 2: "
                            "create/update .env.example with explicit credential "
                            "requirements (OUROBOROS_LLM_API_KEY, "
                            "OUROBOROS_LLM_BASE_URL, OUROBOROS_MODEL) and "
                            "fallbacks to LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "add-after-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_2_domain",
                    "title": "Domain model",
                    "goal": "Create domain model.",
                    "files_to_create": ["src/test_ws/domain.py", "tests/test_domain.py"],
                    "success_test": "python -m pytest tests/test_domain.py -q",
                },
                {
                    "id": "subtask_3_environment_docs",
                    "title": "Environment documentation and config",
                        "goal": (
                            "Create .env.example documenting credential requirements "
                            "for LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL aliases."
                        ),
                    "files_to_create": [".env.example", "tests/test_env_docs.py"],
                    "success_test": "python -m pytest tests/test_env_docs.py -q",
                },
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_ignores_consider_revision_as_optional(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Consider splitting Phase 4 or adding mid-phase "
                                "checkpoint for frontend work"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "optional-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "frontend",
                    "title": "Build frontend",
                    "goal": "Create the playable frontend.",
                    "files_to_create": ["frontend/src/App.tsx"],
                    "success_test": "cd frontend && npm run build",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_accepts_arrow_revision_when_remaining_keyword_is_present(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Add Graph context diagram between GMAS nodes: "
                                "specialists -> DecisionAggregator -> "
                                "ActionExecutor -> GameEngine"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "arrow-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "topology",
                    "title": "Document agent topology",
                    "goal": (
                        "Add a graph context diagram showing DecisionAggregator, "
                        "ActionExecutor, and GameEngine flow."
                    ),
                    "success_test": "npm run build",
                }
            ],
        },
    )

    assert result.startswith("OK:")


def test_propose_phase_plan_accepts_space_separated_subtask_number_revision(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Add explicit LLM configuration section to subtask "
                                "010: document how to set OUROBOROS_LLM_API_KEY/"
                                "LLM_API_KEY, LLM_BASE_URL, "
                                "LLM_MODEL, and list supported providers"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "subtask-space-number",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_010",
                    "title": "Document LLM configuration",
                    "goal": (
                        "Document how to set LLM_API_KEY, "
                        "LLM_BASE_URL, "
                        "LLM_MODEL, and supported providers."
                    ),
                    "files_to_create": [
                        "docs/llm-config.md",
                        "tests/test_documentation.py",
                    ],
                    "success_test": "python -m pytest tests/test_documentation.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_matches_numbered_hyphenated_subtask_revision(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Expand subtask 02-gmas-bot-framework with "
                                "specific tool signatures for propose_treaty "
                                "and evaluate_trade"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "hyphenated-subtask-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "02-gmas-bot-framework",
                    "title": "Build GMAS bot framework",
                    "goal": (
                        "Expand the bot framework with specific tool signatures "
                        "for propose_treaty and evaluate_trade."
                    ),
                    "files_to_create": [
                        "src/test_ws/agents/bots.py",
                        "tests/test_agents.py",
                    ],
                    "success_test": "python -m pytest tests/test_agents.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_accepts_numeric_id_for_subtask_revision(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Fix subtask 5 success_test: use actual build "
                                "verification with TypeScript compilation and "
                                "frontend build"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "numeric-id-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": 5,
                    "title": "Frontend build verification",
                    "goal": (
                        "Verify TypeScript compilation and frontend build "
                        "with a checked-in pytest wrapper."
                    ),
                    "files_to_create": ["tests/verify_frontend_build.py"],
                    "success_test": "python -m pytest tests/verify_frontend_build.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_accepts_rename_revision_without_old_typo(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            "Typo: Rename `subtest_e2e_script` to `subtask_e2e_script`"
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "rename-review-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_e2e_script",
                    "title": "Build E2E script",
                    "goal": "Exercise HTTP and WebSocket behavior through a checked-in test.",
                    "files_to_create": ["tests/test_e2e.py"],
                    "success_test": "python -m pytest tests/test_e2e.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_accepts_decimal_subtask_revision_targets(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Add explicit frontend test files to subtasks "
                                "3.2-3.5: frontend/src/components/__tests__/"
                                "*.test.tsx and frontend/src/hooks/__tests__/"
                                "*.test.ts"
                            ),
                            (
                                "Add transaction rollback test to subtask 4.2 "
                                "acceptance criteria: Invalid bot action causes "
                                "transaction rollback without state corruption"
                            ),
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "decimal-review-targets",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "3.2",
                    "title": "Game board",
                    "goal": (
                        "Implement GameBoard with frontend component tests in "
                        "frontend/src/components/__tests__/GameBoard.test.tsx."
                    ),
                    "success_test": "cd frontend && npm run test -- GameBoard",
                },
                {
                    "id": "3.5",
                    "title": "WebSocket hooks",
                    "goal": (
                        "Implement useWebSocket with frontend hook tests in "
                        "frontend/src/hooks/__tests__/useWebSocket.test.ts."
                    ),
                    "success_test": "cd frontend && npm run test -- useWebSocket",
                },
                {
                    "id": "4.2",
                    "title": "Turn cycle",
                    "goal": "Implement GMAS turn cycle orchestration.",
                    "files_to_create": ["tests/integration/test_turn_cycle.py"],
                    "success_test": (
                        "python -m pytest tests/integration/test_turn_cycle.py -q"
                    ),
                    "acceptance_criteria": (
                        "Invalid bot action causes transaction rollback without "
                        "state corruption."
                    ),
                },
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_ignores_phase_numbers_in_revision_targets(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Phase 3 (st_3_1): Clarify GMAS graph topology - "
                                "Specify how economy/diplomacy/military agents "
                                "interact in the graph and how decisions converge "
                                "into a single game action."
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "phase-number-reference-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "st_3_1",
                    "title": "Clarify GMAS graph topology",
                    "goal": (
                        "Specify that economy, diplomacy, and military agents "
                        "interact in a parallel graph and converge into a single "
                        "game action through an arbitration reducer."
                    ),
                    "files_to_create": ["docs/agent_topology.md"],
                    "files_to_change": ["tests/test_agent_topology.py"],
                    "success_test": "python -m pytest tests/test_agent_topology.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_still_enforces_semantic_revision_numbers(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Update subtask_2 runtime retry policy: retry 3 "
                                "times before pausing the bot turn"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "semantic-number-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_2",
                    "title": "Runtime retry policy",
                    "goal": (
                        "Retry transient LLM failures before pausing the bot turn."
                    ),
                    "files_to_create": ["src/test_ws/llm_runtime.py"],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:"), result
    assert "missing number(s): 3" in result


def test_propose_phase_plan_accepts_success_test_quality_revision_when_behavioral(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            "Strengthen success tests to verify behavior, not just file existence"
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "behavioral-success-tests",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "game_state_behavior",
                    "title": "Test game state behavior",
                    "goal": "Implement and test turn/resource behavior.",
                    "files_to_create": [
                        "backend/models/game_state.py",
                        "tests/test_game_state.py",
                    ],
                    "success_test": "python -m pytest tests/test_game_state.py -q",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_cross_platform_success_test_revision(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Fix subtask_1_3 success test for cross-platform "
                                "compatibility: replace 'test -d dist' with "
                                "'exists dir dist' (Python) or provide a "
                                "platform-appropriate command"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "cross-platform-success-test-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_1_3",
                    "title": "Frontend build setup",
                    "goal": "Configure Vite frontend build.",
                    "files_to_create": ["frontend/package.json"],
                    "success_test": "npm --prefix frontend run build",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_does_not_block_optional_polish_revision(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Optional polish: clarify that llm_config.py is "
                                "Python via a brief docstring"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "optional-polish-retry",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_1_1",
                    "title": "Runtime resolver",
                    "goal": "Implement LLM runtime alias resolver.",
                    "files_to_create": [
                        "src/test_ws/llm_config.py",
                        "tests/test_llm_config.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_config.py -q",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_allows_scripts_for_verification_helpers(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "verification-scripts-root",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_check_env",
                    "title": "Add runtime env verification",
                    "goal": "Check runtime aliases before localhost startup.",
                    "files_to_create": [
                        "src/test_ws/llm_config.py",
                        "scripts/check_env.py",
                    ],
                    "success_test": "python scripts/check_env.py",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_quoted_added_subtask_revision(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Add 'create_bot_tools' subtask to Backend Game "
                                "Engine phase for game action tools (@tool decorator)"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "quoted-added-subtask",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "create_bot_tools",
                    "title": "Create bot tools",
                    "goal": (
                        "Implement backend game action tools with @tool "
                        "decorators for GMAS agents."
                    ),
                    "files_to_create": [
                            "src/test_ws/agents/bot_tools.py",
                        "tests/test_bot_tools.py",
                    ],
                    "success_test": "python -m pytest tests/test_bot_tools.py -q",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_bare_workspace_verify_for_build_subtask(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "generic-verify",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "build_engine",
                    "title": "Build engine",
                    "goal": "Implement game engine.",
                    "success_test": "run_workspace_verify",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "bare `run_workspace_verify`" in result


def test_propose_phase_plan_rejects_bare_workspace_verify_for_deployment_subtask(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_f0cee725:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-bare-deployment-verify",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "localhost-deployment",
                    "title": "Localhost Deployment",
                    "goal": "Deploy and verify localhost game server.",
                    "files_to_create": ["tests/test_localhost_deployment.py"],
                    "success_test": "run_workspace_verify",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "bare `run_workspace_verify`/`run_unit_tests`" in result
    assert "localhost-deployment" in result


def test_propose_phase_plan_rejects_bare_unit_tests_for_build_subtask(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_211b9e5b:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-bare-unit-tests",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "subtask_1_domain",
                    "title": "Implement core game domain models",
                    "goal": "Implement domain models under src/.",
                    "files_to_create": [
                        "src/test_ws/models.py",
                        "tests/test_models.py",
                    ],
                    "success_test": "run_unit_tests",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "bare `run_workspace_verify`/`run_unit_tests`" in result
    assert "subtask_1_domain" in result


def test_propose_phase_plan_rejects_captured_shell_masked_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_f0cee725:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-masked-pytest",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "project-setup",
                    "title": "Project setup",
                    "goal": "Create package skeleton and import tests.",
                    "files_to_create": [
                        "src/test_ws/__init__.py",
                        "tests/test_pkg_imports.py",
                    ],
                    "success_test": (
                        "python -m pytest tests/test_pkg_imports.py -q || true"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "masks command failure" in result
    assert "project-setup" in result


def test_propose_phase_plan_rejects_captured_devnull_or_build_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_b34a047f:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "civilization"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "phase_plan:project-setup-docs",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "project-setup-docs",
                    "title": "Create project structure with docs and scaffolding",
                    "goal": (
                        "Set up src/civlite package, frontend build configs, "
                        "and durable docs under docs/."
                    ),
                    "files_to_create": [
                        "docs/architecture.md",
                        "docs/agent_topology.md",
                        "pyproject.toml",
                        "src/civlite/__init__.py",
                        "frontend/index.html",
                        "frontend/package.json",
                        "frontend/vite.config.ts",
                        "frontend/tsconfig.json",
                        "frontend/src/main.tsx",
                        "README.md",
                    ],
                    "success_test": (
                        "python -m pytest -c /dev/null -m "
                        "'not (integration or e2e)' -q 2>/dev/null || "
                        "(cd frontend && npm run build)"
                    ),
                }
            ],
            "llm_runtime_contract": [
                "Resolve LLM_API_KEY",
                "Resolve LLM_BASE_URL",
                "Resolve LLM_MODEL",
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "project-setup-docs" in result
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_uses_concrete_verification_command_alias(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "concrete-alias",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "build_engine",
                    "title": "Build engine",
                    "goal": "Implement game engine.",
                    "success_test": "run_workspace_verify",
                    "verification_command": "python -m pytest tests/test_engine.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:")


def test_propose_phase_plan_rejects_unbalanced_python_command(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "broken-command",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "validate_llm",
                    "title": "Validate LLM",
                    "goal": "Validate LLM setup.",
                    "success_test": (
                        "python -c \"from backend.core.llm_tools import "
                        "validate_llm_config; assert validate_llm_config()"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "unbalanced double quotes" in result


def test_propose_phase_plan_rejects_success_test_command_list(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "command-list",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "env_setup",
                    "title": "Set up environments",
                    "goal": "Install Python and frontend dependencies.",
                    "success_test": [
                        "python -m pytest tests/test_backend.py -q",
                        "cd frontend && npm run build",
                    ],
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "success_test must be a single executable" in result


def test_propose_phase_plan_rejects_option_only_success_test_command_object(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "option-only-success-test",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "game_engine",
                    "title": "Build game engine",
                    "goal": "Create deterministic game engine.",
                    "files_to_create": ["src/test_ws/game_engine.py"],
                    "success_test": {
                        "type": "python",
                        "command": "-m pytest tests/test_game_engine.py -v",
                    },
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "missing an executable" in result
    assert "python -m pytest" in result


def test_propose_phase_plan_rejects_invalid_python_inline_syntax(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bad-python-inline",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "tools_import",
                    "title": "Validate tools import",
                    "goal": "Check economy and diplomacy tools are importable.",
                    "success_test": (
                        "python -c \"from bot_engine.tools import "
                        "analyze_economy, evaluate_trade proposal\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "invalid `python -c` code" in result


def test_propose_phase_plan_rejects_nonportable_shell_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "nonportable-shell",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "localhost",
                    "title": "Verify localhost",
                    "goal": "Boot and stop the local service.",
                    "success_test": (
                        "ps aux | grep uvicorn || "
                        "(uvicorn backend.main:app & sleep 2 && pkill -f uvicorn)"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_rejects_decorative_echo_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_2d604b82:plan"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-echo-sentinel",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "project-setup",
                    "title": "Initialize Python + TypeScript project",
                    "goal": "Create frontend project setup and prove the build works.",
                    "files_to_create": [
                        "pyproject.toml",
                        "frontend/package.json",
                        "frontend/vite.config.ts",
                    ],
                    "success_test": (
                        "cd frontend && npm install && npm run build && "
                        "echo 'Frontend deps OK'"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "decorative shell output command" in result
    assert "echo" in result


def test_propose_phase_plan_rejects_workspace_prefixed_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "workspace-prefixed-test",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "backend_tests",
                    "title": "Run backend tests",
                    "goal": "Verify backend behavior.",
                    "success_test": (
                        "cd workspaces/test_ws/backend && "
                        "python -m pytest tests/test_api.py -q"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "host workspace path" in result


def test_propose_phase_plan_rejects_captured_cd_src_pytest_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_921912db:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-cd-src-pytest",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "docs-architecture",
                    "title": "Document game architecture",
                    "goal": "Create durable architecture docs and tests.",
                    "files_to_create": [
                        "docs/architecture.md",
                        "docs/agent_topology.md",
                        "tests/test_architecture.py",
                    ],
                    "success_test": (
                        "cd src && python -m pytest tests/test_architecture.py -q"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "changes into source root `src`" in result
    assert "docs-architecture" in result


def test_propose_phase_plan_rejects_depth_limit_placeholder(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "placeholder-plan",
            "workspace_id": "test_ws",
            "phases": [
                {
                    "id": "phase_1",
                    "title": "Build game",
                    "subtasks": [{"_depth_limit": True}],
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "depth-limit placeholder" in result


def test_propose_phase_plan_rejects_empty_plan_with_shape_guidance(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(ctx)

    assert result.startswith("ERROR:")
    assert "top-level `subtasks` array" in result


def test_propose_phase_plan_rejects_llm_heuristic_fallback(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_decisions",
                    "title": "Build bot decisions",
                    "goal": "Use GMAS LLM calls for bot turns.",
                    "success_test": "python -m pytest tests/test_bots.py -q",
                    "failure_policy": (
                        "If LLM fails, fallback to deterministic heuristic "
                        "decisions so the game continues."
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "heuristic fallback for LLM" in result


def test_propose_phase_plan_rejects_llm_no_credentials_deterministic_fallback(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-no-credentials-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_decisions",
                    "title": "Build bot decisions",
                    "goal": "Use GMAS LLM calls for bot turns.",
                    "success_test": "python -m pytest tests/test_bots.py -q",
                    "failure_policy": (
                        "If no LLM credentials configured, use deterministic "
                        "fallback rules for bots."
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "fallback for LLM" in result


def test_propose_phase_plan_rejects_llm_generic_fallback_logic(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-generic-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_decisions",
                    "title": "Build GMAS bot decisions",
                    "goal": "Use real LLM decisions for bot turns.",
                    "success_test": "python -m pytest tests/test_bots.py -q",
                }
            ],
            "risks_and_mitigations": [
                {
                    "risk": "LLM response nondeterminism",
                    "mitigation": (
                        "Use harness_run for agent tests; add retry/fallback "
                        "logic."
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "generic fallback logic" in result


def test_propose_phase_plan_rejects_llm_hyphenated_fallback_behavior(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-hyphen-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_decisions",
                    "title": "Build GMAS bot decisions",
                    "goal": "Use real LLM decisions for bot turns.",
                    "success_test": "python -m pytest tests/test_bots.py -q",
                }
            ],
            "risks_and_mitigations": [
                {
                    "risk": "LLM timeout",
                    "mitigation": "Add retry policy and fall-back behavior.",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "generic fallback logic" in result


def test_propose_phase_plan_rejects_llm_random_valid_action_fallback(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-random-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_turns",
                    "title": "Build bot turns",
                    "goal": "Use GMAS LLM calls for bot turns.",
                    "success_test": "python -m pytest tests/test_bots.py -q",
                }
            ],
            "risk_mitigation": {
                "timeout_risk": (
                    "Set LLM timeout=30s per turn, fallback to random valid "
                    "action if exceeded."
                )
            },
        },
    )

    assert result.startswith("ERROR:")
    assert "fallback for LLM" in result


def test_propose_phase_plan_rejects_llm_cached_decision_fallback(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-cached-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_failure_handling",
                    "title": "Handle LLM API failure",
                    "goal": (
                        "For LLM API failures, fallback to cached decisions and "
                        "graceful degradation."
                    ),
                    "files_to_create": ["backend/agents/failure_handling.py"],
                    "success_test": "python -m pytest tests/test_llm_failure.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "fallback for LLM" in result


def test_propose_phase_plan_rejects_captured_civilization_rule_based_ai_fallback(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-civilization-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "st_3_1_gmas_integration",
                    "title": "GMAS framework integration",
                    "goal": (
                        "Create GMAS agents for bot turns using "
                        "LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL."
                    ),
                    "files_to_create": [
                        "src/test_ws/ai/gmas_setup.py",
                        "tests/test_gmas_setup.py",
                    ],
                    "success_test": "python -m pytest tests/test_gmas_setup.py -q",
                }
            ],
            "risk_mitigation": [
                {
                    "risk": "GMAS integration complexity could delay AI implementation",
                    "mitigation": (
                        "Implement simple rule-based AI fallback first, "
                        "incrementally add LLM features, use GMAS examples "
                        "from gmas/examples/ as reference"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "fallback for LLM" in result


def test_propose_phase_plan_rejects_captured_civilization_decision_caching(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-civilization-decision-cache",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "st_3_1_gmas_integration",
                    "title": "GMAS framework integration",
                    "goal": (
                        "Create GMAS agents for bot turns using "
                        "LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL."
                    ),
                    "files_to_create": [
                        "src/test_ws/ai/gmas_setup.py",
                        "tests/test_gmas_setup.py",
                    ],
                    "success_test": "python -m pytest tests/test_gmas_setup.py -q",
                }
            ],
            "risk_mitigation": [
                {
                    "risk": "LLM API costs could exceed budget during development",
                    "mitigation": (
                        "Use model from LLM_MODEL env var, "
                        "implement caching stable, unchanging decisions, set "
                        "strict token limits per turn, track costs in game logs"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "cached decision/action/reasoning reuse" in result


def test_propose_phase_plan_rejects_key_context_llm_heuristic_fallback(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-key-context-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire real GMAS bot runtime",
                    "goal": (
                        "GMAS bots resolve LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL, then call real LLM decisions."
                    ),
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
            "decision_policies": {
                "llm_failure_handling": (
                    "Fallback to weighted heuristic for critical path; log "
                    "failures for learning"
                )
            },
        },
    )

    assert result.startswith("ERROR:")
    assert "fallback for LLM" in result


def test_propose_phase_plan_rejects_captured_conservative_strategy_fallback(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_30316f53:plan"
    ctx.loop_state_view = {"phase_label": "linear", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-conservative-strategy-fallback",
            "workspace_id": "test_ws",
            "llm_runtime_contract": {
                "env_var_pairs": [
                    ["OUROBOROS_LLM_API_KEY", "LLM_API_KEY"],
                    ["OUROBOROS_LLM_BASE_URL", "LLM_BASE_URL"],
                    ["OUROBOROS_MODEL", "LLM_MODEL"],
                ],
                "fallback_behavior": (
                    "GMAS agents and LLM tools must check both OUROBOROS_* "
                    "and LLM_* variants. If neither variant is available, fail "
                    "initialization with a clear error message explaining which "
                    "environment variables are required."
                ),
            },
            "subtasks": [
                {
                    "id": "5.2",
                    "title": "Implement LLM tooling and decision pipelines",
                    "goal": (
                        "AI decision tools call LLM via both OUROBOROS_* and "
                        "LLM_* env vars, apply actions to game state, log "
                        "decisions, and test LLM integration."
                    ),
                    "files_to_create": ["src/test_ws/ai_tools.py"],
                    "success_test": "python -m pytest tests/test_ai_tools.py -q",
                }
            ],
            "decision_policy": {
                "agent_behavior": (
                    "AI decisions must come from GMAS multi-agent system, not "
                    "hardcoded rules. LLM failure logs error and uses fallback "
                    "conservative strategy."
                )
            },
        },
    )

    assert result.startswith("ERROR:"), result
    assert "fallback for LLM" in result or "generic fallback" in result


def test_propose_phase_plan_rejects_key_context_llm_reasoning_cache(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-key-context-cache",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire real GMAS bot runtime",
                    "goal": (
                        "GMAS bots resolve LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL, then call real LLM decisions."
                    ),
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
            "risk_mitigation": {
                "llm_cost": (
                    "Cache common reasoning; prompt engineering to reduce tokens "
                    "per turn"
                )
            },
        },
    )

    assert result.startswith("ERROR:")
    assert "cached decision/action/reasoning reuse" in result


def test_propose_phase_plan_rejects_llm_error_as_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-error-as-success",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "gmas_runner",
                    "title": "Run GMAS bot decisions",
                    "goal": "Execute real GMAS/LLM bot turns with inherited env.",
                    "files_to_create": ["gmas_agents/runner.py"],
                    "success_test": (
                        "python -c \"result = {'error': 'ERROR_LLM'}; "
                        "assert 'success' in result or 'error' in result or "
                        "'ERROR_LLM' in str(result)\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "error path as a passing outcome" in result


def test_propose_phase_plan_accepts_protective_no_fallback_policy(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-no-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_turns",
                    "title": "Build bot turns",
                    "goal": "Use GMAS LLM calls for bot turns.",
                    "files_to_create": [
                        "src/test_ws/agents/bots.py",
                        "tests/test_bots.py",
                    ],
                    "success_test": "python -m pytest tests/test_bots.py -q",
                }
            ],
            "llm_policy": (
                "No fallback to hardcoded rules. LLM API errors surface as "
                "exceptions and verification tests detect hardcoded fallback logic. "
                "Runtime resolves LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL."
            ),
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_allows_tests_that_fail_on_hardcoded_fallback(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-fallback-detector",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_turns",
                    "title": "Build bot turns",
                    "goal": "Use GMAS LLM calls for bot turns.",
                    "files_to_create": [
                        "src/test_ws/agents/bots.py",
                        "tests/test_bots.py",
                    ],
                    "success_test": "python -m pytest tests/test_bots.py -q",
                }
            ],
            "llm_policy": (
                "Verification tests fail if they catch any hardcoded fallback "
                "for LLM bot decisions. LLM failures pause the turn and surface "
                "an error. Runtime resolves LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL."
            ),
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_llm_env_alias_fallback_chain_with_defaults(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-env-alias-chain",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_turns",
                    "title": "Build bot turns",
                    "goal": "Use GMAS LLM calls for bot turns.",
                    "files_to_create": [
                        "src/test_ws/agents/bots.py",
                        "tests/test_bots.py",
                    ],
                    "success_test": "python -m pytest tests/test_bots.py -q",
                }
            ],
            "llm_config": (
                "Support LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL. Check OUROBOROS_* first, "
                "fall back to LLM_* aliases."
            ),
            "bot_count": "Default to 3 AI civilizations for initial testing.",
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_llm_env_alias_parenthetical_fallback(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-env-parenthetical-fallback",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_turns",
                    "title": "Build bot turns",
                    "goal": "Use GMAS LLM calls for bot turns.",
                    "files_to_create": [
                        "src/test_ws/agents/bots.py",
                        "tests/test_bots.py",
                    ],
                    "success_test": "python -m pytest tests/test_bots.py -q",
                }
            ],
            "llm_config": (
                "Priority: LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL. OUROBOROS aliases are checked "
                "first, then LLM aliases (fallback). "
                "LLM calls raise AgentExecutionError on timeout; no "
                "replacement decisions are produced."
            ),
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_greenfield_python_outside_src(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "root-python-layout",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "backend_setup",
                    "title": "Initialize backend",
                    "goal": "Create Python backend package.",
                    "files_to_create": ["pyproject.toml", "game_engine/state.py"],
                    "success_test": "python -m pytest tests/test_state.py -q",
                },
                {
                    "id": "agents",
                    "title": "Build LLM GMAS agents",
                    "goal": "Build GMAS agents for bot turns.",
                    "files_to_create": ["agents/civ_agents.py"],
                    "success_test": "python -m pytest tests/test_agents.py -q",
                },
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "src/<package>" in result
    assert "game_engine/state.py" in result


def test_propose_phase_plan_rejects_captured_root_scripts_verify_py(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_8879972b:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "LLM-Powered Civilization Game",
            "workspace_id": "test_ws",
            "runtime_contract": (
                "Generated LLM/GMAS code and tests must resolve "
                "LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL; missing values must surface a "
                "clear error or explicit test skip."
            ),
            "phases": [
                {
                    "name": "Slice 1",
                    "subtasks": [
                        {
                            "id": "project-setup",
                            "title": "Project setup",
                            "goal": "Initialize Python package and docs.",
                            "files_to_create": [
                                "pyproject.toml",
                                "src/test_ws/__init__.py",
                                "docs/architecture.md",
                            ],
                            "success_test": "pytest tests/test_bootstrap.py -xvs",
                        },
                        {
                            "id": "localhost-verify",
                            "title": "Localhost verification",
                            "goal": "Serve app and verify HTTP endpoints with LLM env check.",
                            "files_to_create": [
                                "tests/test_deployment.py",
                                "scripts/verify_server.py",
                            ],
                            "files_to_change": ["src/test_ws/api/app.py"],
                            "success_test": "pytest tests/test_deployment.py -xvs",
                        },
                    ],
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "scripts/verify_server.py" in result
    assert "root `scripts/`" in result


def test_propose_phase_plan_accepts_greenfield_src_layout_with_docs(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "src-python-layout",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "backend_setup",
                    "title": "Initialize backend",
                    "goal": "Create Python backend package.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/test_ws/game_engine/state.py",
                        "docs/architecture.md",
                        "tests/test_state.py",
                    ],
                    "success_test": "python -m pytest tests/test_state.py -q",
                },
                {
                    "id": "agents",
                    "title": "Build LLM GMAS agents",
                    "goal": "Build GMAS agents for bot turns.",
                    "files_to_create": [
                        "src/test_ws/agents/civ_agents.py",
                        "tests/test_agents.py",
                    ],
                    "success_test": "python -m pytest tests/test_agents.py -q",
                },
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_captured_bare_src_python_layout(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_2bb95da0:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "phase_plan:init_workspace",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "init_workspace",
                    "title": "Initialize project structure and dependencies",
                    "goal": "Create Python backend and TS/JSX frontend scaffolding.",
                    "files_to_create": [
                        "pyproject.toml",
                        "package.json",
                        "src/__init__.py",
                        "frontend/src/App.tsx",
                        "docs/architecture.md",
                        "docs/agent_topology.md",
                    ],
                    "success_test": "pytest tests/test_workspace_init.py -v",
                },
                {
                    "id": "backend_game_core",
                    "title": "Implement core game engine and state management",
                    "goal": "Build game mechanics in production Python modules.",
                    "files_to_create": [
                        "src/game_engine.py",
                        "src/models.py",
                        "src/utils.py",
                    ],
                    "success_test": "pytest tests/test_game_engine.py -v",
                },
                {
                    "id": "llm_env_config",
                    "title": "Configure LLM runtime environment",
                    "goal": (
                        "Resolve LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL; fail clearly when absent."
                    ),
                    "files_to_create": ["src/config/llm.py", ".env.example"],
                    "success_test": "pytest tests/test_llm_config.py -v",
                },
                {
                    "id": "gmas_agent_design",
                    "title": "Design GMAS multi-agent system",
                    "goal": "Define GMAS agent profiles and tools.",
                    "files_to_create": [
                        "src/agents/profiles.py",
                        "src/agents/tools/__init__.py",
                    ],
                    "success_test": "pytest tests/test_gmas_design.py -v",
                },
            ],
            "llm_runtime_contract": (
                "Generated code/tests must resolve OUROBOROS_LLM_API_KEY/"
                "LLM_API_KEY, LLM_BASE_URL, and "
                "LLM_MODEL, then fail or pause clearly when "
                "real credentials are absent."
            ),
        },
    )

    assert result.startswith("ERROR: phase plan violates workspace policy")
    assert "bare `src/*.py`" in result
    assert "src/game_engine.py" in result
    assert "one canonical package root" in result
    assert "agents" in result


def test_propose_phase_plan_rejects_greenfield_pytest_modules_inside_src(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "src-python-test-layout",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "engine",
                    "title": "Build game engine",
                    "goal": "Create Python backend package.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/test_ws/game_engine/state.py",
                        "docs/architecture.md",
                        "tests/test_state.py",
                    ],
                    "success_test": "python -m pytest tests/test_state.py -q",
                },
                {
                    "id": "deployment",
                    "title": "Verify deployment",
                    "goal": "Create automated deployment proof.",
                    "files_to_create": [
                        "src/test_ws/verify/local_deployment_test.py",
                    ],
                    "success_test": (
                        "python -m pytest "
                        "src/test_ws/verify/local_deployment_test.py -q"
                    ),
                },
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "must live under `tests/`" in result
    assert "src/test_ws/verify/local_deployment_test.py" in result


def test_propose_phase_plan_rejects_captured_pytest_verify_modules_inside_src(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_1102cdcf:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "civilization"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-src-verify-pytest",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "s1_workspace_setup",
                    "title": "Initialize workspace",
                    "goal": "Create project skeleton and setup verification.",
                    "files_to_create": [
                        "src/civ/__init__.py",
                        "src/civ/verify_setup.py",
                        "docs/architecture.md",
                    ],
                    "success_test": "pytest src/civ/verify_setup.py -v",
                },
                {
                    "id": "s8_integration_e2e",
                    "title": "Integration proof",
                    "goal": "Verify full game behavior.",
                    "files_to_create": [
                        "src/civ/verify_integration.py",
                        "src/civ/verify_e2e.py",
                    ],
                    "success_test": (
                        "pytest src/civ/verify_integration.py -v && "
                        "pytest src/civ/verify_e2e.py -v"
                    ),
                },
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "test-like Python modules under `src/`" in result
    assert "must live under `tests/`" in result


def test_propose_phase_plan_rejects_greenfield_pytest_modules_inside_docs(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "docs-python-test-layout",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "docs",
                    "title": "Write architecture docs",
                    "goal": "Create docs and tests for architecture.",
                    "files_to_create": [
                        "pyproject.toml",
                        "docs/architecture.md",
                        "docs/test_game_model.py",
                    ],
                    "success_test": "python -m pytest docs/test_game_model.py -q",
                },
                {
                    "id": "engine",
                    "title": "Build game engine",
                    "goal": "Create Python backend package.",
                    "files_to_create": ["src/test_ws/game_engine/state.py"],
                    "success_test": "python -m pytest tests/test_state.py -q",
                },
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "must live under `tests/`" in result
    assert "docs/test_game_model.py" in result


def test_propose_phase_plan_rejects_workspace_id_prefixed_paths(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "workspace-prefixed-paths",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "backend_setup",
                    "title": "Initialize backend",
                    "goal": "Create Python package.",
                    "files_to_create": [
                        "test_ws/src/test_ws/game_engine/state.py",
                        "test_ws/docs/architecture.md",
                    ],
                    "success_test": "cd test_ws && python -m pytest tests/test_state.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "workspace-relative file paths" in result
    assert "do not `cd test_ws`" in result


def test_propose_phase_plan_rejects_annotated_pseudo_paths(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "annotated-pseudo-path",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "frontend_setup",
                    "title": "Configure frontend",
                    "goal": "Create frontend configuration files.",
                    "files_to_create": [
                        "frontend/package.json (deps added)",
                        "frontend/src/App.tsx (updated)",
                    ],
                    "success_test": "cd frontend && npx tsc --noEmit",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "annotated pseudo-paths" in result
    assert "frontend/package.json (deps added)" in result


def test_propose_phase_plan_uses_nested_leaves_when_phase_has_test_strategy(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "phase-wrapper-with-tests",
            "workspace_id": "test_ws",
            "llm_runtime_contract": (
                "Generated code resolves LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL, and pauses/surfaces errors when "
                "credentials are absent."
            ),
            "phases": [
                {
                    "id": "phase_1_foundations",
                    "title": "Foundations",
                    "summary": "Build the Python LLM game foundation.",
                    "test_strategy": "Health, model, and agent tests pass.",
                    "subtasks": [
                        {
                            "id": "domain",
                            "title": "Build domain state",
                            "goal": "Create src package and deterministic state.",
                            "files_to_create": [
                                "pyproject.toml",
                                "src/test_ws/game_engine/state.py",
                                "docs/architecture.md",
                                "tests/test_state.py",
                            ],
                            "success_test": "python -m pytest tests/test_state.py -q",
                        },
                        {
                            "id": "agents",
                            "title": "Build GMAS LLM agents",
                            "goal": "Create real runtime-env GMAS agent path.",
                            "files_to_create": [
                                "src/test_ws/agents/civ_agents.py",
                                "tests/test_agents.py",
                            ],
                            "success_test": "python -m pytest tests/test_agents.py -q",
                        },
                    ],
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_success_check_alias_on_leaf(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "success-check-alias",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "engine",
                    "title": "Build game engine",
                    "goal": "Create a checked domain model.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/test_ws/game_engine/state.py",
                        "docs/architecture.md",
                        "tests/test_state.py",
                    ],
                    "success_checks": "python -m pytest tests/test_state.py -q",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_complex_llm_plan_without_docs(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-no-docs",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "engine",
                    "title": "Build engine",
                    "goal": "Create game engine.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/test_ws/game_engine/state.py",
                    ],
                    "success_test": "python -m pytest tests/test_state.py -q",
                },
                {
                    "id": "agents",
                    "title": "Build LLM GMAS agents",
                    "goal": "Build GMAS agents for bot turns.",
                    "files_to_create": ["src/test_ws/agents/civ_agents.py"],
                    "success_test": "python -m pytest tests/test_agents.py -q",
                },
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "docs/" in result


def test_propose_phase_plan_rejects_js_empty_test_bypass(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "empty-js-tests",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "frontend",
                    "title": "Build frontend",
                    "goal": "Create React UI.",
                    "files_to_create": ["frontend/src/App.tsx"],
                    "success_test": "npm test -- --passWithNoTests",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "empty" in result
    assert "test" in result


def test_propose_phase_plan_rejects_posix_test_file_success(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "posix-test-file",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "dist",
                    "title": "Build dist",
                    "goal": "Build frontend distribution.",
                    "files_to_create": ["frontend/src/App.tsx"],
                    "success_test": "npm run build && test -f frontend/dist/index.html",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_rejects_complex_leaf_without_file_contract(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "missing-files",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": f"st-{idx}",
                    "title": f"Build part {idx}",
                    "goal": "Implement GMAS bot feature.",
                    "success_test": f"python -m pytest tests/test_part_{idx}.py -q",
                }
                for idx in range(1, 7)
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "files_to_create" in result
    assert "st-1" in result


def test_propose_phase_plan_rejects_mock_llm_success_test_for_gmas_subtask(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "mock-llm-proof",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "gmas_bot_system",
                    "title": "GMAS Multi-Agent Bot System",
                    "goal": "Build real LLM-backed bot turns.",
                    "files_to_create": ["backend/agents/bots.py"],
                    "success_test": (
                        "pytest tests/test_bot_graph.py "
                        "tests/test_gmas_integration.py -v --mock-llm"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "mock" in result.lower()
    assert "real runtime" in result


def test_propose_phase_plan_rejects_mock_e2e_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "mock-e2e-proof",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "e2e_integration",
                    "title": "End-to-End Integration Test",
                    "goal": "Run the full game through the real runtime.",
                    "files_to_create": ["tests/integration/test_e2e.py"],
                    "success_test": (
                        "python -m pytest tests/integration/test_e2e.py --mock -v"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "mocked path" in result


def test_propose_phase_plan_rejects_captured_e2e_pytest_target_not_declared(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_e4cde249:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "e2e-localhost-verify",
                    "title": "End-to-end simulation and localhost deployment",
                    "goal": "Execute full game with human vs AI bots and confirm localhost deployment.",
                    "files_to_change": [
                        "run_server.sh",
                        "frontend/run_dev.sh",
                        "workspace.toml",
                    ],
                    "success_test": (
                        "python -m pytest tests/test_e2e_simulation.py -q "
                        "--localhost -k test_full_game"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "tests/test_e2e_simulation.py" in result
    assert "files_to_create" in result


def test_propose_phase_plan_accepts_e2e_pytest_target_when_declared(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "e2e-localhost-verify",
                    "title": "End-to-end simulation and localhost deployment",
                    "goal": "Execute full game with human vs AI bots and confirm localhost deployment.",
                    "files_to_create": ["tests/test_e2e_simulation.py"],
                    "files_to_change": ["run_server.sh", "frontend/run_dev.sh"],
                    "success_test": (
                        "python -m pytest tests/test_e2e_simulation.py -q "
                        "--localhost -k test_full_game"
                    ),
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_control_plane_file_mutation(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_883b9f7e:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-control-file-mutation",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "project-setup",
                    "title": "Initialize project setup",
                    "goal": "Create package setup and configure workspace gates.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/civilization/__init__.py",
                        "tests/test_backend_setup.py",
                    ],
                    "files_to_change": ["workspace.toml"],
                    "success_test": "python -m pytest tests/test_backend_setup.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:"), result
    assert "workspace.toml" in result
    assert "control/evaluator" in result


def test_propose_phase_plan_rejects_paths_outside_workspace_boundary(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_883b9f7e:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-boundary-escape",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "unsafe-host-edit",
                    "title": "Patch host policy",
                    "goal": "Modify host-side policy from the generated project.",
                    "files_to_change": [
                        "../umbrella/deep_agent_tools/phase_contract_paths.py",
                        ".git/config",
                    ],
                    "success_test": "python -m pytest tests/test_backend_setup.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:"), result
    assert "../umbrella/deep_agent_tools/phase_contract_paths.py" in result
    assert ".git/config" in result
    assert "active candidate workspace" in result


def test_propose_phase_plan_rejects_final_verification_reusing_prior_target(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_883b9f7e:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-final-proof-reuse",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "integration-e2e",
                    "title": "Test full game loop",
                    "goal": "Create integration proof for the game loop.",
                    "files_to_create": ["tests/integration/test_game_loop.py"],
                    "success_test": (
                        "python -m pytest tests/integration/test_game_loop.py -q"
                    ),
                },
                {
                    "id": "final-verification",
                    "title": "Verify localhost game deployment",
                    "goal": (
                        "Start FastAPI backend and React frontend locally, "
                        "then verify WebSocket behavior."
                    ),
                    "success_test": (
                        "python -m pytest tests/integration/test_game_loop.py -q"
                    ),
                },
            ],
        },
    )

    assert result.startswith("ERROR:"), result
    assert "final-verification" in result
    assert "distinct final proof artifact" in result


def test_propose_phase_plan_accepts_final_verification_owned_target(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_883b9f7e:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "final-proof-owned",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "integration-e2e",
                    "title": "Test full game loop",
                    "goal": "Create integration proof for the game loop.",
                    "files_to_create": ["tests/integration/test_game_loop.py"],
                    "success_test": (
                        "python -m pytest tests/integration/test_game_loop.py -q"
                    ),
                },
                {
                    "id": "final-verification",
                    "title": "Verify localhost game deployment",
                    "goal": (
                        "Start FastAPI backend and React frontend locally, "
                        "then verify WebSocket behavior."
                    ),
                    "files_to_create": [
                        "tests/integration/test_localhost_deployment.py"
                    ],
                    "success_test": (
                        "python -m pytest "
                        "tests/integration/test_localhost_deployment.py -q"
                    ),
                },
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_captured_docs_pytest_target_not_owned(tmp_path):
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_05a23e7b:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-docs-target-gap",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "docs-env-contract",
                    "title": "Write README, architecture docs, and LLM env contract",
                    "goal": (
                        "Document project purpose, setup instructions, and "
                        "LLM runtime aliases."
                    ),
                    "files_to_create": [
                        "README.md",
                        ".env.example",
                        "docs/architecture.md",
                        "docs/agent_topology.md",
                    ],
                    "success_test": "python -m pytest tests/test_docs.py -q",
                },
                {
                    "id": "project-setup",
                    "title": "Initialize project",
                    "goal": "Create Python package metadata.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/civiz/__init__.py",
                        "tests/test_dependencies.py",
                    ],
                    "success_test": "python -m pytest tests/test_dependencies.py -q",
                },
            ],
        },
    )

    assert result.startswith("ERROR:"), result
    assert "tests/test_docs.py" in result
    assert "same or an earlier plan leaf" in result


def test_propose_phase_plan_rejects_captured_docs_python_verifier(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_11159129:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-docs-python-verifier",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "docs-env-contract",
                    "title": "Document architecture and env contract",
                    "goal": "Document architecture and verify the env contract.",
                    "files_to_create": [
                        "docs/architecture.md",
                        "docs/env_contract.md",
                        "docs/verification_script.py",
                    ],
                    "success_test": "python docs/verification_script.py",
                },
                {
                    "id": "runtime-package",
                    "title": "Runtime package",
                    "goal": "Create the Python package skeleton.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/civilization/__init__.py",
                        "tests/test_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_runtime.py -q",
                },
                {
                    "id": "frontend-shell",
                    "title": "Frontend shell",
                    "goal": "Create the initial frontend shell.",
                    "files_to_create": [
                        "frontend/package.json",
                        "frontend/index.html",
                        "frontend/src/main.tsx",
                    ],
                    "success_test": "cd frontend && npm run build",
                },
            ],
        },
    )

    assert result.startswith("ERROR:"), result
    assert "docs/verification_script.py" in result
    assert "Python files do not belong under `docs/`" in result


def test_propose_phase_plan_rejects_unmanaged_localhost_curl_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_92978867:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "localhost-verification",
                    "title": "Localhost verification",
                    "goal": "Verify the integrated backend and frontend game flow.",
                    "files_to_create": ["tests/integration/full_game_flow.py"],
                    "success_test": (
                        "curl -f http://127.0.0.1:8000/health && "
                        "cd frontend && npm run build && "
                        "python -m pytest tests/integration/full_game_flow.py -q"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "direct HTTP shell command" in result
    assert "managed server harness" in result


def test_propose_phase_plan_rejects_frontend_test_declared_outside_frontend(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_92978867:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "player-action-panels",
                    "title": "Player action panels",
                    "goal": "Implement frontend panels with package-level tests.",
                    "files_to_create": ["tests/frontend/panels.test.ts"],
                    "success_test": "cd frontend && npm test -- panels.test.ts",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "tests/frontend/panels.test.ts" in result
    assert "outside the frontend package" in result


def test_propose_phase_plan_rejects_captured_dry_run_e2e_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-dry-run-e2e-proof",
            "workspace_id": "test_ws",
            "phases": [
                {
                    "id": "phase6_integration_deploy",
                    "name": "Integration & Deployment",
                    "goal": (
                        "Integrate all components and deploy for localhost "
                        "testing with proper LLM env handling."
                    ),
                    "subtasks": [
                        {
                            "id": "st_6_6",
                            "title": "Game Launch and Verification",
                            "goal": (
                                "Launch game and verify AI makes coherent "
                                "LLM-based decisions across multiple turns."
                            ),
                            "files_to_create": ["tests/verify_full_game.py"],
                            "success_test": "python tests/verify_full_game.py --dry-run",
                        }
                    ],
                }
            ],
            "decision_policy": {
                "critical": [
                    "All AI agents must use real LLM calls.",
                    (
                        "Workspace code must resolve OUROBOROS_LLM_API_KEY/"
                        "LLM_API_KEY, LLM_BASE_URL, "
                        "and LLM_MODEL at runtime."
                    ),
                ]
            },
        },
    )

    assert result.startswith("ERROR:")
    assert "dry-run" in result.lower()
    assert "real runtime" in result


def test_propose_phase_plan_rejects_captured_llm_mock_env_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_d8ee8bcb:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-real-runtime-proof",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "gmas_4_decision_loop",
                    "title": "Agent Decision Integration",
                    "goal": "Integrate GMAS/LLM bot decisions into the turn loop.",
                    "files_to_create": [
                        "docs/agent_topology.md",
                        "src/civilization/agents/turn_processor.py",
                        "tests/test_turn_processor.py",
                    ],
                    "success_test": {
                        "kind": "cmd",
                        "value": (
                            "pytest tests/test_turn_processor.py -v "
                            "--use-llm-mock-env"
                        ),
                    },
                }
            ],
            "llm_config": (
                "Resolve LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL. Missing config fails explicitly; "
                "no mock LLM behavior is accepted."
            ),
        },
    )

    assert result.startswith("ERROR:"), result
    assert "mocked path" in result
    assert "real runtime" in result


def test_propose_phase_plan_rejects_captured_mock_env_decision_policy(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_d8ee8bcb:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-mock-env-policy",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "gmas_4_decision_loop",
                    "title": "Agent Decision Integration",
                    "goal": "Integrate GMAS/LLM bot decisions into the turn loop.",
                    "files_to_create": [
                        "docs/agent_topology.md",
                        "src/civilization/agents/turn_processor.py",
                        "tests/test_turn_processor.py",
                    ],
                    "success_test": "pytest tests/test_turn_processor.py -v",
                }
            ],
            "llm_config": (
                "Resolve LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL. Missing config fails explicitly."
            ),
            "decision_policy": (
                "Verify with pytest using mock env vars in CI, real LLM calls "
                "in integration; proceed if tests pass"
            ),
        },
    )

    assert result.startswith("ERROR:"), result
    assert "mock/fake/dry-run LLM behavior" in result


def test_propose_phase_plan_rejects_collect_only_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "collect-only-proof",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "project_setup",
                    "title": "Backend Project Setup",
                    "goal": "Create backend package structure.",
                    "files_to_create": ["src/test_ws/__init__.py"],
                    "success_test": "python -m pytest --collect-only",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "collects pytest tests" in result


def test_propose_phase_plan_rejects_mock_fake_llm_test_strategy(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "mock-fake-llm-strategy",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_turns",
                    "title": "LLM bot turns",
                    "goal": "GMAS bots make economy and diplomacy decisions.",
                    "files_to_create": ["backend/agents/bots.py"],
                    "success_test": "python -m pytest tests/test_real_llm_game.py -q",
                }
            ],
            "test_strategy": {
                "integration": (
                    "WebSocket connection tests with mock/fake LLM for reliability."
                )
            },
        },
    )

    assert result.startswith("ERROR:")
    assert "mock/fake/dry-run LLM behavior" in result
    assert "Matched text:" in result
    assert "Repair recipe:" in result
    assert "Remove mock/fake/dry-run" in result


def test_propose_phase_plan_rejection_gives_llm_repair_recipe(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-repeated-llm-rejection-shape",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_runtime",
                    "title": "LLM bot runtime",
                    "goal": "GMAS bots use OPENAI_API_KEY for decisions.",
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/test_ws/bots.py",
                        "tests/test_bots.py",
                    ],
                    "success_test": "python -m pytest tests/test_bots.py -q",
                }
            ],
            "test_strategy": "Use mock/fake LLM for reliability.",
        },
    )

    assert result.startswith("ERROR:")
    assert "Repair recipe:" in result
    assert "public generated-project aliases" in result
    assert "LLM_BASE_URL" in result
    assert "Remove mock/fake/dry-run" in result


def test_propose_phase_plan_allows_protective_no_mock_llm_language(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "protective-no-mock-llm",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_runtime",
                    "title": "Wire GMAS bot runtime",
                    "goal": (
                        "GMAS bot decisions use inherited real LLM runtime "
                        "without mock behavior."
                    ),
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
            "test_strategy": {
                "integration": (
                    "Reject mock/fake LLM paths and require "
                    "LLM_API_KEY, "
                    "LLM_BASE_URL, and "
                    "LLM_MODEL. No mock LLM behavior is accepted."
                )
            },
        },
    )

    assert result.startswith("OK:")


def test_propose_phase_plan_allows_prohibited_dry_run_mock_language(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "protective-prohibited-mocks",
            "workspace_id": "test_ws",
            "llm_runtime_contract": (
                "All GMAS bot decisions use LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL. PROHIBITED: dry-run mocks of LLM "
                "decisions, cached substitutions, or silent fallbacks."
            ),
            "subtasks": [
                {
                    "id": "bot_runtime",
                    "title": "Wire real GMAS bot runtime",
                    "goal": "Call real LLM agents for game decisions.",
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:")


def test_propose_phase_plan_allows_mock_terms_inside_anti_patterns(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "protective-anti-patterns",
            "workspace_id": "test_ws",
            "task_summary": (
                "Build an LLM/GMAS bot path with real runtime env. All LLM "
                "calls are real - no mocks, no dry-run."
            ),
            "subtasks": [
                {
                    "id": "bot_runtime",
                    "title": "Wire real GMAS bot runtime",
                    "goal": (
                        "Resolve LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL, then call the real "
                        "GMAS/LLM decision path."
                    ),
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/test_ws/llm_runtime.py",
                        "tests/test_real_llm_runtime.py",
                    ],
                    "success_test": (
                        "python -m pytest tests/test_real_llm_runtime.py -q"
                    ),
                }
            ],
            "anti_patterns_to_avoid": [
                "Mock LLM responses in any capacity",
                "Dry-run mode without real agent execution",
            ],
        },
    )

    assert result.startswith("OK:"), result


def test_propose_phase_plan_rejects_openai_only_llm_env_contract(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "openai-only-env",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire LLM bot runtime",
                    "goal": (
                        "GMAS bots require OPENAI_API_KEY before the game can "
                        "start and e2e tests check OPENAI_API_KEY."
                    ),
                    "files_to_create": [
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "LLM_API_KEY" in result
    assert "OPENAI_API_KEY" in result


def test_propose_phase_plan_accepts_ouroboros_llm_env_alias_contract(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "ouroboros-env",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire LLM bot runtime",
                    "goal": (
                        "GMAS bots resolve LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL from inherited env."
                    ),
                    "files_to_create": [
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_captured_ouroboros_only_llm_env_contract(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-ouroboros-only-env",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire LLM bot runtime",
                    "goal": (
                        "GMAS bots resolve OUROBOROS_LLM_API_KEY, "
                        "OUROBOROS_LLM_BASE_URL, and OUROBOROS_MODEL from "
                        "the inherited runtime for real LLM decisions."
                    ),
                    "files_to_create": [
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "leaks Umbrella host LLM aliases" in result
    assert "LLM_API_KEY" in result
    assert "LLM_BASE_URL" in result
    assert "LLM_MODEL" in result


def test_propose_phase_plan_rejects_unsupported_ll_base_url_alias(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_05a23e7b:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-ll-base-url-typo",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire LLM bot runtime",
                    "goal": (
                        "GMAS bots resolve LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL from inherited env."
                    ),
                    "files_to_create": [
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
            "llm_runtime_contract": (
                "Backend runtime resolves OUROBOROS_LLM_API_KEY or LLM_API_KEY, "
                "OUROBOROS_LLM_BASE_URL or LL_BASE_URL, and "
                "OUROBOROS_MODEL or LLM_MODEL."
            ),
        },
    )

    assert result.startswith("ERROR:"), result
    assert "LL_BASE_URL" in result
    assert "LLM_BASE_URL" in result


def test_propose_phase_plan_rejects_missing_llm_env_alias_contract(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "missing-llm-env",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "gmas_agents",
                    "title": "Build GMAS bot agents",
                    "goal": (
                        "Create LLM-powered bot decision agents and provider "
                        "configuration for real bot turns."
                    ),
                    "files_to_create": [
                        "src/test_ws/agents/bots.py",
                        "tests/test_agents.py",
                    ],
                    "success_test": "python -m pytest tests/test_agents.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "omits the standalone LLM runtime env contract" in result


def test_propose_phase_plan_rejects_llm_agent_plan_without_env_section(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "llm-agents-no-env-section",
            "workspace_id": "test_ws",
            "title": "LLM-powered civilization agents",
            "subtasks": [
                {
                    "id": "economic_agent",
                    "title": "Implement Economic AI Agent",
                    "goal": (
                        "Build a GMAS economic agent using LLM reasoning to "
                        "choose production and trade decisions."
                    ),
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/test_ws/agents/economic.py",
                        "tests/test_economic_agent.py",
                    ],
                    "success_test": "python -m pytest tests/test_economic_agent.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "omits the standalone LLM runtime env contract" in result


def test_propose_phase_plan_accepts_public_llm_alias_contract_without_ouroboros_aliases(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "public-llm-env",
            "workspace_id": "test_ws",
            "llm_runtime_contract": {
                "api_key": "LLM_API_KEY",
                "base_url": "LLM_BASE_URL",
                "model": "LLM_MODEL",
            },
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire LLM bot runtime",
                    "goal": (
                        "GMAS bots resolve LLM_API_KEY, LLM_BASE_URL, and "
                        "LLM_MODEL for real bot decisions."
                    ),
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/test_ws/agents/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_control_plane_llm_alias_contract(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "leaked-host-alias",
            "workspace_id": "test_ws",
            "llm_runtime_contract": {
                "api_key": "OUROBOROS_LLM_API_KEY/LLM_API_KEY",
                "base_url": "OUROBOROS_LLM_BASE_URL/LLM_BASE_URL",
                "model": "OUROBOROS_MODEL/LLM_MODEL",
            },
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire LLM bot runtime",
                    "goal": (
                        "GMAS bots resolve public env but keep host aliases "
                        "listed for compatibility."
                    ),
                    "files_to_create": [
                        "src/test_ws/agents/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "leaks Umbrella host LLM aliases" in result
    assert "LLM_API_KEY" in result


def test_propose_phase_plan_rejects_unsupported_ouroboros_model_alias(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "wrong-model-alias",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire LLM bot runtime",
                    "goal": (
                        "GMAS bots resolve OUROBOROS_LLM_API_KEY, "
                        "OUROBOROS_LLM_BASE_URL, and OUROBOROS_LLM_MODEL."
                    ),
                    "files_to_create": ["backend/llm_runtime.py", "tests/test_llm_runtime.py"],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "OUROBOROS_LLM_MODEL" in result
    assert "LLM_MODEL" in result


def test_propose_phase_plan_rejects_protective_unsupported_model_alias_note(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_92978867:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "protective-model-alias-note",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire LLM bot runtime",
                    "goal": (
                        "GMAS bots resolve LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL. Do not use "
                        "OUROBOROS_LLM_MODEL for model selection."
                    ),
                    "files_to_create": [
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                        "docs/architecture.md",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "OUROBOROS_LLM_MODEL" in result


def test_propose_phase_plan_rejects_provider_specific_llm_model_default(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "gpt-cost-default",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_budget",
                    "title": "Add LLM budget guard",
                    "goal": (
                        "GMAS bots use LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL. Estimate full-run cost "
                        "with gpt-4o-mini as the default model."
                    ),
                    "files_to_create": ["tests/test_llm_budget.py"],
                    "success_test": "python -m pytest tests/test_llm_budget.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "provider/model-specific" in result
    assert "gpt-*" in result


def test_propose_phase_plan_rejects_captured_provider_default_next_to_no_policy(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_c25a0092:plan"
    ctx.loop_state_view = {
        "phase_label": "plan",
        "active_workspace_id": "civilization",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-provider-default",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "llm_config",
                    "title": "Configure LLM runtime",
                    "goal": (
                        "GMAS bots use LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL from inherited env."
                    ),
                    "files_to_create": [
                        "docs/llm_configuration.md",
                        "src/civgame/config.py",
                        "tests/test_config.py",
                    ],
                    "success_test": "pytest tests/test_config.py -v",
                }
            ],
            "decision_policies": {
                "llm_choice": (
                    "Use configurable LLM provider via environment variables. "
                    "Support both OUROBOROS_* and LLM_* aliases. Default to "
                    "gpt-4o-mini for speed and cost."
                ),
                "gmas_usage": (
                    "Strictly use GMAS for all AI bot logic. No custom LLM "
                    "wrapping or bypassing GMAS APIs."
                ),
            },
        },
    )

    assert result.startswith("ERROR:"), result
    assert "provider/model-specific" in result
    assert "gpt-*" in result


def test_propose_phase_plan_rejects_empty_basic_import_test_skeletons(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "empty-test-shells",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "test_infra",
                    "title": "Create test infrastructure",
                    "goal": (
                        "Create all referenced test files as empty shells with "
                        "basic pytest imports before implementation."
                    ),
                    "files_to_create": ["tests/test_ai_authenticity.py"],
                    "success_test": "python -m pytest tests/test_ai_authenticity.py -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "empty/basic-import test skeletons" in result


def test_propose_phase_plan_allows_protective_empty_test_language(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "behavioral-tests",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "test_infra",
                    "title": "Create behavioral test infrastructure",
                    "goal": (
                        "Do not create empty or import-only test shells; tests "
                        "must contain executable assertions that fail for real "
                        "behavior regressions."
                    ),
                    "files_to_create": ["tests/test_ai_authenticity.py"],
                    "success_test": "python -m pytest tests/test_ai_authenticity.py -q",
                }
            ],
        },
    )

    assert result.startswith("OK:")


def test_propose_phase_plan_allows_captured_no_import_only_policy(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-no-import-only-policy",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "gmas_llm_tests",
                    "title": "Add GMAS LLM integration tests",
                    "goal": "Create tests that prove GMAS bots call the real runtime path.",
                    "files_to_create": ["tests/test_gmas_agents.py"],
                    "success_test": "python -m pytest tests/test_gmas_agents.py -q",
                }
            ],
            "decision_policies": {
                "llm_runtime_configuration": (
                    "Resolve API key from LLM_API_KEY, base URL from "
                    "LLM_BASE_URL, and model from LLM_MODEL."
                ),
                "testing_authenticity": (
                    "No import-only tests. LLM-backed tests verify actual GMAS "
                    "tool calling via MACPRunner and structured JSONs. E2E "
                    "tests run real turns with real LLM when credentials are "
                    "present; if absent, tests skip with pytest.skip and "
                    "message, not silent pass."
                )
            },
        },
    )

    assert result.startswith("OK:")


def test_propose_phase_plan_accepts_public_llm_env_contract_in_notes(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "narrow-env-notes",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_runtime",
                    "title": "Wire GMAS bot runtime",
                    "goal": "GMAS bots call the inherited LLM runtime.",
                    "files_to_create": [
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ],
        },
        notes=(
            "This project requires real LLM configuration via LLM_API_KEY, "
            "LLM_BASE_URL, and LLM_MODEL environment variables."
        ),
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_unwraps_serialized_plan_object(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    embedded = {
        "subtasks": [
            {
                "id": "domain_model",
                "title": "Domain model",
                "goal": "Implement game domain behavior.",
                "files_to_create": [
                    "backend/domain/game_state.py",
                    "tests/test_domain.py",
                ],
                "success_test": "python -m pytest tests/test_domain.py -q",
            }
        ]
    }

    result = _propose_phase_plan(
        ctx,
        plan={"plan": json.dumps(embedded), "plan_len": 1234},
    )

    assert not result.startswith("ERROR:"), result
    latest = json.loads((drive / "state" / "phase_plan_proposal_latest.json").read_text())
    assert latest["plan"]["subtasks"][0]["id"] == "domain_model"


def test_propose_phase_plan_rejects_invalid_serialized_plan_string_clearly(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan": (
                '{"subtasks":[{"id":"domain_model","title":"Domain model",'
                '"success_test":"python -m pytest tests/test_domain.py -q"}'
            )
        },
    )

    assert result.startswith("ERROR:")
    assert "serialized text in `plan.plan`" in result


def test_propose_phase_plan_accepts_python_inline_assert_with_quoted_semicolons(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "llm_runtime_contract": (
                "Generated code resolves LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL, and surfaces missing runtime "
                "credentials as explicit errors."
            ),
            "subtasks": [
                {
                    "id": "config_check",
                    "title": "Config check",
                    "goal": "Validate a simple inline configuration assertion.",
                    "files_to_create": ["backend/config.py", "tests/test_config.py"],
                    "success_test": (
                        'python -c "value = 2; assert value == 2" && '
                        "python -m pytest tests/test_config.py -q"
                    ),
                }
            ]
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_content_alias(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        content={
            "subtasks": [
                {
                    "id": "domain_model",
                    "title": "Domain model",
                    "goal": "Implement game domain behavior.",
                    "files_to_create": [
                        "backend/domain/game_state.py",
                        "tests/test_domain.py",
                    ],
                    "success_test": "python -m pytest tests/test_domain.py -q",
                }
            ]
        },
    )

    assert not result.startswith("ERROR:"), result
    latest = json.loads((drive / "state" / "phase_plan_proposal_latest.json").read_text())
    assert latest["plan"]["subtasks"][0]["id"] == "domain_model"


def test_propose_phase_plan_does_not_treat_llm_driven_real_time_as_signature_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "llm_runtime_contract": (
                "Generated code resolves LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL, and surfaces missing runtime "
                "credentials as explicit errors."
            ),
            "subtasks": [
                {
                    "id": "llm_decisions",
                    "title": "LLM decision evidence",
                    "goal": (
                        "AI civilizations make LLM-driven decisions evidenced by "
                        "tool calls and structured outputs. WebSocket updates "
                        "provide real-time game state."
                    ),
                    "files_to_create": [
                        "src/test_ws/llm_runtime.py",
                        "tests/test_llm_runtime.py",
                    ],
                    "success_test": "python -m pytest tests/test_llm_runtime.py -q",
                }
            ]
        },
    )

    assert not result.startswith("ERROR:"), result
    assert "LLM()` is missing required `real`" not in result


def test_propose_phase_plan_does_not_treat_success_criteria_as_between_signature_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "llm_runtime_contract": (
                "Generated code resolves LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL, and pauses/surfaces errors when "
                "credentials are absent."
            ),
            "success_criteria": [
                "Diplomacy between LLM bots results in treaties, trades, or wars.",
                "Game integration tests pass covering 20+ turns without crashes.",
            ],
            "subtasks": [
                {
                    "id": "integration",
                    "title": "Integration tests",
                    "goal": "Create multi-turn integration tests.",
                    "files_to_create": ["tests/test_integration.py"],
                    "success_test": "python -m pytest tests/test_integration.py -q",
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result
    assert "between()` is missing required `covering`" not in result


def test_propose_phase_plan_rejects_import_only_python_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "import-only",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_tools",
                    "title": "Create bot tools",
                    "goal": "Create callable bot decision tools.",
                    "success_test": (
                        "python -c \"from backend.bots.bot_tools import "
                        "build_city; print('Bot tools imported')\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "only imports modules" in result


def test_propose_phase_plan_rejects_direct_python_pytest_node_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "direct-python-pytest-node",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "llm_connectivity",
                    "title": "Validate LLM connectivity",
                    "goal": "Verify the workspace can reach the configured LLM.",
                    "success_test": (
                        "python tests/test_llm_config.py::test_llm_connectivity"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "python -m pytest" in result


def test_propose_phase_plan_rejects_complex_python_inline_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "complex-inline",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "localhost_check",
                    "title": "Verify localhost",
                    "goal": "Start server and verify HTTP readiness.",
                    "success_test": (
                        "python -c \"import subprocess; import time; import "
                        "requests; proc = subprocess.Popen(['python', '-m', "
                        "'backend.api']); time.sleep(3); resp = "
                        "requests.get('http://localhost:8000/health'); "
                        "proc.kill(); assert resp.status_code == 200\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "too complex" in result


def test_propose_phase_plan_rejects_python_inline_workspace_import_success_test(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_104ff3a2:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-inline-api-schema",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "arch_api",
                    "title": "Design REST API architecture",
                    "goal": "Create API schema definitions.",
                    "files_to_create": [
                        "docs/api_spec.md",
                        "src/civgame/api_schemas.py",
                    ],
                    "success_test": (
                        "python -c \"from src.civgame.api_schemas import "
                        "GameState, Civilization, PlayerAction; gs = "
                        "GameState(civilizations=[], map={'width': 10, "
                        "'height': 10}, turn=1); assert gs.turn == 1\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "imports workspace/application modules" in result


def test_propose_phase_plan_rejects_captured_multiline_python_inline_success_test(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-multiline-python-c",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "game-loop-orchestration",
                    "title": "Implement game loop and turn orchestration",
                    "goal": "Connect game loop to real GameState behavior.",
                    "files_to_create": ["src/civgame/game/loop.py"],
                    "success_test": (
                        "python -c \"\n"
                        "from civgame.game.loop import GameLoop\n"
                        "gs = type('GameState', (), {'turn': 0})()  # Mock state\n"
                        "loop = GameLoop(game_state=gs)\n"
                        "assert loop is not None\n"
                        "print('Game loop OK')\n"
                        "\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "too complex" in result


def test_propose_phase_plan_rejects_descriptive_success_test_suffix(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "descriptive-success",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "domain_model",
                    "title": "Domain model",
                    "goal": "Implement and test game objects.",
                    "success_test": (
                        "python -m pytest tests/test_game_objects.py -q "
                        "- must instantiate all classes"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "descriptive acceptance text" in result


def test_propose_phase_plan_rejects_captured_exit_code_suffix(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_73e6952a:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "civilization"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-exit-code-suffix",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "subtask_1",
                    "title": "Design Core Game Domain",
                    "goal": "Define game rules and GMAS multi-agent roles.",
                    "files_to_create": [
                        "src/civilization/domain/game_state.py",
                        "tests/test_domain.py",
                    ],
                    "success_test": (
                        "pytest tests/test_domain.py tests/test_agents.py "
                        "-v --tb=short - exit code 0"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "descriptive acceptance text" in result


def test_propose_phase_plan_rejects_captured_success_test_outcome_prose(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_882cfdac:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "civilization"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-outcome-prose",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "llm-env-config",
                    "title": "Implement LLM environment configuration",
                    "goal": (
                        "Generated code resolves OUROBOROS_LLM_API_KEY/"
                        "LLM_API_KEY, LLM_BASE_URL, "
                        "and LLM_MODEL, then surfaces clear "
                        "errors when real credentials are absent."
                    ),
                    "files_to_create": [
                        "src/civilization/config.py",
                        "tests/test_config.py",
                    ],
                    "success_test": (
                        "LLM_API_KEY=key LLM_BASE_URL=http://fake "
                        "LLM_MODEL=model pytest tests/test_config.py -q "
                        "succeeds; without env vars pytest tests/test_config.py "
                        "-q fails with clear error"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "descriptive pass/fail outcome prose" in result


def test_propose_phase_plan_rejects_captured_command_prefixed_success_test(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_ee48ce93:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-command-prefix",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "st01_backend_core",
                    "title": "Backend Core: Game State & Rules",
                    "goal": "Implement deterministic game state and rules.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/test_ws/game_state.py",
                        "docs/architecture.md",
                        "tests/test_game_state.py",
                    ],
                    "success_test": (
                        "Command: pytest tests/test_game_state.py -v "
                        "-k 'test_gamestate_initialization'"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "descriptive text with a prefix" in result
    assert "Command:" in result


def test_propose_phase_plan_rejects_captured_parenthetical_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_882cfdac:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "civilization"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-parenthetical-prose",
            "workspace_id": "civilization",
            "subtasks": [
                {
                    "id": "fastapi-websocket",
                    "title": "Implement FastAPI WebSocket server",
                    "goal": "Create WebSocket endpoints and automated behavior tests.",
                    "files_to_create": [
                        "src/civilization/server.py",
                        "tests/test_server.py",
                    ],
                    "success_test": (
                        "pytest tests/test_server.py -q -v "
                        "(TestClient with WebSocket)"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "parenthetical explanatory prose" in result


def test_propose_phase_plan_rejects_descriptive_browser_observation_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "descriptive-browser",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "manual_e2e_verification",
                    "title": "Manual end-to-end verification on localhost",
                    "goal": "Verify the local game UI.",
                    "success_test": (
                        "Server starts cleanly; browser opens to localhost:5173; "
                        "human player completes 3 turns with AI responses visible; "
                        "browser console has zero errors; WebSocket messages show "
                        "in network inspector"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "manual_e2e_verification" in result
    assert "describes browser/user observation" in result


def test_propose_phase_plan_rejects_generic_tool_with_pseudo_args(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "tool-pseudo-args",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "domain_model",
                    "title": "Domain model",
                    "goal": "Implement and test game objects.",
                    "success_test": "run_unit_tests tests/test_game_objects.py",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "pseudo-arguments" in result


def test_propose_phase_plan_rejects_generic_tool_colon_pseudo_args(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "tool-colon-pseudo-args",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "ai_controller",
                    "title": "AI controller",
                    "goal": "Implement and test GMAS AI turns.",
                    "success_test": "harness_run:subtask_ai_controller:3:tests_pass",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "pseudo-arguments" in result


def test_propose_phase_plan_rejects_print_fail_python_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "print-fail",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "file_check",
                    "title": "Check generated files",
                    "goal": "Verify generated files exist.",
                    "success_test": (
                        "python -c \"import pathlib as p; "
                        "print('OK' if p.Path('app.py').exists() else 'FAIL')\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "only prints FAIL/ERROR" in result


def test_propose_phase_plan_rejects_file_existence_only_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "file-existence-only",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "frontend_component",
                    "title": "Create frontend component",
                    "goal": "Implement a game map component.",
                    "files_to_create": ["frontend/src/components/GameMap.tsx"],
                    "success_test": (
                        "node -e \"const fs=require('fs'); "
                        "assert(fs.existsSync('frontend/src/components/GameMap.tsx'))\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "only checks file/path existence" in result


def test_propose_phase_plan_rejects_pathlib_join_exists_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "pathlib-join-existence-only",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "architecture_docs",
                    "title": "Create architecture docs",
                    "goal": "Write durable architecture docs.",
                    "files_to_create": ["docs/architecture.md"],
                    "success_test": (
                        "python -c \"from pathlib import Path; docs = Path('docs'); "
                        "required = ['architecture.md']; "
                        "missing = [f for f in required if not (docs/f).exists()]; "
                        "assert not missing\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "only checks file/path existence" in result


def test_propose_phase_plan_rejects_inline_docs_content_python_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "inline-docs-content",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "docs-contract",
                    "title": "Write architecture docs and runtime contract",
                    "goal": "Document the runtime contract.",
                    "files_to_create": [
                        "README.md",
                        "docs/architecture.md",
                        "docs/bot_personas.md",
                        "docs/setup.md",
                    ],
                    "success_test": (
                        "python -c \"import os; assert "
                        "'OUROBOROS_LLM_API_KEY' in open('README.md').read() "
                        "and 'GMAS' in open('docs/architecture.md').read() "
                        "and 'bot personas' in open('docs/bot_personas.md').read().lower() "
                        "and 'WS' in open('docs/setup.md').read()\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "documentation/content inline" in result


def test_propose_phase_plan_rejects_bare_assert_in_shell_chain(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bare-assert-chain",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "frontend_build",
                    "title": "Build frontend",
                    "goal": "Build frontend and verify the artifact exists.",
                    "success_test": (
                        "cd frontend && npm run build && "
                        "assert os.path.exists('frontend/dist/index.html')"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "bare Python `assert`" in result


def test_propose_phase_plan_rejects_bash_script_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "bash-deployment-test",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "deployment",
                    "title": "Deployment verification",
                    "goal": "Verify the local app launch path.",
                    "files_to_create": ["tests/deployment/test_launch.sh"],
                    "success_test": "bash tests/deployment/test_launch.sh",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_rejects_direct_sh_script_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_1eed9b9c:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "civilization_phase_001",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "st_011",
                    "name": "Localhost Deployment and Verification",
                    "goal": "Deploy locally and verify the playtest path.",
                    "files_to_create": [
                        "scripts/verify_playtest.py",
                        "scripts/run_local.sh",
                    ],
                    "success_test": (
                        "./scripts/run_local.sh && python scripts/verify_playtest.py"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "st_011" in result
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_rejects_env_prefixed_sh_script_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_6809bbeb:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "llm_runtime_contract": (
                "Use LLM_API_KEY, "
                "LLM_BASE_URL, "
                "LLM_MODEL."
            ),
            "subtasks": [
                {
                    "id": "final-e2e-verification",
                    "title": "Run E2E localhost verification with real LLM",
                    "goal": "Create E2E tests and workspace verification.",
                    "files_to_create": [
                        "docs/architecture.md",
                        "src/civgame/__init__.py",
                        "tests/test_e2e.py",
                        "scripts/verify.sh",
                    ],
                    "files_to_change": ["workspace.toml"],
                    "success_test": (
                        "RUN_TESTS_AUTO=true RUN_E2E_AUTO=true scripts/verify.sh"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "final-e2e-verification" in result
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_rejects_bash_c_file_existence_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_d34fe709:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "phase_plan:st_architecture_docs",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "st_architecture_docs",
                    "title": "Architecture Documentation",
                    "goal": "Create durable docs for system architecture.",
                    "files_to_create": [
                        "docs/architecture.md",
                        "docs/agent_topology.md",
                    ],
                    "success_test": (
                        "bash -c '[ -f docs/architecture.md ] && "
                        "[ -f docs/agent_topology.md ] && echo docs_exist'"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "st_architecture_docs" in result
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_rejects_exit_status_shell_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "exit-status-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "frontend_typecheck",
                    "title": "Type-check frontend",
                    "goal": "Run TypeScript checking.",
                    "files_to_create": ["frontend/package.json"],
                    "success_test": "cd frontend && npx tsc --noEmit && exit $?",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_rejects_inline_exit_if_shell_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_740d5c97:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-frontend-build",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "phase_web_740d5c97:subtask_4",
                    "title": "React Frontend with TypeScript + JSX",
                    "goal": "Build the frontend.",
                    "files_to_create": ["frontend/src/App.tsx"],
                    "success_test": (
                        "cd /workspace/frontend && npm run build && exit 0 "
                        "if [ $? -eq 0 ]; then echo 'Frontend build "
                        "successful'; else echo 'Frontend build failed'; "
                        "exit 1; fi"
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_rejects_start_job_success_test(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "start-job-plan",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "localhost_smoke",
                    "title": "Localhost smoke",
                    "goal": "Verify the local app launch path.",
                    "files_to_create": ["scripts/dev.ps1"],
                    "success_test": (
                        'powershell -Command "Start-Job -ScriptBlock '
                        "{ .\\scripts\\dev.ps1 }; Invoke-WebRequest "
                        "http://localhost:8000/health\""
                    ),
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "non-portable or unmanaged shell/process-control" in result


def test_propose_phase_plan_accepts_verification_commands_object(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "verification-commands-object",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_tools",
                    "title": "Create bot tools",
                    "goal": "Implement GMAS bot tool behavior.",
                    "files_to_create": ["src/test_ws/agents/bot_tools.py"],
                    "verification": {
                        "commands": ["python -m pytest tests/test_bot_tools.py -q"]
                    },
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_verification_command_list_alias(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "verification-list-alias",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "bot_tools",
                    "title": "Create bot tools",
                    "goal": "Implement GMAS bot tool behavior.",
                    "files_to_create": ["backend/agents/bot_tools.py"],
                    "verification": [
                        "Run: python -m pytest tests/test_bot_tools.py -q",
                        "Run: python -m pytest tests/test_agent_graph.py -q",
                    ],
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "`verification` is a list" in result
    assert "top-level `success_test`" in result


def test_propose_phase_plan_rejects_missing_revision_number(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Specify relationship_eviction_policy after "
                                "20 turns of no interaction"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "wrong-number",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "agent_memory",
                    "title": "Agent memory",
                    "goal": "Implement memory retention.",
                    "success_test": "python -m pytest tests/test_memory.py -q",
                    "acceptance": [
                        "relationship_eviction_policy after 15 turns of no interaction"
                    ],
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "numeric requirement" in result
    assert "20" in result


def test_propose_phase_plan_ignores_non_actionable_budget_revision_number(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Budget ($0.8 USD) is insufficient for full "
                                "implementation. A functional Civilization "
                                "simulator requires more resources."
                            ),
                            (
                                "WebSocket testing in st_4_4 is complex for "
                                "$0.8 budget - consider simpler HTTP polling "
                                "for MVP."
                            ),
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "budget-review",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "st_4_4",
                    "title": "HTTP polling state updates",
                    "goal": (
                        "Replace WebSocket testing with simpler HTTP polling "
                        "for the MVP update path."
                    ),
                    "files_to_create": ["tests/test_polling_updates.py"],
                    "success_test": (
                        "python -m pytest tests/test_polling_updates.py -q"
                    ),
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result
    assert "0.8" not in result


def test_propose_phase_plan_rejects_truncated_serialized_plan_string(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan": (
                '{"title":"Too large","phases":[{"id":"one","title":"Setup",'
                '"success_test":"python -m pytest tests/test_setup.py -q"}'
            ),
            "plan_len": 26000,
            "plan_sha256": "abc123",
            "plan_truncated": True,
        },
    )

    assert result.startswith("ERROR:")
    assert "truncated serialized text" in result
    assert "compact JSON object" in result


def test_propose_phase_plan_accepts_phase_number_revision_with_decimal_subtasks(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Phase 3: Add component-level tests with Vitest; "
                                "move npm run build to after component implementation"
                            ),
                            (
                                "Test strategy section: Convert high-level "
                                "statements to concrete executable commands"
                            ),
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "phase-three",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "3.1_component_tests",
                    "title": "Phase 3 component tests",
                    "goal": "Add component-level tests with Vitest.",
                    "files_to_create": ["frontend/src/App.test.tsx"],
                    "success_test": "cd frontend; npx vitest run",
                },
                {
                    "id": "3.2_frontend_build",
                    "title": "Phase 3 frontend build",
                    "goal": "Run npm build after component implementation.",
                    "files_to_change": ["frontend/package.json"],
                    "success_test": "cd frontend; npm run build",
                },
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_rejects_frontend_build_before_entrypoint_files(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_ce127a9e:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "captured-frontend-build-before-entrypoint",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "project-setup",
                    "title": "Initialize Python + React project with dependencies",
                    "goal": (
                        "Create pyproject.toml, frontend package metadata, Vite "
                        "config, env example, and README."
                    ),
                    "files_to_create": [
                        "pyproject.toml",
                        "frontend/package.json",
                        "frontend/vite.config.ts",
                        "frontend/tsconfig.json",
                        ".env.example",
                        "README.md",
                    ],
                    "success_test": "cd frontend && npm run build",
                },
                {
                    "id": "frontend-setup",
                    "title": "Initialize React + TypeScript + Vite frontend",
                    "goal": "Create the frontend source entrypoint.",
                    "files_to_create": [
                        "frontend/src/main.tsx",
                        "frontend/src/App.tsx",
                        "frontend/src/index.css",
                    ],
                    "success_test": "cd frontend && npm run build",
                },
            ],
        },
    )

    assert result.startswith("ERROR:"), result
    assert "frontend build success_test before the files needed" in result
    assert "project-setup" in result
    assert "frontend/src/<entry>.tsx" in result
    assert "frontend/index.html" in result


def test_propose_phase_plan_accepts_frontend_build_with_entrypoint_files(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_ce127a9e:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "frontend-build-entrypoint-owned",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "project-setup",
                    "title": "Initialize React frontend package and entrypoint",
                    "goal": "Create Vite package metadata and minimal app entrypoint.",
                    "files_to_create": [
                        "frontend/package.json",
                        "frontend/vite.config.ts",
                        "frontend/tsconfig.json",
                        "frontend/index.html",
                        "frontend/src/main.tsx",
                        "frontend/src/App.tsx",
                    ],
                    "success_test": "cd frontend && npm run build",
                },
                {
                    "id": "docs",
                    "title": "Document frontend quickstart",
                    "goal": "Create durable docs for local frontend startup.",
                    "files_to_create": ["docs/frontend.md", "tests/test_docs.py"],
                    "success_test": "python -m pytest tests/test_docs.py -q",
                },
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_suffixed_decimal_revision_target(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Add explicit WebSocket test harness before Phase 5.4 "
                                "e2e tests - define how tests verify localhost:8000 "
                                "and localhost:3000 connectivity without manual "
                                "server startup"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "suffixed-decimal",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "5.4a_localhost_harness",
                    "title": "Phase 5.4 WebSocket localhost e2e harness",
                    "goal": (
                        "Add an explicit WebSocket test harness that starts the "
                        "backend on localhost:8000 and frontend on localhost:3000, "
                        "then checks connectivity without manual server startup."
                    ),
                    "files_to_create": ["tests/e2e/test_session.py"],
                    "success_test": (
                        "python -m pytest "
                        "tests/e2e/test_session.py::test_localhost_8000_3000 -q"
                    ),
                }
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_test_quality_revision_without_numeric_loop(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        host_repo_root=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_node": {
                "id": "plan",
                "manifest_id": "plan",
                "overlay": {
                    "retry_reason": "micro review requested revisions",
                    "revision_contract": {
                        "revisions": [
                            (
                                "Split test creation and validation into separate "
                                "subtasks for subtasks 2.2, 2.3, 3.3, 4.1-4.5, "
                                "5.4, and all Phase 6 subtasks - create test files "
                                "in one step, validate in another step"
                            )
                        ]
                    },
                },
            }
        },
    )
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "test-quality-revision",
            "workspace_id": "test_ws",
            "subtasks": [
                {
                    "id": "2.2a_turn_manager",
                    "title": "Turn manager with executable tests",
                    "goal": "Create turn manager behavior and real assertions.",
                    "files_to_create": [
                        "docs/architecture.md",
                        "tests/test_turn_manager.py",
                    ],
                    "success_test": "python -m pytest tests/test_turn_manager.py -q",
                },
                {
                    "id": "2.3a_mechanics",
                    "title": "Mechanics with executable tests",
                    "goal": "Create mechanics behavior and real assertions.",
                    "files_to_create": ["tests/test_mechanics.py"],
                    "success_test": "python -m pytest tests/test_mechanics.py -q",
                },
                {
                    "id": "3.3a_agent_graph",
                    "title": "Agent graph with executable tests",
                    "goal": "Create GMAS graph behavior and real assertions.",
                    "files_to_create": ["tests/test_gmas_graph.py"],
                    "success_test": "python -m pytest tests/test_gmas_graph.py -q",
                },
                {
                    "id": "4.1a_websocket_hook",
                    "title": "WebSocket hook with component tests",
                    "goal": "Create WebSocket hook and real frontend assertions.",
                    "files_to_create": ["frontend/src/useWebSocket.test.tsx"],
                    "success_test": "cd frontend && npx vitest run src/useWebSocket.test.tsx",
                },
                {
                    "id": "4.5a_ai_event_log",
                    "title": "AI event log with component tests",
                    "goal": "Create AI event log and real frontend assertions.",
                    "files_to_create": ["frontend/src/AIEventLog.test.tsx"],
                    "success_test": "cd frontend && npx vitest run src/AIEventLog.test.tsx",
                },
                {
                    "id": "5.4a_e2e_harness",
                    "title": "Localhost e2e harness with assertions",
                    "goal": "Create local e2e harness and real assertions.",
                    "files_to_create": ["tests/e2e/test_session.py"],
                    "success_test": "python -m pytest tests/e2e/test_session.py -q",
                },
                {
                    "id": "6.1a_verification_suite",
                    "title": "Verification suite with assertions",
                    "goal": "Create verification tests with real assertions.",
                    "files_to_create": ["tests/verification/test_suite.py"],
                    "success_test": "python -m pytest tests/verification/test_suite.py -q",
                },
            ],
        },
    )

    assert not result.startswith("ERROR:"), result


def test_propose_phase_plan_accepts_phase_mapping_containers(tmp_path):
    workspace = tmp_path / "workspaces" / "test_ws"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "run-123:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": "test_ws"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "mapped-phases",
            "workspace_id": "test_ws",
            "phases": {
                "phase_1_setup": {
                    "title": "Setup",
                    "subtasks": {
                        "setup_backend": {
                            "id": "setup_backend",
                            "title": "Setup backend",
                            "goal": "Create backend package and tests.",
                            "files_to_create": ["backend/tests/test_setup.py"],
                            "success_test": (
                                "python -m pytest backend/tests/test_setup.py -q"
                            ),
                        }
                    },
                }
            },
        },
    )

    assert not result.startswith("ERROR:"), result

