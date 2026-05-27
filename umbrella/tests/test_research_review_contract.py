import json
from pathlib import Path

from umbrella.deep_agent_tools import phase_contract_handlers
from umbrella.deep_agent_tools.phase_control_actions import (
    _submit_micro_review,
    _submit_research_summary,
)
from umbrella.deep_agent_tools.phase_control_common import ToolContext
from umbrella.contracts.schemas import FULL_REVIEW_COVERAGE


_RESEARCH_HANDOFF_NOTES = (
    "Research handoff notes for contract tests with enough detail "
    "to satisfy minimum handoff length requirements."
)


def _seed_capability_declaration(state: Path) -> None:
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "capabilities": {"python": {"available": True, "source": "probe"}},
                "discovery_channels": [
                    {
                        "tool": "github_project_search",
                        "outcome": "ok",
                        "notes": "github search ok",
                    },
                    {
                        "tool": "web_search",
                        "outcome": "provider_error",
                        "notes": "web search unavailable",
                    },
                ],
                "notes": "Submitted capability declaration for research contract tests.",
            }
        ),
        encoding="utf-8",
    )


def test_research_summary_depth_none_allows_zero_findings(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_local_fix:research"
    ctx.context_overlays = {
        "research_depth": "none",
        "phase_manifest": {
            "exit_criteria": {
                "min_palace_writes": [{"store": "palace.run", "n": 3}]
            }
        },
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-local-fix-v1",
        findings_ids=[],
        coverage_status="source_scarce",
        source_scarcity_reason="Research depth none; no external findings needed.",
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert result.startswith("OK:")


def test_research_review_revise_cannot_demote_current_source_scarce_finding(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
        coverage=FULL_REVIEW_COVERAGE,
    )

    assert result.startswith("ERROR:")
    assert "already cites accepted current-run research finding" in result


def test_research_review_false_coverage_gets_phase_specific_guidance(tmp_path):
    drive = tmp_path / "workspaces" / "calculator" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_calc:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}
    coverage = dict(FULL_REVIEW_COVERAGE)
    coverage["oracle_compatibility"] = False

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        issues=[],
        coverage=coverage,
        notes="Oracle details are not yet applicable, but the handoff is otherwise acceptable.",
    )

    assert result.startswith("ERROR:")
    assert "research_review" in result
    assert "not directly applicable" in result
    assert "Use verdict `revise`" in result


def test_research_review_uses_latest_attempt_suffixed_research_task(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    research_task_id = "phase_web_retry:research:1779727076594"
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_retry",
                "task_id": research_task_id,
                "coverage_status": "source_scarce",
                "findings_ids": ["finding-latest"],
                "notes": "Latest suffixed research attempt cites a current accepted finding.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": research_task_id,
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-latest",
                    "kind": "research_finding",
                    "source_path": "github:owner/repo",
                }
            ),
        },
        {
            "task_id": "phase_web_retry:research_review:1779727220000",
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
    ctx.task_id = "phase_web_retry:research_review:1779727220000"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "insufficient_research_evidence",
                "severity": "blocking",
                "phase": "research",
                "message": "palace_search recall missed suffixed attempt findings",
            }
        ],
        loop_back_target="research",
        notes="Loop back because stale memory claims current findings are absent.",
        coverage=FULL_REVIEW_COVERAGE,
    )

    assert result.startswith("ERROR:")
    assert "already cites accepted current-run research finding" in result


def test_research_review_revise_cannot_demote_complete_current_findings(tmp_path):
    drive = tmp_path / "workspaces" / "calculator" / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_calc",
                "coverage_status": "complete",
                "findings_ids": ["finding-gui", "finding-code"],
                "notes": "Complete handoff with accepted current-run findings.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "phase_web_calc:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-gui",
                    "kind": "research_finding",
                    "source_path": "web_search:tkinter calculator",
                }
            ),
        },
        {
            "task_id": "phase_web_calc:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-code",
                    "kind": "research_finding",
                    "source_path": "github:owner/calculator",
                }
            ),
        },
        {
            "task_id": "phase_web_calc:research_review",
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
    ctx.task_id = "phase_web_calc:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "insufficient_research_evidence",
                "severity": "blocking",
                "phase": "research",
                "message": "Research summary cites finding IDs that were not found in palace.",
            }
        ],
        loop_back_target="research",
        notes="palace_search recall did not return the current finding IDs.",
        coverage=FULL_REVIEW_COVERAGE,
    )

    assert result.startswith("ERROR:")
    assert "palace_search recall can be incomplete" in result


def test_plan_review_allows_static_analysis_feedback_for_non_llm_entrypoint(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "calculator" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_calc:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "weak_proof",
                "severity": "blocking",
                "phase": "plan",
                "subtask_id": "launcher-script",
                "message": (
                    "launcher-script proof can use static analysis of "
                    "__main__.py to verify the __name__ guard before execute."
                ),
            }
        ],
        required_plan_changes=[
            "Use static analysis of __main__.py guard or a real module invocation."
        ],
        loop_back_target="plan",
        notes="This is ordinary Python entrypoint proof feedback, not AI runtime fallback.",
        coverage=FULL_REVIEW_COVERAGE,
    )

    assert result.startswith("OK:")


def test_plan_review_does_not_regex_block_llm_fallback_wording(tmp_path):
    drive = tmp_path / "workspaces" / "chatbot" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_bot:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "policy_violation",
                "severity": "blocking",
                "phase": "plan",
                "subtask_id": "runtime",
                "message": (
                    "Add a static fallback mode for LLM model decisions when "
                    "credentials are absent."
                ),
            }
        ],
        required_plan_changes=[
            "Runtime should provide a static fallback for LLM decisions."
        ],
        loop_back_target="plan",
        notes="The generated project depends on LLM runtime behavior.",
        coverage=FULL_REVIEW_COVERAGE,
    )

    assert result.startswith("OK:")


def test_research_palace_add_can_read_tool_rows_without_name_error(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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


def test_research_palace_add_rejects_github_query_grounded_only_by_repo_name(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "calculator" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_calc:research",
                "tool": "github_project_search",
                "args": {"query": "simple calculator GUI Python Tkinter"},
                "result_preview": json.dumps(
                    {
                        "status": "ok",
                        "query": "simple calculator GUI Python Tkinter",
                        "results": [
                            {
                                "name": "calculator",
                                "full_name": "ErmiasBahru/calculator",
                                "html_url": "https://github.com/ErmiasBahru/calculator",
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
    ctx.task_id = "phase_web_calc:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "calculator",
    }

    result = phase_contract_handlers._palace_add(
        ctx,
        content=(
            "The calculator task needs a GUI with number buttons, arithmetic "
            "operators, and a launchable interface."
        ),
        kind="research_finding",
        source_id="github_project_search:simple calculator GUI Python Tkinter",
        evidence_kind="architecture_pattern",
    )

    assert result.startswith("ERROR:")
    assert "does not mention any concrete result item" in result
    assert "ErmiasBahru/calculator" in result


def test_research_palace_add_ignores_llm_alias_mentions_in_evidence_metadata(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "calculator" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_calc:research",
                "tool": "web_search",
                "args": {"query": "python tkinter calculator gui"},
                "result_preview": json.dumps(
                    {
                        "status": "ok",
                        "query": "python tkinter calculator gui",
                        "results": [
                            {
                                "title": "Tkinter calculator tutorial",
                                "url": "https://example.com/tkinter-calculator",
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
        lambda ctx, **kwargs: json.dumps({"id": "gui-task-finding"}),
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_calc:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "calculator",
    }

    result = phase_contract_handlers._palace_add(
        ctx,
        content=(
            "Tkinter calculator tutorial at https://example.com/tkinter-calculator "
            "shows a button-grid GUI pattern for a simple calculator."
        ),
        kind="research_finding",
        source_id="web_search:python tkinter calculator gui",
        evidence_kind="web_search_result",
        provenance_note=(
            "metadata-only disclaimer mentions LLM_API_KEY and OPENAI_API_KEY; "
            "GUI task does not require LLM runtime"
        ),
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert "public LLM runtime aliases" not in result


def test_research_palace_add_accepts_non_llm_task_alias_irrelevant_note(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "calculator" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    (logs / "tools.jsonl").write_text(
        json.dumps(
                {
                    "task_id": "phase_web_calc:research",
                    "tool": "web_search",
                    "args": {"query": "python tkinter calculator gui"},
                    "result_preview": json.dumps(
                        {
                            "status": "ok",
                            "query": "python tkinter calculator gui",
                            "results": [
                                {
                                    "title": "Tkinter calculator tutorial",
                                    "url": "https://example.com/tkinter-calculator",
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
        lambda ctx, **kwargs: json.dumps({"id": "non-llm-gui-finding"}),
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_calc:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "calculator",
    }

    result = phase_contract_handlers._palace_add(
        ctx,
        content=(
            "This calculator is not an LLM/GMAS/bot task. It is a local Tkinter "
            "GUI; the Tkinter calculator tutorial at https://example.com/tkinter-calculator "
            "shows the button-grid pattern, so LLM_API_KEY/LLM_BASE_URL/LLM_MODEL "
            "are irrelevant to this runtime."
        ),
        kind="research_finding",
        source_id="web_search:python tkinter calculator gui",
        evidence_kind="web_search_result",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert "incomplete LLM runtime env contract" not in result


def test_research_palace_add_accepts_repo_handle_from_truncated_github_preview(
    tmp_path, monkeypatch
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
        notes=_RESEARCH_HANDOFF_NOTES,
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
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert ok.startswith("OK:")


def test_research_summary_requires_declared_skill_load(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    rows = [
        {
            "task_id": "phase_web_skill:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-1",
                    "kind": "research_finding",
                    "source_path": "web_search:LLM game architecture",
                }
            ),
        }
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_skill:research"
    ctx.context_overlays = {
        "phase_manifest": {"allowed_skills": ["research-strategy", "architecture-author"]}
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-llm-game-v1",
        findings_ids=["finding-1"],
        coverage_status="complete",
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert result.startswith("ERROR:")
    assert "missing required skill coverage" in result
    assert "load_skill" in result


def test_research_summary_requires_gmas_context_for_gmas_workspace(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    rows = [
        {
            "task_id": "phase_web_gmas:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-1",
                    "kind": "research_finding",
                    "source_path": "web_search:LLM game architecture",
                }
            ),
        }
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_gmas:research"
    ctx.context_overlays = {
        "phase_manifest": {"allowed_skills": []},
        "gmas_prewrite_required": True,
        "detected_domains": ["multi_agent_gmas"],
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-1"],
        coverage_status="complete",
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert result.startswith("ERROR:")
    assert "missing GMAS context coverage" in result
    assert "get_gmas_context" in result


def test_research_summary_accepts_truncated_gmas_context_preview(tmp_path):
    from ouroboros.limits import TOOL_LOG_PREVIEW_CHARS
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    full_payload = {
        "query": "GMAS bot leader architecture",
        "recommended_pattern": "Hybrid deterministic-LLM game loop",
        "confidence": 1.0,
        "key_symbols": ["gmas.builder.GraphBuilder"],
        "padding": "x" * 2500,
    }
    truncated = json.dumps(full_payload)[:TOOL_LOG_PREVIEW_CHARS]
    rows = [
        {
            "task_id": "phase_web_trunc_gmas:research",
            "tool": "get_gmas_context",
            "args": {"query": "GMAS bot leader architecture"},
            "result_preview": truncated,
        },
        {
            "task_id": "phase_web_trunc_gmas:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-1",
                    "kind": "research_finding",
                    "source_path": "web_search:LLM game architecture",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_trunc_gmas:research"
    ctx.context_overlays = {
        "phase_manifest": {"allowed_skills": []},
        "gmas_prewrite_required": True,
        "detected_domains": ["multi_agent_gmas"],
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-1"],
        coverage_status="source_scarce",
        source_scarcity_reason="Truncated GMAS preview still counts as coverage.",
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert "missing GMAS context coverage" not in result


def test_research_summary_rejects_blocked_gmas_context_for_gmas_workspace(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    rows = [
        {
            "task_id": "phase_web_gmas_blocked:research",
            "tool": "get_gmas_context",
            "args": {"query": "multi-agent LLM bot strategy game"},
            "result_preview": json.dumps(
                {
                    "status": "blocked",
                    "reason": "gmas_context_query_too_generic",
                    "query": "multi-agent LLM bot strategy game",
                }
            ),
        },
        {
            "task_id": "phase_web_gmas_blocked:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-1",
                    "kind": "research_finding",
                    "source_path": "web_search:LLM game architecture",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_gmas_blocked:research"
    ctx.context_overlays = {
        "phase_manifest": {"allowed_skills": []},
        "gmas_prewrite_required": True,
        "detected_domains": ["multi_agent_gmas"],
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-1"],
        coverage_status="complete",
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert result.startswith("ERROR:")
    assert "missing GMAS context coverage" in result


def test_research_palace_add_evidence_kind_observation_stays_observation(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_obs:research"
    ctx.context_overlays = {"phase_node": {"id": "research", "manifest_id": "research"}}

    result = phase_contract_handlers._palace_add(
        ctx,
        content="This is a research note, not a counted finding.",
        evidence_kind="observation",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["kind"] == "observation"


def test_research_summary_source_scarce_counts_truncated_github_rows(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert result.startswith("ERROR:")
    assert "unharvested usable source evidence: 2/3" in result
    assert "`github:jcarn/civ-builder`" in result


def test_research_summary_empty_findings_suggests_recent_usable_source(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert result.startswith("ERROR:")
    assert "Recent usable discovery source candidate" in result
    assert "`github:harsoradheer19-hub/Gesture-Fruit-Ninja-`" in result
    assert "call `palace_add` with `kind=\"research_finding\"`" in result


def test_research_summary_unknown_ids_suggests_recent_usable_source(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    state = drive / "state"
    state.mkdir(parents=True)
    _seed_capability_declaration(state)
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
        notes=_RESEARCH_HANDOFF_NOTES,
    )

    assert result.startswith("ERROR:")
    assert "Known ids: none" in result
    assert "Recent usable discovery source candidate" in result
    assert "`github:harsoradheer19-hub/Gesture-Fruit-Ninja-`" in result


def test_github_namespace_accepted_from_deep_search_anchor() -> None:
    from umbrella.deep_agent_tools.research_provenance import (
        github_namespace_seen_in_discovery,
        research_finding_source_provenance_issue,
    )

    rows = [
        {
            "tool": "deep_search",
            "args": {"query": "hybrid LLM game AI"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "results": [
                        {
                            "html_url": "https://github.com/vox-deorum/vox-deorum",
                            "title": "Vox Deorum",
                        }
                    ],
                }
            ),
        }
    ]
    assert github_namespace_seen_in_discovery(rows, "vox-deorum/vox-deorum")
    assert research_finding_source_provenance_issue(
        rows, source_id="github:vox-deorum/vox-deorum"
    ) == ""
