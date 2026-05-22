import json

from umbrella.deep_agent_tools import phase_contract_handlers
from umbrella.deep_agent_tools.phase_control_actions import (
    _submit_micro_review,
    _submit_research_summary,
)
from umbrella.deep_agent_tools.phase_control_common import ToolContext


def test_research_review_revise_cannot_demote_current_source_scarce_finding(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    state.mkdir(parents=True)
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_65c28d9f",
                "coverage_status": "source_scarce",
                "findings_ids": ["finding-github"],
                "notes": "Source-scarce handoff with one accepted current-run finding.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "phase_web_65c28d9f:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-github",
                    "kind": "research_finding",
                    "source_path": "github_project_search:multi-agent GMAS python",
                }
            ),
        },
        {
            "task_id": "phase_web_65c28d9f:research_review",
            "tool": "palace_search",
            "args": {"query": "research findings"},
            "result_preview": "stale memory says accepted findings are missing",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_65c28d9f:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "insufficient_research_evidence",
                "severity": "blocking",
                "phase": "research",
                "message": "palace_search recall says there are no accepted findings",
            }
        ],
        loop_back_target="research",
        notes="Loop back because stale memory claims current findings are absent.",
    )

    assert result.startswith("ERROR:")
    assert "already cites accepted current-run research finding" in result


def test_research_palace_add_can_read_tool_rows_without_name_error(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_a31fd64b:research",
                "tool": "github_project_search",
                "args": {
                    "query": "typescript react strategy game simulation",
                },
                "result_preview": json.dumps(
                    {
                        "status": "ok",
                        "query": "typescript react strategy game simulation",
                        "results": [
                            {
                                "full_name": "TomaszMarczak/roulette-strategy",
                                "html_url": "https://github.com/TomaszMarczak/roulette-strategy",
                            }
                        ],
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        phase_contract_handlers,
        "_save_umbrella_memory",
        lambda ctx, **kwargs: json.dumps({"id": "legacy-research-finding"}),
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_a31fd64b:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = phase_contract_handlers._palace_add(
        ctx,
        title="Current prior-art source",
        content=(
            "TomaszMarczak/roulette-strategy is a React/TypeScript "
            "simulation project relevant to UI architecture."
        ),
        kind="research_finding",
        source_id="github_project_search:typescript react strategy game simulation",
        evidence_kind="architecture_pattern",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "research_finding"


def test_research_palace_add_accepts_repo_handle_from_truncated_github_preview(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_5cb4d4af:research",
                "tool": "github_project_search",
                "args": {
                    "query": "python game AI LLM strategy",
                    "max_repos": 5,
                },
                "result_preview": (
                    '{"status": "ok", "query": "python game AI LLM strategy", '
                    '"results": [{"name": "4x-game-agent", '
                    '"full_name": "sonpiaz/4x-game-agent", '
                    '"html_url": "https://github.com/sonpiaz/4x-game-agent", '
                    '"description": "LLM-powered AI agent framework"}], ...'
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        phase_contract_handlers,
        "_save_umbrella_memory",
        lambda ctx, **kwargs: json.dumps({"id": "legacy-research-finding"}),
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_5cb4d4af:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = phase_contract_handlers._palace_add(
        ctx,
        title="Current GitHub prior-art source",
        content=(
            "sonpiaz/4x-game-agent is a Python 4X game agent framework "
            "using an LLM-powered hybrid architecture."
        ),
        kind="research_finding",
        source_id="github:sonpiaz/4x-game-agent",
        evidence_kind="github_project",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["source_path"] == "github:sonpiaz/4x-game-agent"
    assert payload["kind"] == "research_finding"


def test_research_palace_add_rejects_palace_search_as_finding_source(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_510d982f:research",
                "tool": "palace_search",
                "args": {"query": "game simulation AI bot strategy architecture"},
                "result_preview": json.dumps(
                    {
                        "palace_memory": [
                            {
                                "id": "drawer-old",
                                "content": (
                                    "Stale memory says browser strategy games "
                                    "used adaptive gameplay."
                                ),
                            }
                        ],
                        "include_unverified": False,
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        phase_contract_handlers,
        "_save_umbrella_memory",
        lambda ctx, **kwargs: json.dumps({"id": "legacy-research-finding"}),
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_510d982f:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = phase_contract_handlers._palace_add(
        ctx,
        title="Palace memory strategy pattern",
        content=(
            "Palace search revealed browser strategy-game memory with adaptive "
            "gameplay patterns."
        ),
        kind="research_finding",
        source_id="palace_search:game simulation AI bot strategy architecture",
    )

    assert result.startswith("ERROR:")
    assert "memory recall, not current source provenance" in result
    assert "kind=observation" in result


def test_research_palace_add_rejects_tool_qualified_finding_not_grounded_in_rows(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_98b70476:research",
                "tool": "mcp_discover",
                "args": {"query": "game AI simulation", "max_results": 5},
                "result_preview": json.dumps(
                    {
                        "status": "ok",
                        "query": "game AI simulation",
                        "results": [
                            {
                                "name": "nikhilkichili/nba-analytics-mcp",
                                "url": "https://github.com/nikhilkichili/nba-analytics-mcp",
                            },
                            {
                                "name": "geeks-accelerator/animal-house-ai-tamagotchi",
                                "url": "https://github.com/geeks-accelerator/animal-house-ai-tamagotchi",
                            },
                        ],
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        phase_contract_handlers,
        "_save_umbrella_memory",
        lambda ctx, **kwargs: json.dumps({"id": "legacy-research-finding"}),
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_98b70476:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    bad = phase_contract_handlers._palace_add(
        ctx,
        title="Available MCP Servers for Game AI/Simulation",
        content=(
            "MCP discovery found @modelcontextprotocol/server-puppeteer, "
            "server-kafka, and server-gdrive for game testing."
        ),
        kind="research_finding",
        source_id="mcp_discover:game AI simulation",
    )

    assert bad.startswith("ERROR:")
    assert "does not mention any concrete result item" in bad
    assert "nikhilkichili/nba-analytics-mcp" in bad

    good = phase_contract_handlers._palace_add(
        ctx,
        title="Current MCP discovery source",
        content=(
            "nikhilkichili/nba-analytics-mcp is the concrete MCP result "
            "returned for game simulation discovery."
        ),
        kind="research_finding",
        source_id="mcp_discover:game AI simulation",
    )
    payload = json.loads(good)
    assert payload["saved"] is True
    assert payload["kind"] == "research_finding"


def test_research_palace_add_accepts_deep_search_from_truncated_raw_sources(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_4eaea518:research",
                "tool": "deep_search",
                "args": {
                    "query": (
                        "LLM AI opponent game economy diplomacy decision "
                        "making examples"
                    ),
                    "intent": "planner_research",
                    "max_results": 5,
                },
                "result_preview": (
                    '{"status": "ok", "query": "LLM AI opponent game economy '
                    'diplomacy decision making examples", "intent": '
                    '"planner_research", "answer": "Found 5 result(s):\\n\\n'
                    '[1] AI Diplomacy: LLM-Powered Strategic Gameplay - GitHub\\n'
                    '    URL: https://github.com/GoodStartLabs/AI_Diplomacy\\n'
                    '    Each power is controlled by an autonomous LLM agent.", '
                    '"sources": [{"title": "AI Diplomacy: LLM-Powered Strategic '
                    'Gameplay", "url": "https://github.com/GoodStartLabs/AI_Diplomacy"}], ...'
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        phase_contract_handlers,
        "_save_umbrella_memory",
        lambda ctx, **kwargs: json.dumps({"id": "legacy-research-finding"}),
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_4eaea518:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = phase_contract_handlers._palace_add(
        ctx,
        title="LLM diplomacy prior art",
        content=(
            "GoodStartLabs/AI_Diplomacy shows LLM-powered diplomacy agents "
            "with state, relationships, negotiation, and strategic decisions."
        ),
        kind="research_finding",
        source_id=(
            "deep_search:LLM AI opponent game economy diplomacy decision "
            "making examples"
        ),
        evidence_kind="relevant_prior_art",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "research_finding"
    assert payload["source_path"].startswith("deep_search:LLM AI opponent")


def test_research_summary_source_scarce_requires_each_usable_github_row(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    github_row = {
        "task_id": "phase_web_cadd6381:research",
        "tool": "github_project_search",
        "args": {"query": "python turn-based game", "max_repos": 3},
        "result_preview": json.dumps(
            {
                "status": "ok",
                "query": "python turn-based game",
                "results": [
                    {
                        "full_name": "Grimmys/rpg_tactical_fantasy_game",
                        "html_url": "https://github.com/Grimmys/rpg_tactical_fantasy_game",
                    },
                    {
                        "full_name": "marblexu/PythonStrategyRPG",
                        "html_url": "https://github.com/marblexu/PythonStrategyRPG",
                    },
                    {
                        "full_name": "ben-ryder/Conqueror-of-Empires",
                        "html_url": "https://github.com/ben-ryder/Conqueror-of-Empires",
                    },
                ],
            }
        ),
    }
    web_failed = {
        "task_id": "phase_web_cadd6381:research",
        "tool": "web_search",
        "args": {"query": "LLM strategy game"},
        "result_preview": json.dumps(
            {
                "status": "provider_error",
                "query": "LLM strategy game",
                "sources": [],
                "attempts": [],
            }
        ),
    }
    deep_failed = {
        "task_id": "phase_web_cadd6381:research",
        "tool": "deep_search",
        "args": {"query": "LLM strategy game"},
        "result_preview": json.dumps(
            {
                "status": "provider_error",
                "query": "LLM strategy game",
                "sources": [],
                "attempts": [],
            }
        ),
    }
    mcp_empty = {
        "task_id": "phase_web_cadd6381:research",
        "tool": "mcp_discover",
        "args": {"query": "strategy game"},
        "result_preview": json.dumps(
            {"status": "ok", "query": "strategy game", "results": []}
        ),
    }
    accepted_first = {
        "task_id": "phase_web_cadd6381:research",
        "tool": "palace_add",
        "args": {"kind": "research_finding"},
        "result_preview": json.dumps(
            {
                "saved": True,
                "id": "finding-1",
                "kind": "research_finding",
                "source_path": "github:Grimmys/rpg_tactical_fantasy_game",
            }
        ),
    }
    (logs / "tools.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [github_row, web_failed, deep_failed, mcp_empty, accepted_first]
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_cadd6381:research"
    ctx.loop_state_view = {"phase_label": "research"}
    ctx.context_overlays = {
        "phase_manifest": {
            "exit_criteria": {
                "min_palace_writes": [{"store": "palace.run", "n": 3}]
            }
        },
    }

    blocked = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-1"],
        coverage_status="source_scarce",
        source_scarcity_reason="Only the first usable repo was harvested.",
    )

    assert blocked.startswith("ERROR:")
    assert "unharvested usable source evidence: 1/3" in blocked
    assert "`github:marblexu/PythonStrategyRPG`" in blocked
    assert "`github:ben-ryder/Conqueror-of-Empires`" in blocked

    accepted_more = [
        {
            "task_id": "phase_web_cadd6381:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-2",
                    "kind": "research_finding",
                    "source_path": "github:marblexu/PythonStrategyRPG",
                }
            ),
        },
        {
            "task_id": "phase_web_cadd6381:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-3",
                    "kind": "research_finding",
                    "source_path": "github:ben-ryder/Conqueror-of-Empires",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                github_row,
                web_failed,
                deep_failed,
                mcp_empty,
                accepted_first,
                *accepted_more,
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ok = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-1", "finding-2", "finding-3"],
        coverage_status="source_scarce",
        source_scarcity_reason="All usable GitHub rows were harvested.",
    )

    assert ok.startswith("OK:")


def test_research_summary_source_scarce_counts_truncated_github_rows(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    github_row = {
        "task_id": "phase_web_17d93f38:research",
        "tool": "github_project_search",
        "args": {"query": "civ civilization strategy game", "max_repos": 5},
        "result_preview": (
            '{"status": "ok", "query": "civ civilization strategy game", '
            '"results": [{"full_name": "bigai-ai/civrealm", '
            '"html_url": "https://github.com/bigai-ai/civrealm"}, '
            '{"full_name": "pixlark/Civilization-V", '
            '"html_url": "https://github.com/pixlark/Civilization-V"}, '
            '... "full_name": "pikodrak/pikodrak-game-civgame", '
            '"html_url": "https://github.com/pikodrak/pikodrak-game-civgame"}, '
            '{"name": "civ-builder", "full_name": "jcarn/civ-builder", '
            '"html_url": "https://github.com/jcarn/civ-builder"}]}'
        ),
    }
    rows = [
        github_row,
        {
            "task_id": "phase_web_17d93f38:research",
            "tool": "web_search",
            "args": {"query": "LLM strategy game"},
            "result_preview": json.dumps({"status": "provider_error", "sources": []}),
        },
        {
            "task_id": "phase_web_17d93f38:research",
            "tool": "deep_search",
            "args": {"query": "LLM strategy game"},
            "result_preview": json.dumps({"status": "provider_error", "sources": []}),
        },
        {
            "task_id": "phase_web_17d93f38:research",
            "tool": "mcp_discover",
            "args": {"query": "game development framework web server"},
            "result_preview": json.dumps({"status": "ok", "results": []}),
        },
        {
            "task_id": "phase_web_17d93f38:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-1",
                    "kind": "research_finding",
                    "source_path": "github:bigai-ai/civrealm",
                }
            ),
        },
        {
            "task_id": "phase_web_17d93f38:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-2",
                    "kind": "research_finding",
                    "source_path": "github:pikodrak/pikodrak-game-civgame",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_17d93f38:research"
    ctx.loop_state_view = {"phase_label": "research"}
    ctx.context_overlays = {
        "phase_manifest": {
            "exit_criteria": {
                "min_palace_writes": [{"store": "palace.run", "n": 3}]
            }
        },
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-1", "finding-2"],
        coverage_status="source_scarce",
        source_scarcity_reason="Only two GitHub repositories were harvested.",
    )

    assert result.startswith("ERROR:")
    assert "unharvested usable source evidence: 2/3" in result
    assert "`github:jcarn/civ-builder`" in result


def test_research_summary_empty_findings_suggests_recent_usable_source(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_fc69f439:research",
                "tool": "github_project_search",
                "args": {
                    "query": "python game engine web browser",
                    "max_results": 5,
                },
                "result_preview": json.dumps(
                    {
                        "status": "ok",
                        "query": "python game engine web browser",
                        "results": [
                            {
                                "full_name": "harsoradheer19-hub/Gesture-Fruit-Ninja-",
                                "html_url": "https://github.com/harsoradheer19-hub/Gesture-Fruit-Ninja-",
                            }
                        ],
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_fc69f439:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-llm-game-v1",
        findings_ids=[],
        coverage_status="source_scarce",
        source_scarcity_reason="Discovery produced only one usable GitHub row.",
    )

    assert result.startswith("ERROR:")
    assert "Recent usable discovery source candidate" in result
    assert "`github:harsoradheer19-hub/Gesture-Fruit-Ninja-`" in result
    assert "call `palace_add` with `kind=\"research_finding\"`" in result


def test_research_summary_unknown_ids_suggests_recent_usable_source(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_fc69f439:research",
                "tool": "github_project_search",
                "args": {
                    "query": "python game engine web browser",
                    "max_results": 5,
                },
                "result_preview": json.dumps(
                    {
                        "status": "ok",
                        "query": "python game engine web browser",
                        "results": [
                            {
                                "full_name": "harsoradheer19-hub/Gesture-Fruit-Ninja-",
                                "html_url": "https://github.com/harsoradheer19-hub/Gesture-Fruit-Ninja-",
                            }
                        ],
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_fc69f439:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-llm-game-v1",
        findings_ids=["drawer_observation_not_a_finding"],
        coverage_status="complete",
    )

    assert result.startswith("ERROR:")
    assert "Known ids: none" in result
    assert "Recent usable discovery source candidate" in result
    assert "`github:harsoradheer19-hub/Gesture-Fruit-Ninja-`" in result
