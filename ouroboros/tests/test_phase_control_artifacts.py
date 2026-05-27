import json

import pytest

from ouroboros.tools.phase_control import (
    _loop_back_to,
    _mark_subtask_complete,
    _mutate_phase_plan,
    _phase_subtask_completion_issue,
    _referenced_workspace_paths,
    _submit_research_summary,
)
from ouroboros.tools.registry import ToolContext
from umbrella.contracts.schemas import FULL_REVIEW_COVERAGE
from umbrella.deep_agent_tools.phase_control_actions import _submit_micro_review as _submit_micro_review_impl
from umbrella.deep_agent_tools.phase_control_retry import _completion_llm_memory_claim_issue
from umbrella.orchestrator.phase_plan import load_plan, save_plan


def _submit_micro_review(
    ctx,
    *,
    verdict,
    issues=None,
    revisions=None,
    notes="",
    coverage=None,
    **kwargs,
):
    phase = str(getattr(ctx, "task_id", "") or "").split(":")[-1]
    if coverage is None and phase.endswith("_review"):
        coverage = FULL_REVIEW_COVERAGE
    return _submit_micro_review_impl(
        ctx,
        verdict=verdict,
        issues=issues,
        revisions=revisions,
        notes=notes,
        coverage=coverage,
        **kwargs,
    )


def test_completion_memory_rejects_control_plane_llm_alias_leak() -> None:
    issue = _completion_llm_memory_claim_issue(
        subtask_id="docs-env",
        summary=(
            "Implemented docs that support LLM_API_KEY, LLM_BASE_URL, LLM_MODEL "
            "and OUROBOROS_MODEL compatibility."
        ),
        evidence=[],
    )

    assert "ERROR: mark_subtask_complete rejected" in issue
    assert "Generated workspace code/tests/docs must use the public aliases" in issue
    assert "LLM_API_KEY" in issue


def test_submit_research_summary_persists_latest_artifact(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    drive.mkdir(parents=True)
    logs = drive / "logs"
    logs.mkdir()
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "id": "memory-1",
                        "legacy": {"id": "finding-1"},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete research notes that are useful for planning.",
    )

    assert result.startswith("OK: Research summary submitted")
    latest = json.loads(
        (drive / "state" / "research_summary_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["run_id"] == "run-1"
    assert latest["workspace_id"] == "mini_game"
    assert latest["phase"] == "research"
    assert latest["architecture_id"] == "arch-1"
    assert latest["findings_ids"] == ["memory-1"]
    ledger = (drive / "state" / "research_summaries.jsonl").read_text(
        encoding="utf-8"
    )
    assert "Concrete research notes" in ledger


def test_submit_research_summary_rejects_stale_capability_probe_todo(tmp_path):
    drive = tmp_path / "workspaces" / "calculator" / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    state.mkdir()
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "run_id": "run-1",
                "workspace_id": "calculator",
                "capabilities": {
                    "desktop_gui_runtime": {
                        "available": True,
                        "source": "probe",
                        "probe": {
                            "kind": "command",
                            "command": [
                                "python",
                                "-c",
                                "import tkinter as tk; root=tk.Tk(); root.destroy()",
                            ],
                        },
                    }
                },
                "probe_audit": {"desktop_gui_runtime": True},
                "notes": "Desktop GUI runtime probe passed.",
            }
        ),
        encoding="utf-8",
    )
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "args": {
                    "kind": "research_finding",
                    "source_id": "github:owner/repo",
                },
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "verified": True,
                        "id": "finding-1",
                        "kind": "research_finding",
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-calculator",
        findings_ids=["finding-1"],
        notes=(
            "Implementation strategy: Probe desktop GUI runtime capability "
            "before building the calculator UI."
        ),
    )

    assert result.startswith("ERROR:"), result
    assert "contradicts capability_declaration" in result


def test_submit_research_summary_rejects_captured_progress_ledger_finding(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_e4cde249:research",
                "tool": "palace_add",
                "args": {
                    "content": (
                        "Research evidence ledger - Current finding attempts: "
                        "0/3 accepted. Continue gathering evidence."
                    ),
                },
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "id": "ledger-1",
                        "kind": "research_finding",
                        "legacy": {"id": "drawer_ledger"},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_e4cde249:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "mini_game",
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["ledger-1"],
        notes="Concrete research notes that should cite real findings only.",
    )

    assert result.startswith("ERROR:")
    assert "not accepted" in result


def test_submit_research_summary_rejects_captured_continue_note_finding(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_e8afe5ca:research",
                "tool": "palace_add",
                "args": {
                    "content": (
                        "I need to continue researching and make at least 3 "
                        "palace_add calls before submit_research_summary. Let "
                        "me explore more specific patterns for game AI and "
                        "turn-based strategy."
                    ),
                    "source_id": "ouros",
                    "evidence_kind": "hypothesis",
                },
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "id": "6ed6f79d-7f31-4366-9a03-17a64b02dc1c",
                        "kind": "research_finding",
                        "verified": False,
                        "legacy": {"id": "drawer_682ab99895bb8134ca17604f"},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_e8afe5ca:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["6ed6f79d-7f31-4366-9a03-17a64b02dc1c"],
        notes="Concrete research notes that should cite real findings only.",
    )

    assert result.startswith("ERROR:")
    assert "not accepted" in result


def test_submit_research_summary_rejects_explicit_unverified_finding(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_6c2e6608:research",
                "tool": "palace_add",
                "args": {
                    "content": (
                        "Current workspace state: workspace.toml confirms "
                        "multi_agent_gmas skill is enabled."
                    ),
                    "evidence_kind": "observation_from_log",
                },
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "id": "28a3ef0e-25fc-4770-99ab-55692c51884d",
                        "kind": "research_finding",
                        "verified": False,
                        "legacy": {"id": "drawer_4ba25a0e63439b7588ab1e03"},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_6c2e6608:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["28a3ef0e-25fc-4770-99ab-55692c51884d"],
        notes="Concrete research notes that should cite verified findings only.",
    )

    assert result.startswith("ERROR:")
    assert "not accepted" in result


def test_submit_research_summary_accepts_captured_verified_research_finding(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    finding_id = "a6a95dfe-9a76-462a-9655-271e03021c4b"
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_20eb1a6a:research",
                "tool": "palace_add",
                "args": {
                    "content": (
                        "Search for civilization game python TypeScript LLM "
                        "AI bots returned no direct results, so the build needs "
                        "separate web-game, strategy, and LLM-agent patterns."
                    ),
                    "kind": "research_finding",
                    "workspace_id": "civilization",
                    "tags": "github_search,civilization_game,architecture_discovery",
                },
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "id": finding_id,
                        "kind": "research_finding",
                        "verified": True,
                        "legacy": {"id": "drawer_d62de1d015756e788ef4a6bc"},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_20eb1a6a:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "civilization",
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=[finding_id],
        notes="Concrete research notes cite a verified current finding.",
    )

    assert result.startswith("OK: Research summary submitted"), result


def test_submit_research_summary_does_not_count_architecture_as_finding(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_5a090940:research",
                "tool": "palace_add",
                "args": {
                    "kind": "architecture",
                    "content": "Architecture decision: use GMAS plus FastAPI.",
                },
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "id": "arch-memory-1",
                        "kind": "architecture",
                        "legacy": {"id": "drawer_arch"},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_5a090940:research"
    ctx.loop_state_view = {
        "phase_label": "research",
        "active_workspace_id": "mini_game",
    }

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["drawer_arch"],
        notes="Concrete research notes that should cite findings, not architecture memory.",
    )

    assert result.startswith("ERROR:")
    assert "not accepted" in result


def test_phase_control_signal_derives_phase_from_task_id_when_label_is_linear(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "exit_criteria": {
                    "min_palace_writes": [{"store": "palace.run", "n": 3}]
                }
            }
        },
    )
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=["blocking success_test is missing"],
    )

    assert result.startswith("OK: Micro-review submitted: revise")
    row = json.loads(
        (drive / "state" / "phase_control_signals.jsonl").read_text(
            encoding="utf-8"
        )
    )
    assert row["phase"] == "plan_review"


def test_submit_research_summary_rejects_unknown_finding_ids(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding_001"],
        notes="Concrete research notes with enough context for planning.",
    )

    assert result.startswith("ERROR:")
    assert "not accepted by palace_add" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_rejects_llm_handoff_without_env_contract(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_fa9a4d2c:research",
                "tool": "palace_add",
                "args": {
                    "title": "Game Mechanics Scope for Simplified Civilization",
                    "content": (
                        "Turn-based 12x12 map with one human player vs one AI bot. "
                        "The LLM analyzes game state, then LLM calls action tools "
                        "such as trade_proposal, move_unit, and build_structure."
                    ),
                    "tags": "game-mechanics,turn-based",
                },
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "id": "game-mechanics-1",
                        "legacy": {"id": "drawer_game"},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_fa9a4d2c:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["game-mechanics-1"],
        notes=(
            "Research handoff: simplified civilization uses LLM-driven bot "
            "decisions and structured action tools."
        ),
    )

    assert result.startswith("ERROR:")
    assert "omits the standalone LLM runtime env contract" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_submit_research_summary_accepts_domain_finding_when_env_contract_cited(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_fa9a4d2c:research",
            "tool": "palace_add",
            "args": {
                "title": "Game Mechanics Scope for Simplified Civilization",
                "content": (
                    "Turn-based 12x12 map with one human player vs one AI bot. "
                    "The LLM analyzes game state, then LLM calls action tools "
                    "such as trade_proposal, move_unit, and build_structure."
                ),
                "tags": "game-mechanics,turn-based",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "game-mechanics-1",
                    "legacy": {"id": "drawer_game"},
                }
            ),
        },
        {
            "task_id": "phase_web_fa9a4d2c:research",
            "tool": "palace_add",
            "args": {
                "title": "LLM Runtime Environment Contract",
                "content": (
                    "Generated workspace code resolves "
                    "LLM_API_KEY, "
                    "LLM_BASE_URL, and "
                    "LLM_MODEL from the inherited runtime."
                ),
                "tags": "llm-contract,environment,gmas",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "llm-contract-1",
                    "legacy": {"id": "drawer_env"},
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_fa9a4d2c:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["game-mechanics-1", "llm-contract-1"],
        notes=(
            "Research handoff: simplified civilization uses LLM-driven bot "
            "decisions and structured action tools."
        ),
    )

    assert result.startswith("OK: Research summary submitted")
    latest = json.loads(
        (drive / "state" / "research_summary_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["findings_ids"] == ["game-mechanics-1", "llm-contract-1"]


def test_submit_research_summary_rejects_captured_mock_fallback_notes(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_bf471ba0:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "GMAS streaming execution enables real-time conversational "
                    "diplomacy for LLM bots."
                ),
                "tags": "research_finding,gmas,diplomacy",
            },
            "result_preview": json.dumps({"saved": True, "id": "dialogue-1"}),
        },
        {
            "task_id": "phase_web_bf471ba0:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "Economic advisors generate resource allocation decisions "
                    "from LLM reasoning over game-state text."
                ),
                "tags": "research_finding,economy,llm",
            },
            "result_preview": json.dumps({"saved": True, "id": "economy-1"}),
        },
        {
            "task_id": "phase_web_bf471ba0:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "AgentMemory stores diplomatic relationship history and "
                    "past trade outcomes across turns."
                ),
                "tags": "research_finding,memory,gmas",
            },
            "result_preview": json.dumps({"saved": True, "id": "memory-1"}),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_bf471ba0:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-hybrid-llm-simulator-v1",
        findings_ids=["dialogue-1", "economy-1", "memory-1"],
        notes=(
            "Research phase complete for an LLM-driven civilization simulator.\n"
            "All generated workspace code/tests MUST resolve "
            "`OUROBOROS_LLM_API_KEY`/`LLM_API_KEY`, "
            "`OUROBOROS_LLM_BASE_URL`/`LLM_BASE_URL`, and "
            "`OUROBOROS_MODEL`/`LLM_MODEL`.\n"
            "Runtime behavior when credentials absent:\n"
            "- Startup MUST fail fast with clear error message.\n"
            "- Tests MUST skip LLM-dependent tests when credentials are missing.\n"
            "- MUST provide fallback mode (mock/deterministic bots) when LLM unavailable.\n"
            "- UI MUST show clear status: LLM not configured - using mock opponents."
        ),
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_rejects_captured_human_only_fallback_mode(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_52ccc80f:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "FastAPI and WebSockets synchronize game state for a "
                    "turn-based civilization board."
                ),
                "tags": "research_finding,backend",
            },
            "result_preview": json.dumps({"saved": True, "id": "backend-1"}),
        },
        {
            "task_id": "phase_web_52ccc80f:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "GMAS bot agents propose economic, diplomatic, and military "
                    "decisions from natural-language state."
                ),
                "tags": "research_finding,gmas,llm",
            },
            "result_preview": json.dumps({"saved": True, "id": "agents-1"}),
        },
        {
            "task_id": "phase_web_52ccc80f:research",
            "tool": "palace_add",
            "args": {
                "content": "React TypeScript renders board state and chat updates.",
                "tags": "research_finding,frontend",
            },
            "result_preview": json.dumps({"saved": True, "id": "frontend-1"}),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_52ccc80f:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["backend-1", "agents-1", "frontend-1"],
        notes=(
            "Research complete for LLM-driven civilization bots. Required env "
            "vars: LLM_API_KEY, "
            "LLM_BASE_URL, LLM_MODEL. "
            "Failure handling: fail fast if credentials missing, support "
            "human-only fallback mode."
        ),
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_rejects_captured_rule_based_degradation(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_9701612a:research",
            "tool": "palace_add",
            "args": {
                "content": "FastAPI plus Vite synchronize a turn-based map UI.",
                "tags": "research_finding,backend,frontend",
            },
            "result_preview": json.dumps({"saved": True, "id": "backend-1"}),
        },
        {
            "task_id": "phase_web_9701612a:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "GMAS bot agents use the active LLM runtime for diplomacy "
                    "and economy decisions."
                ),
                "tags": "research_finding,gmas,llm",
            },
            "result_preview": json.dumps({"saved": True, "id": "agents-1"}),
        },
        {
            "task_id": "phase_web_9701612a:research",
            "tool": "palace_add",
            "args": {
                "content": "Hex-map mechanics define resources, turns, and victory.",
                "tags": "research_finding,gameplay",
            },
            "result_preview": json.dumps({"saved": True, "id": "gameplay-1"}),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_9701612a:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["backend-1", "agents-1", "gameplay-1"],
        notes=(
            "Revised research phase addressing all revision requirements. "
            "LLM runtime environment contract requires "
            "LLM_API_KEY, "
            "LLM_BASE_URL, and "
            "LLM_MODEL with graceful degradation to "
            "rule-based AI when credentials missing."
        ),
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_rejects_captured_mock_llm_behavior_verification(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_c8307523:research",
            "tool": "palace_add",
            "args": {
                "content": "GMAS GraphBuilder and MACPRunner drive LLM bot turns.",
                "tags": "research_finding,gmas,llm",
            },
            "result_preview": json.dumps({"saved": True, "id": "gmas-1"}),
        },
        {
            "task_id": "phase_web_c8307523:research",
            "tool": "palace_add",
            "args": {
                "content": "FastAPI plus WebSockets synchronize game state.",
                "tags": "research_finding,backend",
            },
            "result_preview": json.dumps({"saved": True, "id": "backend-1"}),
        },
        {
            "task_id": "phase_web_c8307523:research",
            "tool": "palace_add",
            "args": {
                "content": "AgentMemory stores diplomatic history per civilization.",
                "tags": "research_finding,memory,gmas",
            },
            "result_preview": json.dumps({"saved": True, "id": "memory-1"}),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_c8307523:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["gmas-1", "backend-1", "memory-1"],
        notes=(
            "Civilization-style strategy game architecture combining Python "
            "GMAS-based LLM bots with TypeScript/JSX frontend. All LLM bots "
            "must use inherited Umbrella runtime with "
            "LLM_API_KEY, "
            "LLM_BASE_URL, and "
            "LLM_MODEL. Testing strategy combines unit tests "
            "(tool logic, state transitions), integration tests (mock LLM for "
            "bot behavior verification), and runtime validation."
        ),
    )

    assert result.startswith("ERROR:")
    assert "mock/fake/dry-run LLM" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_submit_research_summary_rejects_captured_llm_driven_without_env_contract(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_3c6a6b33:research",
            "tool": "deep_search",
            "args": {"query": "LLM game AI strategy civilizations economic diplomacy"},
            "result_preview": json.dumps({"status": "no_results"}),
        },
        {
            "task_id": "phase_web_3c6a6b33:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "## Finding: Full-Stack Architecture Pattern\n\n"
                    "Python backend with TypeScript/JSX frontend. AI turns "
                    "take longer due to LLM latency and stream to the UI."
                ),
                "tags": "research_finding,architecture",
            },
            "result_preview": json.dumps({"saved": True, "id": "full-stack-1"}),
        },
        {
            "task_id": "phase_web_3c6a6b33:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "## Finding: LLM-Driven Game AI Architecture\n\n"
                    "Economic and diplomatic decisions should be handled via "
                    "LLM agents rather than discrete button-based systems. "
                    "Each AI civilization is a GMAS agent with access to game "
                    "state tools."
                ),
                "tags": "research_finding,ai,architecture",
            },
            "result_preview": json.dumps({"saved": True, "id": "llm-ai-1"}),
        },
        {
            "task_id": "phase_web_3c6a6b33:research",
            "tool": "palace_add",
            "args": {
                "content": (
                    "## Finding: Discovery - LLM Game AI Architecture\n\n"
                    "No existing published patterns found for LLM-driven "
                    "economic/diplomatic AI in Civilization-style games."
                ),
                "tags": "research_finding,discovery,no_results",
            },
            "result_preview": json.dumps({"saved": True, "id": "discovery-1"}),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_3c6a6b33:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["full-stack-1", "llm-ai-1", "discovery-1"],
        notes=(
            "Research complete with architecture blueprint and LLM AI design "
            "plus discovery coverage showing limited existing patterns."
        ),
    )

    assert result.startswith("ERROR:")
    assert "omits the standalone LLM runtime env contract" in result


def test_mutate_phase_plan_updates_execute_subtask_success_test_from_capture(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan_path = state / "phase_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "run_id": "phase_web_c7d5731f",
                "workspace_id": "civilization",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "0_llm_config_setup",
                                "status": "pending",
                                "success_test": {
                                    "kind": "cmd",
                                    "value": (
                                        "pytest tests/test_llm_config.py::"
                                        "test_llm_config_reads_aliases -v"
                                    ),
                                },
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_c7d5731f:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "current_subtask_id": "0_llm_config_setup",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "0_llm_config_setup",
                    "success_test": (
                        "pytest tests/test_llm_config.py::"
                        "TestLLMConfigReadsAliases::"
                        "test_llm_config_reads_aliases -v"
                    ),
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated")
    mutated = json.loads(plan_path.read_text(encoding="utf-8"))
    subtask = mutated["nodes"][0]["subtasks"][0]
    assert subtask["success_test"] == {
        "kind": "cmd",
        "value": (
            "pytest tests/test_llm_config.py::"
            "TestLLMConfigReadsAliases::"
            "test_llm_config_reads_aliases -v"
        ),
    }
    assert mutated["version"] == 2
    assert mutated["edits_log"][-1]["applied"] == ["subtasks.0_llm_config_setup"]
    signals = (state / "phase_control_signals.jsonl").read_text(encoding="utf-8")
    assert "mutate_phase_plan" in signals
    assert "subtasks.0_llm_config_setup" in signals


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_mutate_phase_plan_rejects_captured_direct_python_pytest_command(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan_path = state / "phase_plan.json"
    original = {
        "run_id": "phase_web_11159129",
        "workspace_id": "civilization",
        "version": 1,
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "docs-env-contract",
                        "status": "pending",
                        "files_to_create": [
                            "docs/architecture.md",
                            "docs/env_contract.md",
                            "tests/test_architecture_verification.py",
                        ],
                        "success_test": {
                            "kind": "cmd",
                            "value": (
                                "python -m pytest "
                                "tests/test_architecture_verification.py -q"
                            ),
                        },
                    }
                ],
            }
        ],
    }
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_11159129:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
        "current_subtask_id": "docs-env-contract",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "docs-env-contract",
                    "contract_migration_reason": (
                        "Workspace write policy blocks Python scripts under "
                        "docs/ or scripts/; migrate the proof into pytest."
                    ),
                    "contract_migration_files": [
                        "tests/test_architecture_verification.py"
                    ],
                    "success_test": {
                        "kind": "cmd",
                        "value": "python tests/test_architecture_verification.py -q",
                    },
                }
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan"), result
    assert "python ..." in result
    assert "python -m pytest" in result
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original
    assert not (state / "phase_control_signals.jsonl").exists()


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_mutate_phase_plan_records_contract_migration_reason(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan_path = state / "phase_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "plan_id": "plan-201af72f",
                "run_id": "phase_web_201af72f",
                "workspace_id": "civilization",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "subtask_2_game_model",
                                "status": "pending",
                                "success_test": {
                                    "kind": "cmd",
                                    "value": "pytest tests/test_game_model.py -v -x",
                                },
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_201af72f:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "subtask_2_game_model",
                    "contract_migration_reason": (
                        "Generated trade test expected 70 even though its own "
                        "setup computes 60."
                    ),
                    "contract_migration_files": ["tests/test_game_model.py"],
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated")
    mutated = json.loads(plan_path.read_text(encoding="utf-8"))
    subtask = mutated["nodes"][0]["subtasks"][0]
    assert "expected 70" in subtask["contract_migration_reason"]
    assert subtask["contract_migration_files"] == ["tests/test_game_model.py"]
    loaded = load_plan(drive)
    assert loaded is not None
    save_plan(loaded, drive)
    round_tripped = json.loads(plan_path.read_text(encoding="utf-8"))
    round_trip_subtask = round_tripped["nodes"][0]["subtasks"][0]
    assert "expected 70" in round_trip_subtask["contract_migration_reason"]
    assert round_trip_subtask["contract_migration_files"] == [
        "tests/test_game_model.py"
    ]
    signals = (state / "phase_control_signals.jsonl").read_text(encoding="utf-8")
    assert "mutate_phase_plan" in signals
    assert "contract_migration_reason" in signals


def test_mutate_phase_plan_merges_file_scope_lists_from_captured_setup_patch(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    frontend = tmp_path / "workspaces" / "civilization" / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    (frontend / "tsconfig.json").write_text("{}", encoding="utf-8")
    plan_path = state / "phase_plan.json"
    original_files = [
        "src/civgame/__init__.py",
        "src/civgame/ai/__init__.py",
        "frontend/package.json",
        "frontend/vite.config.ts",
        "frontend/tsconfig.json",
        "frontend/index.html",
        "frontend/src/main.tsx",
        "frontend/src/App.tsx",
        "tests/test_project_structure.py",
        "README.md",
        "docs/architecture.md",
    ]
    plan_path.write_text(
        json.dumps(
            {
                "run_id": "phase_web_d3db1ce5",
                "workspace_id": "civilization",
                "version": 3,
                "nodes": [
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "project-setup",
                                "status": "pending",
                                "success_test": {
                                    "kind": "cmd",
                                    "value": "python -m pytest tests/test_project_structure.py -q",
                                },
                                "files_to_create": original_files,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_d3db1ce5:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "project-setup",
                    "files_to_create": ["frontend/tsconfig.node.json"],
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated")
    mutated = json.loads(plan_path.read_text(encoding="utf-8"))
    files = mutated["nodes"][0]["subtasks"][0]["files_to_create"]
    assert files[: len(original_files)] == original_files
    assert files[-1] == "frontend/tsconfig.node.json"
    assert "existing implementation root" not in result


def test_mutate_phase_plan_rejects_duplicate_subtask_patch_from_capture(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan_path = state / "phase_plan.json"
    original = {
        "run_id": "phase_web_d94824b2",
        "workspace_id": "civilization",
        "version": 2,
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "project-setup-and-domain-state",
                        "status": "pending",
                        "success_test": {
                            "kind": "cmd",
                            "value": "python -m pytest tests/test_game_state.py -q",
                        },
                    }
                ],
            }
        ],
    }
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.current_task_type = "phase_run"
    ctx.task_id = "phase_web_d94824b2:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "project-setup-and-domain-state",
                    "contract_migration_reason": (
                        "The generated test API differs from a clean "
                        "architectural implementation."
                    ),
                    "contract_migration_files": ["tests/test_game_state.py"],
                },
                {
                    "id": "project-setup-and-domain-state",
                    "contract_migration_reason": (
                        "Test file has line ending issues causing import failures."
                    ),
                    "contract_migration_files": ["tests/test_game_state.py"],
                },
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan")
    assert "duplicate patch.subtasks entry" in result
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original
    assert not (state / "phase_control_signals.jsonl").exists()


def test_mutate_phase_plan_rejects_active_success_test_api_preference_capture(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir()
    plan_path = state / "phase_plan.json"
    original = {
        "run_id": "phase_web_d94824b2",
        "workspace_id": "civilization",
        "version": 2,
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "project-setup-and-domain-state",
                        "status": "pending",
                        "success_test": {
                            "kind": "cmd",
                            "value": "python -m pytest tests/test_game_state.py -q",
                        },
                    }
                ],
            }
        ],
    }
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-19T21:35:13.950518+00:00",
                "task_id": "phase_web_d94824b2:execute",
                "tool": "shell",
                "result_preview": json.dumps(
                    {
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_game_state.py",
                            "-q",
                        ],
                        "exit_code": 2,
                        "output": (
                            "ERROR collecting tests/test_game_state.py; "
                            "ImportError while importing test module"
                        ),
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.current_task_type = "phase_run"
    ctx.task_id = "phase_web_d94824b2:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "project-setup-and-domain-state",
                    "contract_migration_reason": (
                        "The test file tests/test_game_state.py was generated "
                        "with an API that significantly differs from a clean "
                        "architectural implementation. These mismatches would "
                        "require rewriting the implementation to match generated "
                        "test expectations rather than following clean architecture."
                    ),
                    "contract_migration_files": ["tests/test_game_state.py"],
                }
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan")
    assert "declared success-test contract migration" in result
    assert "Repair the implementation" in result
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original
    assert not (state / "phase_control_signals.jsonl").exists()


def test_mutate_phase_plan_accepts_watcher_proven_test_contract_contradiction(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir()
    plan_path = state / "phase_plan.json"
    original = {
        "run_id": "phase_web_ba3413d2",
        "workspace_id": "civilization",
        "version": 5,
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "docs-env-contract",
                        "status": "pending",
                        "goal": (
                            "Document real LLM/GMAS runtime aliases "
                            "LLM_API_KEY, "
                            "LLM_BASE_URL, and "
                            "LLM_MODEL."
                        ),
                        "files_to_create": [
                            "README.md",
                            "docs/architecture.md",
                            "tests/test_docs_content.py",
                        ],
                        "success_test": {
                            "kind": "cmd",
                            "value": "python -m pytest tests/test_docs_content.py -q",
                        },
                    }
                ],
            }
        ],
    }
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    failure = {
        "command": ["python", "-m", "pytest", "tests/test_docs_content.py", "-q"],
        "exit_code": 1,
        "output": (
            "AssertionError: sample README contains OUROBOROS_LLM_MODEL while "
            "the same test asserts OUROBOROS_LLM_MODEL must not appear"
        ),
    }
    rows = []
    for idx in range(3):
        rows.append(
            {
                "ts": f"2026-05-20T03:04:{30 + idx:02d}.000000+00:00",
                "task_id": "phase_web_ba3413d2:execute",
                "tool": "shell",
                "result_preview": json.dumps(failure),
            }
        )
    rows.append(
        {
            "ts": "2026-05-20T03:05:38.713143+00:00",
            "task_id": "phase_web_ba3413d2:execute",
            "tool": "request_watcher_review",
            "result_preview": json.dumps(
                {
                    "reviewer": "umbrella",
                    "review_kind": "retry_watcher",
                    "status": "review_recorded",
                    "subtask_id": "docs-env-contract",
                    "success_test": "python -m pytest tests/test_docs_content.py -q",
                    "failed_attempts": 3,
                    "operator_reason": (
                        "The generated test is self-contradictory: its sample "
                        "contains OUROBOROS_LLM_MODEL, then asserts that the "
                        "same substring must not appear."
                    ),
                }
            ),
        }
    )
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.current_task_type = "phase_run"
    ctx.task_id = "phase_web_ba3413d2:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "docs-env-contract",
                    "contract_migration_files": ["tests/test_docs_content.py"],
                    "contract_migration_reason": (
                        "Generated test has internal contradiction. Line 433 "
                        "asserts OUROBOROS_LLM_MODEL must not appear, but "
                        "sample README line 483 includes OUROBOROS_LLM_MODEL. "
                        "The sample violates its own validation rule, while "
                        "the correct runtime model alias remains OUROBOROS_MODEL."
                    ),
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated")
    mutated = json.loads(plan_path.read_text(encoding="utf-8"))
    subtask = mutated["nodes"][0]["subtasks"][0]
    assert "sample violates its own validation rule" in subtask[
        "contract_migration_reason"
    ]
    assert subtask["contract_migration_files"] == ["tests/test_docs_content.py"]
    signals = (state / "phase_control_signals.jsonl").read_text(encoding="utf-8")
    assert "mutate_phase_plan" in signals


def test_mutate_phase_plan_accepts_watcher_proven_structural_toml_test_defect(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir()
    plan_path = state / "phase_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "run_id": "phase_web_8d1bf872",
                "workspace_id": "civilization",
                "version": 4,
                "nodes": [
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "project-setup",
                                "status": "pending",
                                "goal": "Create project package and setup files.",
                                "files_to_create": [
                                    "src/civ/__init__.py",
                                    "pyproject.toml",
                                    "tests/test_setup.py",
                                ],
                                "success_test": {
                                    "kind": "cmd",
                                    "value": "python -m pytest tests/test_setup.py -q",
                                },
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    failure = {
        "command": ["python", "-m", "pytest", "tests/test_setup.py", "-q"],
        "exit_code": 1,
        "output": (
            "AssertionError: pyproject.toml should have GMAS source reference\n"
            "assert 'tool.uv.sources' in {'project': {}, 'tool': {'uv': "
            "{'sources': {'frontier-ai-gmas': {'path': '../gmas'}}}}}"
        ),
    }
    rows = [
        {
            "ts": "2026-05-20T04:29:53.592434+00:00",
            "task_id": "phase_web_8d1bf872:execute",
            "tool": "shell",
            "result_preview": json.dumps(failure),
        },
        {
            "ts": "2026-05-20T04:30:05.862252+00:00",
            "task_id": "phase_web_8d1bf872:execute",
            "tool": "request_watcher_review",
            "result_preview": json.dumps(
                {
                    "reviewer": "umbrella",
                    "review_kind": "retry_watcher",
                    "status": "review_recorded",
                    "subtask_id": "project-setup",
                    "success_test": "python -m pytest tests/test_setup.py -q",
                    "failed_attempts": 1,
                    "operator_reason": (
                        "The test assertion `tool.uv.sources in pyproject` is "
                        "structurally impossible: TOML nested sections become "
                        "nested dictionaries, never a flat key. The generated "
                        "test should check pyproject['tool']['uv']['sources']."
                    ),
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.current_task_type = "phase_run"
    ctx.task_id = "phase_web_8d1bf872:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "project-setup",
                    "contract_migration_files": ["tests/test_setup.py"],
                    "contract_migration_reason": (
                        "Umbrella watcher review recorded the generated "
                        "test-contract defect; migrate the setup test while "
                        "preserving the intended GMAS source verification."
                    ),
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated")
    subtask = json.loads(plan_path.read_text(encoding="utf-8"))["nodes"][0][
        "subtasks"
    ][0]
    assert "test-contract defect" in subtask["contract_migration_reason"]
    assert subtask["contract_migration_files"] == ["tests/test_setup.py"]


def test_mutate_phase_plan_ignores_future_cards_and_runtime_overlay_from_capture(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir()
    (tmp_path / "src" / "game").mkdir(parents=True)
    (tmp_path / "src" / "game" / "state.py").write_text(
        "def create_initial_state():\n    return object()\n",
        encoding="utf-8",
    )
    plan_path = state / "phase_plan.json"
    original = {
        "run_id": "phase_web_110d7ea6",
        "workspace_id": "civilization",
        "version": 6,
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "project-setup",
                        "status": "done",
                        "success_test": {
                            "kind": "cmd",
                            "value": "python -m pytest tests/test_setup.py -q",
                        },
                        "files_to_create": ["src/game/__init__.py"],
                    },
                    {
                        "id": "domain-models",
                        "status": "done",
                        "success_test": {
                            "kind": "cmd",
                            "value": "python -m pytest tests/test_game_state.py -q",
                        },
                        "completion": {
                            "summary": (
                                "Implemented create_initial_state after earlier "
                                "import failures."
                            )
                        },
                    },
                    {
                        "id": "map-engine",
                        "status": "pending",
                        "goal": "Implement hex map generation and terrain logic.",
                        "files_to_create": [
                            "src/game/map.py",
                            "src/game/pathfinding.py",
                            "tests/test_map.py",
                        ],
                        "success_test": {
                            "kind": "cmd",
                            "value": "python -m pytest tests/test_map.py -q",
                        },
                    },
                    {
                        "id": "frontend-setup",
                        "status": "pending",
                        "goal": "Initialize the React frontend later in execute.",
                        "files_to_create": [
                            "frontend/package.json",
                            "frontend/index.html",
                            "frontend/src/main.tsx",
                        ],
                        "success_test": {
                            "kind": "cmd",
                            "value": "cd frontend && npm run build",
                        },
                    },
                    {
                        "id": "workspace-verify",
                        "status": "pending",
                        "goal": "Add smoke verification after the app exists.",
                        "files_to_change": ["workspace.toml", "tests/test_smoke.py"],
                        "success_test": {
                            "kind": "cmd",
                            "value": "python -m pytest tests/test_smoke.py -q",
                        },
                    },
                ],
                "overlay": {
                    "retry_context": {
                        "last_task_result_excerpt": (
                            "request_watcher_review said create_initial_state "
                            "was missing/import-broken before the repair."
                        )
                    }
                },
            }
        ],
    }
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    rows = [
        {
            "ts": "2026-05-20T03:30:01.000000+00:00",
            "task_id": "phase_web_110d7ea6:execute",
            "tool": "shell",
            "result_preview": json.dumps(
                {
                    "command": [
                        "python",
                        "-m",
                        "pytest",
                        "tests/test_map.py",
                        "-q",
                    ],
                    "exit_code": 1,
                    "output": "AssertionError: assert 3 <= 2",
                }
            ),
        },
        {
            "ts": "2026-05-20T03:31:01.000000+00:00",
            "task_id": "phase_web_110d7ea6:execute",
            "tool": "request_watcher_review",
            "result_preview": json.dumps(
                {
                    "review_kind": "retry_watcher",
                    "status": "review_recorded",
                    "subtask_id": "map-engine",
                    "success_test": "python -m pytest tests/test_map.py -q",
                    "failed_attempts": 3,
                    "operator_reason": (
                        "The generated test is internally contradictory: it "
                        "uses abs(q)+abs(r)+abs(q+r)//2 although the correct "
                        "hex distance is (abs(q)+abs(r)+abs(q+r))//2."
                    ),
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.current_task_type = "phase_run"
    ctx.task_id = "phase_web_110d7ea6:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
        "current_subtask_id": "map-engine",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "map-engine",
                    "contract_migration_files": ["tests/test_map.py"],
                    "contract_migration_reason": (
                        "Generated test is internally contradictory: it "
                        "computes abs(q) + abs(r) + abs(q + r) // 2, which "
                        "maxes at 4 for radius 2 because // binds first. The "
                        "correct formula is (abs(q) + abs(r) + abs(q + r)) "
                        "// 2, which maxes at 2."
                    ),
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated"), result
    mutated = json.loads(plan_path.read_text(encoding="utf-8"))
    subtasks = mutated["nodes"][0]["subtasks"]
    assert [item["id"] for item in subtasks] == [
        "project-setup",
        "domain-models",
        "map-engine",
        "frontend-setup",
        "workspace-verify",
    ]
    map_subtask = next(item for item in subtasks if item["id"] == "map-engine")
    assert "maxes at 4" in map_subtask["contract_migration_reason"]
    assert map_subtask["contract_migration_files"] == ["tests/test_map.py"]
    signals = (state / "phase_control_signals.jsonl").read_text(encoding="utf-8")
    assert "mutate_phase_plan" in signals


def test_mutate_phase_plan_accepts_wrong_alias_warning_context_capture(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir()
    plan_path = state / "phase_plan.json"
    original = {
        "run_id": "phase_web_1f254e11",
        "workspace_id": "civilization",
        "version": 2,
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "docs-env",
                        "status": "pending",
                        "goal": (
                            "Document GMAS agent design and the real LLM "
                            "runtime env contract: OUROBOROS_LLM_API_KEY/"
                            "LLM_API_KEY, OUROBOROS_LLM_BASE_URL/"
                            "LLM_BASE_URL, LLM_MODEL, with "
                            "clear fail/skip/pause behavior when real LLM "
                            "credentials are absent."
                        ),
                        "files_to_create": [
                            "docs/architecture.md",
                            "docs/llm_runtime.md",
                            "tests/test_docs_readable.py",
                        ],
                        "success_test": {
                            "kind": "cmd",
                            "value": "python -m pytest tests/test_docs_readable.py -q",
                        },
                    }
                ],
            }
        ],
    }
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-20T03:51:58.519131+00:00",
                "task_id": "phase_web_1f254e11:execute",
                "tool": "shell",
                "result_preview": json.dumps(
                    {
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_docs_readable.py",
                            "-q",
                        ],
                        "exit_code": 1,
                        "output": (
                            "AssertionError: 'OUROBOROS_LLM_MODEL' is "
                            "contained here: **NOT** `OUROBOROS_LLM_MODEL`"
                        ),
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.current_task_type = "phase_run"
    ctx.task_id = "phase_web_1f254e11:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
        "current_subtask_id": "docs-env",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "docs-env",
                    "contract_migration_reason": (
                        "Generated test assertion 'OUROBOROS_LLM_MODEL' not "
                        "in content is too strict because it fails even when "
                        "the documentation correctly warns NOT to use that "
                        "alias. The docs mention the wrong alias only in "
                        "warning contexts."
                    ),
                    "contract_migration_files": ["tests/test_docs_readable.py"],
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated"), result
    mutated = json.loads(plan_path.read_text(encoding="utf-8"))
    subtask = mutated["nodes"][0]["subtasks"][0]
    assert "warning contexts" in subtask["contract_migration_reason"]
    assert subtask["contract_migration_files"] == ["tests/test_docs_readable.py"]
    signals = (state / "phase_control_signals.jsonl").read_text(encoding="utf-8")
    assert "mutate_phase_plan" in signals


def test_mutate_phase_plan_rejects_captured_llm_alias_deprecation_memory(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan_path = state / "phase_plan.json"
    original = {
        "plan_id": "phase_plan:foundation_docs",
        "run_id": "phase_web_40737336",
        "workspace_id": "civilization",
        "version": 8,
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "llm_agent_system",
                        "status": "pending",
                        "success_test": "python -m pytest tests/test_agents.py -v",
                    }
                ],
            }
        ],
    }
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_40737336:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "llm_agent_system",
                    "contract_migration_files": ["src/civ_game/agents.py"],
                    "contract_migration_reason": (
                        "Updated LLM runtime configuration to exclusively use "
                        "Umbrella/Ouroboros standard variables "
                        "(OUROBOROS_LLM_API_KEY, OUROBOROS_LLM_BASE_URL, "
                        "OUROBOROS_MODEL). Removed all references to legacy "
                        "LLM_* variables from error messages and comments. "
                        "The test_get_llm_runtime_config_fallback_to_legacy "
                        "test needs to be removed from test suite as it tests "
                        "unsupported legacy behavior."
                    ),
                }
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan")
    assert "LLM_API_KEY" in result
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original
    assert not (state / "phase_control_signals.jsonl").exists()


def test_mutate_phase_plan_accepts_supported_llm_alias_memory(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan_path = state / "phase_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "plan_id": "phase_plan:foundation_docs",
                "run_id": "phase_web_40737336",
                "workspace_id": "civilization",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "llm_agent_system",
                                "status": "pending",
                                "success_test": (
                                    "python -m pytest tests/test_agents.py -v"
                                ),
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_40737336:execute"
    ctx.loop_state_view = {
        "phase_label": "execute",
        "active_workspace_id": "civilization",
    }

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "llm_agent_system",
                    "contract_migration_files": ["tests/test_agents.py"],
                    "contract_migration_reason": (
                        "Keep runtime env priority as "
                        "LLM_API_KEY, "
                        "LLM_BASE_URL, and "
                        "LLM_MODEL; missing credentials raise "
                        "clear errors."
                    ),
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated")
    mutated = json.loads(plan_path.read_text(encoding="utf-8"))
    subtask = mutated["nodes"][0]["subtasks"][0]
    assert "LLM_API_KEY" in subtask["contract_migration_reason"]


def test_mutate_phase_plan_rejects_missing_subtask_without_memory_churn(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan_path = state / "phase_plan.json"
    original = {
        "run_id": "phase_web_c7d5731f",
        "workspace_id": "civilization",
        "version": 1,
        "nodes": [
            {
                "id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "0_llm_config_setup",
                        "status": "pending",
                        "success_test": {
                            "kind": "cmd",
                            "value": "pytest tests/test_llm_config.py -v",
                        },
                    }
                ],
            }
        ],
    }
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_c7d5731f:execute"
    ctx.loop_state_view = {"phase_label": "execute"}

    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": "missing_subtask",
                    "success_test": "pytest tests/test_missing.py -v",
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "subtask 'missing_subtask' not found" in result
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original


def test_submit_research_summary_rejects_random_architecture_id(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {"saved": True, "id": "memory-1", "legacy": {"id": "finding-1"}}
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="swift/flow/hybrid-cloud",
        findings_ids=["finding-1"],
        notes="Concrete readable research notes with enough context for planning.",
    )

    assert result.startswith("ERROR:")
    assert "architecture_id" in result
    assert "arch-civilization-gmas-web-v1" in result
    assert "findings_ids" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_tells_model_not_to_use_palace_id_as_architecture_id(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {
                        "saved": True,
                        "id": "2148bcc8-7ea9-4d2b-a2c3-289ac79d1f71",
                        "legacy": {"id": "drawer_39e1240b75eef446ad6f689a"},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="2148bcc8-7ea9-4d2b-a2c3-289ac79d1f71",
        findings_ids=["drawer_39e1240b75eef446ad6f689a"],
        notes="Concrete readable research notes with enough context for planning.",
    )

    assert result.startswith("ERROR:")
    assert "Do not pass palace_add/memory ids" in result
    assert "architecture_id" in result
    assert "findings_ids" in result


def test_submit_research_summary_rejects_underscore_mock_architecture_id(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_0714d92b:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {"saved": True, "id": "finding-1", "legacy": {"id": "drawer-1"}}
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_0714d92b:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="architecture_civ_game_mock_servers",
        findings_ids=["finding-1"],
        notes=(
            "Concrete readable research notes with enough context for planning "
            "and GMAS/WebSocket implementation."
        ),
    )

    assert result.startswith("ERROR:")
    assert "architecture_id" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_rejects_mock_token_in_architecture_id(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {"saved": True, "id": "finding-1", "legacy": {"id": "drawer-1"}}
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="architecture-civ-game-mock-servers",
        findings_ids=["finding-1"],
        notes=(
            "Concrete readable research notes with enough context for planning "
            "and GMAS/WebSocket implementation."
        ),
    )

    assert result.startswith("ERROR:")
    assert "mock, fake, stub" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_rejects_mojibake_notes(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {"saved": True, "id": "memory-1", "legacy": {"id": "finding-1"}}
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civ-llm",
        findings_ids=["finding-1"],
        notes=(
            "\u00e7\u00a0\u201d\u00e7\u00a9\u00b6\u00e5\u00ae\u0152"
            "\u00e6\u02c6\u0090\u00e3\u20ac\u201a FastAPI evidence."
        ),
    )

    assert result.startswith("ERROR:")
    assert "mojibake" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_requires_manifest_finding_floor(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {"saved": True, "legacy": {"id": "finding-1"}}
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "exit_criteria": {
                    "min_palace_writes": [{"store": "palace.run", "n": 3}]
                }
            }
        },
    )
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete research notes with one accepted finding only.",
    )

    assert result.startswith("ERROR:")
    assert "at least 3 accepted palace_add" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_submit_research_summary_accepts_source_scarce_after_exhausted_discovery(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "web_search",
            "args": {
                "query": "civilization strategy game python websockets architecture"
            },
            "result_preview": json.dumps(
                {
                    "status": "provider_error",
                    "provider": "gmas_web_search",
                    "query": "civilization strategy game python websockets architecture",
                    "error": "TimeoutError",
                }
            ),
        },
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "deep_search",
            "args": {"query": "civilization strategy game websockets"},
            "result_preview": json.dumps(
                {
                    "status": "no_results",
                    "query": "civilization strategy game websockets",
                }
            ),
        },
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "github_project_search",
            "args": {"query": "python websockets game server real-time multiplayer"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "query": "python websockets game server real-time multiplayer",
                    "results": [{"full_name": "nav2991/connect4-multiplayer"}],
                }
            ),
        },
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "mcp_discover",
            "args": {"query": "websockets real-time communication game server"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "query": "websockets real-time communication game server",
                    "results": [],
                }
            ),
        },
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "search_gmas_knowledge",
            "args": {"query": "GraphBuilder MACPRunner game agents"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "query": "GraphBuilder MACPRunner game agents",
                    "confidence": 0.69,
                    "metadata": {"fallback": True},
                }
            ),
        },
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-websocket",
                    "kind": "research_finding",
                    "source_path": (
                        "github_project_search:"
                        "python websockets game server real-time multiplayer"
                    ),
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": [
                    "github_project_search",
                    "deep_search",
                    "web_search",
                    "mcp_discover",
                    "submit_research_summary",
                ],
                "exit_criteria": {
                    "min_palace_writes": [{"store": "palace.run", "n": 3}]
                },
            }
        },
    )
    ctx.task_id = "phase_web_af37a8b6:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-websocket"],
        notes=(
            "Concrete low-evidence handoff. One accepted WebSocket server "
            "finding is available. Other discovery attempts were empty, timed "
            "out, or fallback-only. Generated project LLM runtime must use "
            "LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL with no mock decisions."
        ),
        coverage_status="source_scarce",
        source_scarcity_reason="Only one usable source row remained after discovery.",
    )

    assert result.startswith("OK:")
    artifact = json.loads(
        (drive / "state" / "research_summary_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert artifact["coverage_status"] == "source_scarce"
    assert artifact["coverage_report"]["usable_source_count"] == 1
    assert artifact["coverage_report"]["accepted_finding_count"] == 1


def test_submit_research_summary_source_scarce_requires_deep_after_web_error(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_scarce:research",
            "tool": "web_search",
            "args": {"query": "civilization strategy game python websockets"},
            "result_preview": json.dumps(
                {
                    "status": "provider_error",
                    "provider": "gmas_web_search",
                    "query": "civilization strategy game python websockets",
                    "error": "TimeoutError",
                }
            ),
        },
        {
            "task_id": "phase_web_scarce:research",
            "tool": "github_project_search",
            "args": {"query": "python websockets game server"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "query": "python websockets game server",
                    "results": [{"full_name": "owner/server"}],
                }
            ),
        },
        {
            "task_id": "phase_web_scarce:research",
            "tool": "mcp_discover",
            "args": {"query": "websocket game server"},
            "result_preview": json.dumps(
                {"status": "ok", "query": "websocket game server", "results": []}
            ),
        },
        {
            "task_id": "phase_web_scarce:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-1",
                    "kind": "research_finding",
                    "source_path": "github_project_search:python websockets game server",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "exit_criteria": {
                    "min_palace_writes": [{"store": "palace.run", "n": 3}]
                }
            }
        },
    )
    ctx.task_id = "phase_web_scarce:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-source-scarce",
        findings_ids=["finding-1"],
        notes="One accepted source, web_search timed out, source scarce.",
        coverage_status="source_scarce",
        source_scarcity_reason="Only one usable source row remained after discovery.",
    )

    assert result.startswith("ERROR:")
    assert 'deep_search(intent="planner_research")' in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_source_scarce_rejects_unharvested_usable_source(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "github_project_search",
            "args": {"query": "python websockets game server"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "query": "python websockets game server",
                    "results": [{"full_name": "owner/server"}],
                }
            ),
        },
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "web_search",
            "args": {"query": "websocket game architecture"},
            "result_preview": json.dumps(
                {
                    "provider": "gmas_web_search",
                    "query": "websocket game architecture",
                    "sources": [{"url": "https://example.test/ws"}],
                }
            ),
        },
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "mcp_discover",
            "args": {"query": "game server websocket"},
            "result_preview": json.dumps(
                {"status": "ok", "query": "game server websocket", "results": []}
            ),
        },
        {
            "task_id": "phase_web_af37a8b6:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-github",
                    "kind": "research_finding",
                    "source_path": "github_project_search:python websockets game server",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": [
                    "github_project_search",
                    "web_search",
                    "mcp_discover",
                    "submit_research_summary",
                ],
                "exit_criteria": {
                    "min_palace_writes": [{"store": "palace.run", "n": 3}]
                },
            }
        },
    )
    ctx.task_id = "phase_web_af37a8b6:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-github"],
        notes=(
            "One accepted finding exists, but another web source is still "
            "usable and should be saved before claiming scarcity. LLM_API_KEY, "
            "LLM_BASE_URL, and LLM_MODEL are the public runtime aliases."
        ),
        coverage_status="source_scarce",
    )

    assert result.startswith("ERROR:")
    assert "unharvested usable source evidence" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_shortfall_suggests_recent_discovery_source(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {"saved": True, "id": "finding-1", "kind": "research_finding"}
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {"saved": True, "id": "finding-2", "kind": "research_finding"}
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "search_gmas_knowledge",
            "args": {"query": "GMAS architecture patterns for game simulation"},
            "result_preview": json.dumps(
                {
                    "query": "GMAS architecture patterns for game simulation",
                    "recommended_pattern": "Use ToolRegistry for agent tools",
                    "key_symbols": ["ToolRegistry"],
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "exit_criteria": {
                    "min_palace_writes": [{"store": "palace.run", "n": 3}]
                }
            }
        },
    )
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1", "finding-2"],
        notes="Concrete research notes with two accepted findings only.",
    )

    assert result.startswith("ERROR:")
    assert (
        "Recent usable discovery source candidate: "
        "`search_gmas_knowledge:GMAS architecture patterns for game simulation`"
    ) in result
    assert "Before retrying `submit_research_summary`, call `palace_add`" in result


def test_submit_research_summary_rejects_positive_empty_discovery_source_claims(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_b63586a5:research",
            "tool": "github_project_search",
            "args": {"query": "LLM civilization strategy game AI bots python"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "query": "LLM civilization strategy game AI bots python",
                    "results": [],
                }
            ),
        },
        {
            "task_id": "phase_web_b63586a5:research",
            "tool": "mcp_discover",
            "args": {"query": "game development simulation"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "query": "game development simulation",
                    "results": [],
                }
            ),
        },
        {
            "task_id": "phase_web_b63586a5:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-gmas-1",
                    "kind": "research_finding",
                    "source_path": "gmas:multi-agent game AI",
                }
            ),
        },
        {
            "task_id": "phase_web_b63586a5:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-gmas-2",
                    "kind": "research_finding",
                    "source_path": "search_gmas_knowledge:game tools",
                }
            ),
        },
        {
            "task_id": "phase_web_b63586a5:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-gmas-3",
                    "kind": "research_finding",
                    "source_path": "get_gmas_context:agents",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_b63586a5:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-gmas-1", "finding-gmas-2", "finding-gmas-3"],
        notes=(
            "Architecture grounded in accepted GMAS findings. "
            "MCP discovery returned docker and simulation-related servers. "
            "GitHub results for strategy games inform turn-based state modeling."
        ),
    )

    assert result.startswith("ERROR:")
    assert "positive GitHub discovery evidence" in result
    assert "none of the cited accepted research findings" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_allows_positive_github_claim_with_matching_source(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-github",
                    "kind": "research_finding",
                    "source_path": "github:example/strategy-game",
                }
            ),
        }
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-github"],
        notes=(
            "GitHub results for strategy games inform turn-based state modeling. "
            "The cited finding carries matching GitHub source provenance."
        ),
    )

    assert result.startswith("OK: Research summary submitted")


def test_submit_research_summary_rejects_captured_unbacked_source_labels(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_779c6ad4:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-fastapi",
                    "kind": "research_finding",
                    "source_path": "deep_search:web game python fastapi typescript react tutorial",
                }
            ),
        },
        {
            "task_id": "phase_web_779c6ad4:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-webgame",
                    "kind": "research_finding",
                    "source_path": "deep_search:github isadri transcendence civilization strategy game",
                }
            ),
        },
        {
            "task_id": "phase_web_779c6ad4:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-llm",
                    "kind": "research_finding",
                    "source_path": "deep_search:LLM-powered game bot economy diplomacy framework",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_779c6ad4:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-fastapi-gmas-react-v1",
        findings_ids=["finding-fastapi", "finding-webgame", "finding-llm"],
        notes=(
            "Architecture uses public LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL. "
            "\n**Source**: deep_search:fastapi react full stack tutorial"
            "\n**Source**: deep_search:github isadri transcendence"
            "\n**Source**: deep_search:GMAS early_stop_example.py"
        ),
    )

    assert result.startswith("ERROR:")
    assert "source label(s) not backed by the cited accepted findings" in result
    assert "deep_search:fastapi react full stack tutorial" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_accepts_exact_backed_source_labels(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    source = "deep_search:web game python fastapi typescript react tutorial"
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-fastapi",
                    "kind": "research_finding",
                    "source_path": source,
                }
            ),
        }
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-fastapi-gmas-react-v1",
        findings_ids=["finding-fastapi"],
        notes=(
            "Architecture uses public LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL. "
            f"\n**Source**: {source}"
        ),
    )

    assert result.startswith("OK: Research summary submitted")


def test_submit_research_summary_rejects_captured_bare_github_discovery_claim(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_96995622:research",
            "tool": "github_project_search",
            "args": {"query": "python game web server websocket multiplayer"},
            "result_preview": json.dumps(
                {
                    "status": "ok",
                    "query": "python game web server websocket multiplayer",
                    "results": [
                        {
                            "full_name": "kochj23/Web-Pennmush",
                            "html_url": "https://github.com/kochj23/Web-Pennmush",
                        }
                    ],
                }
            ),
        },
        {
            "task_id": "phase_web_96995622:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-gmas",
                    "kind": "research_finding",
                    "source_path": "search_gmas_knowledge:agent tools",
                }
            ),
        },
        {
            "task_id": "phase_web_96995622:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-web",
                    "kind": "research_finding",
                    "source_path": "deep_search:web game python typescript frontend architecture",
                }
            ),
        },
        {
            "task_id": "phase_web_96995622:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-github",
                    "kind": "research_finding",
                    "source_path": "github_project_search",
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_96995622:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-py-ts-v1",
        findings_ids=["finding-gmas", "finding-web", "finding-github"],
        notes=(
            "Architecture is grounded in GMAS and web research. "
            "External Discovery: GitHub discovery executed - see finding "
            "finding-github for Python web game architecture patterns."
        ),
    )

    assert result.startswith("ERROR:")
    assert "positive GitHub discovery evidence" in result
    assert "usable current GitHub source" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_requires_manifest_discovery_coverage(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {"saved": True, "legacy": {"id": "finding-1"}}
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": ["github_project_search", "mcp_discover"]
            }
        },
    )
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    from tests.helpers.capability_declaration import seed_submitted_declaration

    seed_submitted_declaration(ctx, discovery_channels=[])

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete research notes with a palace finding.",
    )

    assert result.startswith("ERROR:")
    assert "discovery coverage" in result or "discovery_channels" in result


def test_submit_research_summary_accepts_manifest_discovery_coverage(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "github_project_search",
            "args": {"query": "civilization llm game"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "mcp_discover",
            "args": {"query": "strategy game state"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": ["github_project_search", "mcp_discover"]
            }
        },
    )
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete research notes with discovery coverage.",
    )

    assert result.startswith("OK: Research summary submitted")


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_submit_research_summary_rejects_mcp_tool_arg_error_as_coverage(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_740d5c97:research",
            "tool": "github_project_search",
            "args": {"query": "turn based strategy game python backend frontend"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_740d5c97:research",
            "tool": "mcp_discover",
            "args": {
                "query": "LLM openai anthropic",
                "max_results": 3,
                "file_path": (
                    "workspaces/civilization/.memory/drive/memory/knowledge/"
                    "inspiration/neochar/hexity/index.md"
                ),
            },
            "result_preview": (
                "⚠️ TOOL_ARG_ERROR (mcp_discover): _mcp_discover() got an "
                "unexpected keyword argument 'file_path'"
            ),
        },
        {
            "task_id": "phase_web_740d5c97:research",
            "tool": "palace_add",
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": ["github_project_search", "mcp_discover"]
            }
        },
    )
    ctx.task_id = "phase_web_740d5c97:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civ-gmas-backend",
        findings_ids=["finding-1"],
        notes="Concrete research notes with a failed MCP discovery attempt.",
    )

    assert result.startswith("ERROR:")
    assert "mcp_discover" in result
    assert "TOOL_ARG_ERROR" in result


def test_submit_research_summary_rejects_llm_fallback_actions_handoff(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "github_project_search",
            "args": {"query": "civilization llm game"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "mcp_discover",
            "args": {"query": "strategy game state"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": ["github_project_search", "mcp_discover"]
            }
        },
    )
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civ-game",
        findings_ids=["finding-1"],
        notes=(
            "LLM bots use GMAS agents with a 30 second timeout and fallback "
            "actions when provider calls fail."
        ),
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result


def test_submit_research_summary_rejects_captured_cost_fallback_finding(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_8b680883:research",
            "tool": "github_project_search",
            "args": {"query": "civilization llm game"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_8b680883:research",
            "tool": "mcp_discover",
            "args": {"query": "strategy game state"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_8b680883:research",
            "tool": "palace_add",
            "args": {
                "title": "LLM-Driven Diplomacy & Economy Implementation",
                "content": (
                    "Core Innovation: Free-form LLM decisions instead of "
                    "discrete buttons. No fixed always-build-barracks behavior. "
                    "Game Balance Considerations: Bot difficulty tuning via LLM "
                    "prompt constraints. Cost management: Rate-limit LLM calls, "
                    "cache similar decisions. Fallback to heuristics if LLM costs "
                    "become prohibitive."
                ),
                "tags": "llm,diplomacy,economy,ai,agent-persona",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "d88fccb4-870e-4671-af82-58489fb16a58",
                    "legacy": {"id": "drawer_db45c57178e33c60856060b1"},
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": ["github_project_search", "mcp_discover"]
            }
        },
    )
    ctx.task_id = "phase_web_8b680883:research"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["d88fccb4-870e-4671-af82-58489fb16a58"],
        notes="Research complete with LLM-driven game systems.",
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result


def test_submit_research_summary_rejects_captured_decision_caching_notes(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_14d924fc:research",
            "tool": "palace_add",
            "args": {
                "title": "LLM Diplomacy Architecture",
                "content": (
                    "GMAS bots use fresh inherited runtime env LLM calls for "
                    "diplomatic and economic decisions."
                ),
                "tags": "research_finding,llm,gmas",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "bbcd506f-8498-4fad-a85c-b5107612b77a",
                    "legacy": {"id": "drawer_81146fc82e64f0c21ecbd9d8"},
                }
            ),
        },
        {
            "task_id": "phase_web_14d924fc:research",
            "tool": "palace_add",
            "args": {
                "title": "Full-Stack Web Game Architecture",
                "content": "FastAPI, WebSocket, React, and TypeScript architecture.",
                "tags": "research_finding,fastapi,react,typescript",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "947796fe-0e7f-4895-bca1-e1cb86a86b6f",
                    "legacy": {"id": "drawer_5e7d2ac23b7202b825023448"},
                }
            ),
        },
        {
            "task_id": "phase_web_14d924fc:research",
            "tool": "palace_add",
            "args": {
                "title": "GMAS Framework",
                "content": "Use MACPRunner, ToolRegistry, and structured schemas.",
                "tags": "research_finding,gmas,multi-agent",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "fa41be1a-8eea-4559-ad3a-8202938c4506",
                    "legacy": {"id": "drawer_ec7b81dcd310b39eac275690"},
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_14d924fc:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=[
            "bbcd506f-8498-4fad-a85c-b5107612b77a",
            "947796fe-0e7f-4895-bca1-e1cb86a86b6f",
            "fa41be1a-8eea-4559-ad3a-8202938c4506",
        ],
        notes=(
            "LLM/GMAS bot runtime uses LLM_API_KEY, "
            "LLM_BASE_URL, and LLM_MODEL. "
            "Missing credentials pause bot turns or surface explicit errors. "
            "Performance: LLM latency per bot per turn. Mitigate via parallel "
            "processing, early stopping for no-action bots, and caching stable, "
            "unchanging decisions."
        ),
    )

    assert result.startswith("ERROR:")
    assert "cached decision/action/response reuse" in result
    assert not (drive / "state" / "research_summary_latest.json").exists()


def test_submit_research_summary_rejects_duplicate_id_and_legacy_aliases(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "github_project_search",
            "args": {"query": "civilization game python llm ai bot strategy"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "web_search",
            "args": {"query": "LLM powered game AI civilization strategy bot"},
            "result_preview": json.dumps(
                {"status": "provider_error", "provider": "gmas_web_search", "error": "TimeoutError"}
            ),
        },
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "mcp_discover",
            "args": {"query": "LLM game AI simulation development"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "palace_add",
            "args": {
                "title": "Architecture: LLM-Driven Civilization Game",
                "content": "Use GMAS agents and Umbrella runtime aliases.",
                "tags": "research_finding,architecture",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "a3da5bed-f1f0-4047-a7ec-0ed85cb96958",
                    "legacy": {"id": "drawer_08f66dd89645dbb96080200e"},
                }
            ),
        },
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "palace_add",
            "args": {
                "title": "GMAS Patterns for Civilization AI Agents",
                "content": "Use ToolRegistry, AgentMemory, and native tool calls.",
                "tags": "research_finding,gmas,implementation",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "bb753128-29af-4b3f-a9cb-0377fca56276",
                    "legacy": {"id": "drawer_160b86cf7fbef06667476f9d"},
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": [
                    "github_project_search",
                    "web_search",
                    "mcp_discover",
                ]
            }
        },
    )
    ctx.task_id = "phase_web_0dd335be:research"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civ-game-gmas-fastapi-v1",
        findings_ids=[
            "a3da5bed-f1f0-4047-a7ec-0ed85cb96958",
            "bb753128-29af-4b3f-a9cb-0377fca56276",
            "drawer_08f66dd89645dbb96080200e",
            "drawer_160b86cf7fbef06667476f9d",
        ],
        notes=(
            "Research complete for LLM-driven Civilization game using GMAS, "
            "FastAPI, React, and Umbrella runtime aliases."
        ),
    )

    assert result.startswith("ERROR:")
    assert "same palace_add finding" in result
    assert "legacy drawer aliases" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_submit_research_summary_normalises_legacy_alias_to_primary_id(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "github_project_search",
            "args": {"query": "civilization game python llm ai bot strategy"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "web_search",
            "args": {"query": "LLM powered game AI civilization strategy bot"},
            "result_preview": json.dumps(
                {"status": "provider_error", "provider": "gmas_web_search", "error": "TimeoutError"}
            ),
        },
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "mcp_discover",
            "args": {"query": "LLM game AI simulation development"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "palace_add",
            "args": {
                "title": "Architecture finding",
                "content": (
                    "Use GMAS agents and resolve "
                    "LLM_API_KEY, "
                    "LLM_BASE_URL, and "
                    "LLM_MODEL from the inherited runtime."
                ),
                "tags": "research_finding,architecture",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-primary",
                    "legacy": {"id": "drawer_primary"},
                }
            ),
        },
        {
            "task_id": "phase_web_0dd335be:research",
            "tool": "palace_add",
            "args": {
                "title": "Implementation finding",
                "content": "Use ToolRegistry, AgentMemory, and native tool calls.",
                "tags": "research_finding,gmas,implementation",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-secondary",
                    "legacy": {"id": "drawer_secondary"},
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": [
                    "github_project_search",
                    "web_search",
                    "mcp_discover",
                ]
            }
        },
    )
    ctx.task_id = "phase_web_0dd335be:research"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civ-game-gmas-fastapi-v1",
        findings_ids=["drawer_primary", "finding-secondary"],
        notes=(
            "Research complete for LLM-driven Civilization game using GMAS, "
            "FastAPI, React, and Umbrella runtime aliases."
        ),
    )

    assert result.startswith("OK: Research summary submitted")
    assert "findings: 2" in result
    latest = json.loads(
        (drive / "state" / "research_summary_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["findings_ids"] == ["finding-primary", "finding-secondary"]


def test_submit_research_summary_allows_protective_no_fallback_finding(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_8b680883:research",
            "tool": "github_project_search",
            "args": {"query": "civilization llm game"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_8b680883:research",
            "tool": "mcp_discover",
            "args": {"query": "strategy game state"},
            "result_preview": "{}",
        },
        {
            "task_id": "phase_web_8b680883:research",
            "tool": "palace_add",
                "args": {
                    "title": "LLM error handling",
                    "content": (
                        "LLM failures pause bot turns and surface retry/pause errors. "
                        "No automatic fallback to heuristics or cached decisions is allowed. "
                        "Generated workspace code resolves LLM_API_KEY, "
                        "LLM_BASE_URL, and LLM_MODEL from the inherited runtime."
                    ),
                    "tags": "llm,error-handling",
                },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-safe",
                    "legacy": {"id": "drawer_safe"},
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": ["github_project_search", "mcp_discover"]
            }
        },
    )
    ctx.task_id = "phase_web_8b680883:research"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=["finding-safe"],
        notes=(
            "Research complete with explicit LLM retry and pause behavior. "
            "Runtime env contract: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL."
        ),
    )

    assert result.startswith("OK: Research summary submitted")


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_submit_research_summary_requires_github_mcp_and_internet_when_available(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "github_project_search",
            "args": {"query": "civilization llm game"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "mcp_discover",
            "args": {"query": "strategy game state"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive,
        context_overlays={
            "phase_manifest": {
                "allowed_tools": [
                    "github_project_search",
                    "web_search",
                    "deep_search",
                    "mcp_discover",
                ]
            }
        },
    )
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete research notes with only github and mcp coverage.",
    )

    assert result.startswith("ERROR:")
    assert "one internet search tool" in result

    with (logs / "tools.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "task_id": "run-1:research",
                    "tool": "web_search",
                    "args": {"query": "turn based strategy game architecture"},
                    "result_preview": "{}",
                }
            )
            + "\n"
        )

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete research notes with github, internet, and mcp coverage.",
    )

    assert result.startswith("OK: Research summary submitted")


def test_submit_research_summary_rejects_placeholder_notes(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:research",
                "tool": "palace_add",
                "result_preview": json.dumps(
                    {"saved": True, "legacy": {"id": "finding-1"}}
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Research phase pending completion - preparing palace writes",
    )

    assert result.startswith("ERROR:")
    assert "placeholder" in result


def test_submit_research_summary_rejects_captured_incomplete_progress_notes(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_225ca559:research",
            "tool": "palace_add",
            "result_preview": json.dumps(
                {"saved": True, "id": "2d0ab652-9f5f-4f44-b25a-9c3dbd820a7b"}
            ),
        },
        {
            "task_id": "phase_web_225ca559:research",
            "tool": "palace_add",
            "result_preview": json.dumps(
                {"saved": True, "id": "5ae86430-c66c-4e09-b879-7c18b0db201e"}
            ),
        },
    ]
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_225ca559:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-fastapi-llm-v1",
        findings_ids=[
            "2d0ab652-9f5f-4f44-b25a-9c3dbd820a7b",
            "5ae86430-c66c-4e09-b879-7c18b0db201e",
        ],
        notes=(
            "Research in progress. Currently 2 findings persisted; need "
            "minimum 3 findings before completion. Continuing to gather "
            "evidence on implementation approaches."
        ),
    )

    assert result.startswith("ERROR:")
    assert "placeholder" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_submit_research_summary_rejects_captured_interrupted_coverage_notes(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    finding_ids = [
        "875f3fe9-235d-40ff-869d-b0b8b0b7a06d",
        "4efd3728-2cc7-4b12-89fa-dae728f493bd",
        "683c31bb-3f9e-486a-9889-204c7e1f8358",
    ]
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as fh:
        for finding_id in finding_ids:
            fh.write(
                json.dumps(
                    {
                        "task_id": "phase_web_368db408:research",
                        "tool": "palace_add",
                        "result_preview": json.dumps(
                            {"saved": True, "id": finding_id}
                        ),
                    }
                )
                + "\n"
            )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_368db408:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=finding_ids,
        notes=(
            "PHASE INTERRUPTED - INCOMPLETE COVERAGE. Research gathered core "
            "architecture findings but did not complete all discovery "
            "requirements. Missing: TypeScript/JSX frontend ecosystem research, "
            "testing strategy for LLM-long game workflows, palace search for "
            "similar projects, and skill loading."
        ),
    )

    assert result.startswith("ERROR:")
    assert "placeholder" in result


def test_submit_research_summary_does_not_count_scratchpad_as_finding(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_368db408:research",
            "tool": "palace_add",
            "args": {
                "kind": "research_finding",
                "content": "Finding 1: FastAPI WebSocket architecture is viable.",
                "tags": "research_finding,architecture",
            },
            "result_preview": json.dumps(
                {"saved": True, "id": "875f3fe9-235d-40ff-869d-b0b8b0b7a06d"}
            ),
        },
        {
            "task_id": "phase_web_368db408:research",
            "tool": "palace_add",
            "args": {
                "kind": "scratchpad",
                "content": "Research progress: 1/3 palace findings saved.",
            },
            "result_preview": json.dumps(
                {"saved": True, "id": "683c31bb-3f9e-486a-9889-204c7e1f8358"}
            ),
        },
        {
            "task_id": "phase_web_368db408:research",
            "tool": "palace_add",
            "args": {
                "kind": "research_finding",
                "content": "Finding 2: GMAS multi-agent runner fits LLM bot turns.",
                "tags": "research_finding,gmas",
            },
            "result_preview": json.dumps(
                {"saved": True, "id": "4efd3728-2cc7-4b12-89fa-dae728f493bd"}
            ),
        },
    ]
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_368db408:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-gmas-web-v1",
        findings_ids=[
            "875f3fe9-235d-40ff-869d-b0b8b0b7a06d",
            "683c31bb-3f9e-486a-9889-204c7e1f8358",
            "4efd3728-2cc7-4b12-89fa-dae728f493bd",
        ],
        notes=(
            "Concrete complete research notes with FastAPI, GMAS, frontend, "
            "and LLM runtime alias coverage."
        ),
    )

    assert result.startswith("ERROR:")
    assert "not accepted by palace_add" in result
    assert "683c31bb-3f9e-486a-9889-204c7e1f8358" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_submit_research_summary_counts_incidental_placeholder_words_in_findings(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "phase_web_92a8e0d4:research",
            "tool": "palace_add",
            "args": {
                "kind": "research_finding",
                "content": (
                    "Finding: current workspace is empty. workspace.toml contains "
                    "only placeholder meta configuration, so this is a greenfield "
                    "project requiring full Python backend and TypeScript frontend setup."
                ),
                "tags": "workspace-analysis,greenfield,research_finding",
            },
            "result_preview": json.dumps(
                {"saved": True, "id": "7ef8732d-1771-4ea3-bad5-e079ae992a25"}
            ),
        },
        {
            "task_id": "phase_web_92a8e0d4:research",
            "tool": "palace_add",
            "args": {
                "kind": "research_finding",
                "content": (
                    "Finding: FastAPI plus React/Vite is a viable web architecture. "
                    "API shape TBD during planning, but WebSocket state sync and "
                    "server authoritative turns are the recommended direction."
                ),
                "tags": "web-architecture,fastapi,react,research_finding",
            },
            "result_preview": json.dumps(
                {"saved": True, "id": "0ee33970-474e-4816-8640-50f1af2d6e35"}
            ),
        },
        {
            "task_id": "phase_web_92a8e0d4:research",
            "tool": "palace_add",
            "args": {
                "kind": "research_finding",
                "content": (
                    "Finding: GMAS AgentProfile and MACPRunner fit real LLM bot "
                    "turns with tool-mediated game actions."
                ),
                "tags": "gmas,llm,research_finding",
            },
            "result_preview": json.dumps(
                {"saved": True, "id": "bdc24769-cd42-4610-8032-d5219d8b187d"}
            ),
        },
    ]
    with (logs / "tools.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_92a8e0d4:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-civilization-game-v1",
        findings_ids=[
            "7ef8732d-1771-4ea3-bad5-e079ae992a25",
            "0ee33970-474e-4816-8640-50f1af2d6e35",
            "bdc24769-cd42-4610-8032-d5219d8b187d",
        ],
        notes=(
            "Research complete: FastAPI/React architecture, GMAS real LLM bots, "
            "and greenfield setup requirements. LLM runtime must use "
            "LLM_API_KEY, "
            "LLM_BASE_URL, and LLM_MODEL."
        ),
    )

    assert result.startswith("OK: Research summary submitted")


def test_submit_research_summary_rejects_unread_file_claims(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "main.py").write_text("def create_game():\n    pass\n", encoding="utf-8")
    (workspace / "game_engine.py").write_text("class GameEngine:\n    pass\n", encoding="utf-8")
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "game_engine.py"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Blocker",
                "content": "main.py calls GameEngine(game); game_engine.py has the class.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete notes cite main.py as a current blocker.",
    )

    assert result.startswith("ERROR:")
    assert "not read in this research phase" in result
    assert "main.py" in result


def test_submit_research_summary_allows_file_claims_after_read_file(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "main.py").write_text("def create_game():\n    pass\n", encoding="utf-8")
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "main.py"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Entrypoint",
                "content": "main.py wires the FastAPI endpoint.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete notes cite main.py after reading it.",
    )

    assert result.startswith("OK: Research summary submitted")


def test_submit_research_summary_ignores_non_workspace_tool_paths(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "GMAS references",
                "content": (
                    "search_gmas_knowledge returned gmas/builder.py and "
                    "gmas/execution/runner/core.py as external framework leads."
                ),
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete notes cite gmas/builder.py as a tool-search lead.",
    )

    assert result.startswith("OK: Research summary submitted")


def test_referenced_workspace_paths_preserves_tsx_extension():
    assert _referenced_workspace_paths("frontend/src/App.tsx", "mini_game") == {
        "frontend/src/App.tsx"
    }


def test_submit_research_summary_matches_read_paths_case_insensitively(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "main.py").write_text("def create_game():\n    pass\n", encoding="utf-8")
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "main.py"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Entrypoint",
                "content": "Main.py wires the FastAPI endpoint.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete notes cite Main.py after reading main.py.",
    )

    assert result.startswith("OK: Research summary submitted")


def test_submit_research_summary_allows_basename_after_full_path_read(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "backend" / "bots").mkdir(parents=True)
    (workspace / "backend" / "bots" / "bot_tools.py").write_text(
        "def get_game_state_tool():\n    pass\n",
        encoding="utf-8",
    )
    (workspace / "game_core").mkdir(parents=True)
    (workspace / "game_core" / "bot_tools.py").write_text(
        "def core_tool():\n    pass\n",
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Bot tools",
                "content": "bot_tools.py exposes GMAS action tools for bots.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete notes cite bot_tools.py after reading its full path.",
    )

    assert result.startswith("OK: Research summary submitted")


def test_submit_research_summary_rejects_ambiguous_basename_reads(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "backend" / "bots").mkdir(parents=True)
    (workspace / "game_core").mkdir(parents=True)
    (workspace / "backend" / "bots" / "bot_tools.py").write_text(
        "def backend_tool():\n    pass\n",
        encoding="utf-8",
    )
    (workspace / "game_core" / "bot_tools.py").write_text(
        "def core_tool():\n    pass\n",
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "game_core/bot_tools.py"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Bot tools",
                "content": "bot_tools.py exposes GMAS action tools for bots.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete notes cite bot_tools.py ambiguously.",
    )

    assert result.startswith("ERROR:")
    assert "bot_tools.py" in result


def test_submit_research_summary_rejects_stale_missing_symbol_claim(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": json.dumps(
                {"content": "def get_game_state_tool(game_state, player_id):\n    pass\n"}
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": "backend/bots/bot_tools.py missing get_game_state_tool export.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Verification blockers: bot_tools.py missing get_game_state_tool export.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result
    assert "get_game_state_tool" in result


def test_submit_research_summary_rejects_missing_symbol_in_file_wording(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": json.dumps(
                {"content": "def get_game_state_tool(game_state, player_id):\n    pass\n"}
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": "Missing get_game_state_tool in bot_tools.py.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Missing get_game_state_tool in bot_tools.py.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_submit_research_summary_rejects_stale_missing_optional_param_claim(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "game_engine.py"},
            "result_preview": json.dumps(
                {
                    "content": (
                        "class GameEngine:\n"
                        "    def __init__(self, game, ai_controller=None):\n"
                        "        self.ai_controller = ai_controller\n"
                    )
                }
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": "GameEngine.__init__() missing ai_controller argument.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="GameEngine.__init__() missing ai_controller argument.",
    )

    assert result.startswith("ERROR:")
    assert "already shows that parameter" in result


def test_submit_research_summary_rejects_missing_optional_param_with_article(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "game_engine.py"},
            "result_preview": json.dumps(
                {
                    "content": (
                        "class GameEngine:\n"
                        "    def __init__(self, game, ai_controller=None):\n"
                        "        self.ai_controller = ai_controller\n"
                    )
                }
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": (
                    "Verification indicates a GameEngine.__init__() "
                    "initialization issue missing the ai_controller argument."
                ),
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes=(
            "Verification indicates a GameEngine.__init__() initialization "
            "issue missing the ai_controller argument."
        ),
    )

    assert result.startswith("ERROR:")
    assert "already shows that parameter" in result


def test_submit_research_summary_rejects_fix_calls_to_include_existing_param(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
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
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "main.py"},
            "result_preview": json.dumps(
                {"content": "return GameEngine(game, ai_controller)\n"}
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Architecture",
                "content": "Current flow is GameState to GameEngine.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Next step: Fix GameEngine.__init__ calls to include ai_controller parameter.",
    )

    assert result.startswith("ERROR:")
    assert "already shows that parameter" in result


def test_submit_research_summary_rejects_truncated_preview_with_current_file(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    source = workspace / "backend" / "bots"
    source.mkdir(parents=True)
    (source / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": '{"content": "def get_game_state_tool(...',
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": "backend/bots/bot_tools.py missing get_game_state_tool export.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="backend/bots/bot_tools.py missing get_game_state_tool export.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_submit_research_summary_rejects_without_passing_optional_param_claim(
    tmp_path,
):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        self.ai_controller = ai_controller\n",
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "main.py"},
            "result_preview": json.dumps({"content": "engine = GameEngine(game=game)"}),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": (
                    "GameEngine instances are created without passing "
                    "ai_controller, causing missing required positional errors."
                ),
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes=(
            "main.py creates GameEngine instances without passing ai_controller, "
            "causing a missing required positional argument."
        ),
    )

    assert result.startswith("ERROR:")
    assert "already shows that parameter" in result


def test_submit_research_summary_rejects_expected_symbol_mismatch_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    source = workspace / "backend" / "bots"
    source.mkdir(parents=True)
    (source / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    tests_dir = workspace / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_bot_tools.py").write_text(
        "from backend.bots.bot_tools import get_game_state_tool\n",
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "tests/test_bot_tools.py"},
            "result_preview": json.dumps(
                {"content": "from backend.bots.bot_tools import get_game_state_tool\n"}
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": json.dumps(
                {"content": "def get_game_state_tool(game_state, player_id):\n    pass\n"}
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": (
                    "tests/test_bot_tools.py expects get_game_state_tool "
                    "function but bot_tools.py contains GetGameStateTool class."
                ),
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes=(
            "pytest fails because tests/test_bot_tools.py expects "
            "get_game_state_tool function but bot_tools.py contains "
            "GetGameStateTool class."
        ),
    )

    assert result.startswith("ERROR:")
    assert "does not contain that class definition" in result


def test_submit_research_summary_rejects_false_class_claim_in_finding(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    source = workspace / "backend" / "bots"
    source.mkdir(parents=True)
    (source / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": json.dumps(
                {"content": "def get_game_state_tool(game_state, player_id):\n    pass\n"}
            ),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Bot tools",
                "content": (
                    "backend/bots/bot_tools.py contains GetGameStateTool "
                    "class and get_game_state_tool instance."
                ),
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="Concrete notes summarize current bot tool findings.",
    )

    assert result.startswith("ERROR:")
    assert "finding-1" in result
    assert "does not contain that class definition" in result


def test_submit_research_summary_rejects_param_claim_without_target_read(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "README.md"},
            "result_preview": json.dumps({"content": "Project readme only."}),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": "GameEngine.__init__() missing required ai_controller parameter.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="GameEngine.__init__() missing required ai_controller parameter.",
    )

    assert result.startswith("ERROR:")
    assert "no current source file" in result


def test_submit_research_summary_allows_missing_symbol_when_read_file_lacks_it(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    rows = [
        {
            "task_id": "run-1:research",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": json.dumps({"content": "def other_tool():\n    pass\n"}),
        },
        {
            "task_id": "run-1:research",
            "tool": "palace_add",
            "args": {
                "title": "Verification blockers",
                "content": "backend/bots/bot_tools.py missing get_game_state_tool export.",
            },
            "result_preview": json.dumps(
                {"saved": True, "legacy": {"id": "finding-1"}}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research"
    ctx.loop_state_view = {"phase_label": "research"}

    result = _submit_research_summary(
        ctx,
        architecture_id="arch-1",
        findings_ids=["finding-1"],
        notes="backend/bots/bot_tools.py missing get_game_state_tool export.",
    )

    assert result.startswith("OK: Research summary submitted")


def test_research_review_ok_requires_latest_summary_read(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "notes": "main.py endpoint claim needs review.",
            }
        ),
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research is verified.",
    )

    assert result.startswith("ERROR:")
    assert "research_summary_latest.json" in result


def test_research_review_ok_allows_external_claims_for_empty_workspace(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir()
    (workspace / "TASK_MAIN.md").write_text("Build an LLM game.", encoding="utf-8")
    (workspace / "workspace.toml").write_text("[skills]\n", encoding="utf-8")
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "run-1",
                "notes": (
                    "Use external GMAS FastAPI React TypeScript architecture; "
                    "workspace is intentionally empty before implementation."
                ),
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
        {
            "ts": "1970-01-01T00:02:01Z",
            "task_id": "run-1:research_review",
            "tool": "get_gmas_context",
            "args": {"query": "GMAS FastAPI React TypeScript"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="External framework claims validated via get_gmas_context.",
    )

    assert result.startswith("OK: Micro-review submitted: ok")


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_research_review_revise_rejects_stale_missing_symbol_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        notes="Critical blocker: backend/bots/bot_tools.py missing get_game_state_tool export.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_research_review_revise_rejects_nonblocking_prior_art_wording(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "Correct prior-art summary wording and repository URLs; "
            "the architecture is otherwise actionable."
        ],
        notes="Minor discrepancy in novelty phrasing only.",
    )

    assert result.startswith("ERROR:")
    assert "verdict=ok" in result


def test_research_review_revise_allows_unsafe_hot_memory_blocker(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "state").mkdir()
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_2dc4819e:research_review"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "Finding 4 in hot context still contains unrevised "
            "'Graceful Degradation: When LLM credentials are absent, provide "
            "mock/simulation mode for testing without paying for AI' language.",
            "Replace that policy-violating research memory with a corrected "
            "palace_add finding that requires explicit configuration and "
            "surfaced startup/runtime errors.",
        ],
        notes=(
            "The detailed finding artifact retains policy-violating mock mode "
            "language. This unsafe hot memory can be recalled by planning."
        ),
    )

    assert result.startswith("OK: Micro-review submitted: revise")


def test_research_review_ok_rejects_summary_citing_unsafe_hot_memory(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    state.mkdir()
    unsafe_finding = (
        "## LLM Runtime Environment Contract\n"
        "Required variables: OUROBOROS_LLM_API_KEY / LLM_API_KEY, "
        "OUROBOROS_LLM_BASE_URL / LLM_BASE_URL, and OUROBOROS_MODEL / "
        "LLM_MODEL.\n"
        "Fallback Priority: check OUROBOROS_LLM_* variables first, then fall "
        "back to LLM_* credential aliases. If both are missing, fail with a "
        "clear error message.\n"
        "Implementation Requirements: Graceful Degradation: When LLM "
        "credentials are absent, provide mock/simulation mode for testing "
        "without paying for AI.\n"
        "Testing Strategy: Unit tests without real LLM (mock responses)."
    )
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "phase_web_2dc4819e",
                "architecture_id": "arch-civilization-gmas-web-v1",
                "findings_ids": ["048bd6a8-c56a-491d-b16d-f632175b8dbd"],
                "notes": (
                    "Application validates presence of LLM credentials "
                    "(OUROBOROS_LLM_API_KEY or LLM_API_KEY, "
                    "OUROBOROS_LLM_BASE_URL or LLM_BASE_URL, "
                    "OUROBOROS_MODEL or LLM_MODEL) at startup and raises a "
                    "clear error if missing. No mock/fallback mode."
                ),
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:01:00Z",
            "task_id": "phase_web_2dc4819e:research",
            "tool": "palace_add",
            "args": {
                "title": "LLM Runtime Environment Contract",
                "content": unsafe_finding,
                "tags": "environment,gmas,llm-credentials,testing",
            },
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "048bd6a8-c56a-491d-b16d-f632175b8dbd",
                    "legacy": {"id": "drawer_6c78e8f82c19a1320a3194f9"},
                }
            ),
        },
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "phase_web_2dc4819e:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_2dc4819e:research_review"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes=(
            "Research quality is sufficient. Minor citation issue: Finding 4 "
            "in hot context retains old Graceful Degradation and "
            "mock/simulation mode language, but the authoritative summary says "
            "No mock/fallback mode, so planning can proceed."
        ),
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result
    assert "Research review cannot accept ok" in result


def test_research_review_ok_rejects_captured_rule_based_degradation_summary(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    state.mkdir()
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "phase_web_9701612a",
                "architecture_id": "arch-civilization-gmas-web-v1",
                "findings_ids": ["agents-1"],
                "notes": (
                    "LLM runtime environment contract requiring resolution of "
                    "LLM_API_KEY, "
                    "LLM_BASE_URL, and "
                    "LLM_MODEL with graceful degradation to "
                    "rule-based AI when credentials missing."
                ),
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:01:00Z",
            "task_id": "phase_web_9701612a:research",
            "tool": "palace_add",
            "args": {
                "content": "GMAS bot agents use the inherited LLM runtime.",
                "tags": "research_finding,gmas,llm",
            },
            "result_preview": json.dumps({"saved": True, "id": "agents-1"}),
        },
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "phase_web_9701612a:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_9701612a:research_review"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research summary was read and seems complete.",
    )

    assert result.startswith("ERROR:")
    assert "forbidden LLM fallback" in result


def test_research_review_ok_rejects_captured_mock_llm_behavior_summary(tmp_path):
    workspace = tmp_path / "workspaces" / "civilization"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    state = drive / "state"
    logs.mkdir(parents=True)
    state.mkdir()
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "phase_web_c8307523",
                "architecture_id": "arch-civilization-gmas-web-v1",
                "findings_ids": ["gmas-1"],
                "notes": (
                    "Civilization-style strategy game architecture combining "
                    "Python GMAS-based LLM bots with TypeScript/JSX frontend. "
                    "All LLM bots must use inherited Umbrella runtime with "
                    "LLM_API_KEY, "
                    "LLM_BASE_URL, and "
                    "LLM_MODEL. Testing strategy combines "
                    "unit tests, integration tests (mock LLM for bot behavior "
                    "verification), and runtime validation."
                ),
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:01:00Z",
            "task_id": "phase_web_c8307523:research",
            "tool": "palace_add",
            "args": {
                "content": "GMAS bot agents use the inherited real LLM runtime.",
                "tags": "research_finding,gmas,llm",
            },
            "result_preview": json.dumps({"saved": True, "id": "gmas-1"}),
        },
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "phase_web_c8307523:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_c8307523:research_review"
    ctx.loop_state_view = {
        "phase_label": "linear",
        "active_workspace_id": "civilization",
    }

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research summary was read and appears complete.",
    )

    assert result.startswith("ERROR:")
    assert "mock/fake/dry-run LLM" in result


def test_submit_micro_review_rejects_empty_revise_feedback(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=["  "],
        notes="  ",
    )

    assert result.startswith("ERROR:")
    assert "requires actionable feedback" in result


def test_plan_review_ok_requires_submitted_plan_artifact_read(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "phase_web_2e5b4630",
                "plan_id": "phase_plan:01_backend_core",
                "plan": {
                    "subtasks": [
                        {
                            "id": "01_backend_stack",
                            "title": "Backend stack",
                            "files_to_create": [
                                "src/civ/server/app.py",
                                "tests/test_backend_stack.py",
                            ],
                            "success_test": "pytest tests/test_backend_stack.py -v",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "phase_web_2e5b4630:plan_review",
            "tool": "search_gmas_knowledge",
            "args": {"query": "MACPRunner GraphBuilder agent tools"},
            "result_preview": "{}",
        },
        {
            "ts": "1970-01-01T00:02:10Z",
            "task_id": "phase_web_2e5b4630:plan_review",
            "tool": "load_skill",
            "args": {"slug": "review-checklist"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_2e5b4630:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(ctx, verdict="ok")

    assert result.startswith("ERROR:"), result
    assert "phase_plan_submitted_latest.json" in result
    assert "submitted handoff from memory" in result


def test_plan_review_ok_accepts_after_submitted_plan_artifact_read(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "phase_web_2e5b4630",
                "plan_id": "phase_plan:01_backend_core",
                "plan": {
                    "subtasks": [
                        {
                            "id": "01_backend_stack",
                            "title": "Backend stack",
                            "files_to_create": [
                                "src/civ/server/app.py",
                                "tests/test_backend_stack.py",
                            ],
                            "success_test": "pytest tests/test_backend_stack.py -v",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "phase_web_2e5b4630:plan_review",
            "tool": "read_file",
            "args": {
                "file_path": (
                    ".memory/drive/state/phase_plan_submitted_latest.json"
                )
            },
            "result_preview": "{}",
        }
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_2e5b4630:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(ctx, verdict="ok")

    assert result.startswith("OK: Micro-review submitted: ok")


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_plan_review_ok_rejects_captured_conservative_strategy_fallback_plan(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "phase_web_30316f53",
                "plan_id": "LLM-Powered Civilization Game Implementation",
                "plan": {
                    "workspace_id": "civilization",
                    "llm_runtime_contract": {
                        "env_var_pairs": [
                            ["OUROBOROS_LLM_API_KEY", "LLM_API_KEY"],
                            ["OUROBOROS_LLM_BASE_URL", "LLM_BASE_URL"],
                            ["OUROBOROS_MODEL", "LLM_MODEL"],
                        ],
                        "fallback_behavior": (
                            "GMAS agents and LLM tools must check both "
                            "OUROBOROS_* and LLM_* variants. If neither variant "
                            "is available, fail initialization with a clear "
                            "error message explaining which environment "
                            "variables are required."
                        ),
                    },
                    "subtasks": [
                        {
                            "id": "5.2",
                            "title": "Implement LLM tooling and decision pipelines",
                            "goal": (
                                "AI decision tools call LLM via both OUROBOROS_* "
                                "and LLM_* env vars, apply actions to game state, "
                                "log decisions, and test LLM integration."
                            ),
                            "files_to_create": ["src/civilization/ai_tools.py"],
                            "success_test": (
                                "python -m pytest tests/test_ai_tools.py -q"
                            ),
                        }
                    ],
                    "decision_policy": {
                        "agent_behavior": (
                            "AI decisions must come from GMAS multi-agent system, "
                            "not hardcoded rules. LLM failure logs error and uses "
                            "fallback conservative strategy."
                        )
                    },
                },
                "notes": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "phase_web_30316f53:plan_review",
            "tool": "read_file",
            "args": {
                "file_path": ".memory/drive/state/phase_plan_submitted_latest.json"
            },
            "result_preview": "{}",
        }
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_30316f53:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(ctx, verdict="ok")

    assert result.startswith("ERROR:"), result
    assert "violates workspace policy" in result
    assert "fallback" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_plan_review_ok_rejects_captured_final_proof_gap_plan(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "phase_web_883b9f7e",
                "plan_id": "phase_plan:docs-env",
                "plan": {
                    "subtasks": [
                        {
                            "id": "integration-e2e",
                            "title": "Test full game loop with real AI decisions",
                            "goal": (
                                "Create integration proof that simulates player "
                                "turn and AI decisions."
                            ),
                            "files_to_create": [
                                "tests/integration/test_game_loop.py"
                            ],
                            "success_test": (
                                "python -m pytest "
                                "tests/integration/test_game_loop.py -q"
                            ),
                        },
                        {
                            "id": "final-verification",
                            "title": "Verify localhost game deployment",
                            "goal": (
                                "Start FastAPI backend and React frontend "
                                "locally, simulate a player turn, and verify "
                                "WebSocket behavior."
                            ),
                            "files_to_change": ["workspace.toml"],
                            "success_test": (
                                "python -m pytest "
                                "tests/integration/test_game_loop.py -q"
                            ),
                        },
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "phase_web_883b9f7e:plan_review",
            "tool": "read_file",
            "args": {
                "file_path": ".memory/drive/state/phase_plan_submitted_latest.json"
            },
            "result_preview": "{}",
        }
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_883b9f7e:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes=(
            "The only note is that final-verification shares the same "
            "success_test as integration-e2e, but this is not a blocker."
        ),
    )

    assert result.startswith("ERROR:"), result
    assert "violates workspace policy" in result
    assert "final-verification" in result
    assert "distinct final proof artifact" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_plan_review_rejects_nonblocking_detail_loop_for_executable_plan(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "plan_id": "phase_plan:civilization",
                "plan": {
                    "subtasks": [
                        {
                            "id": "st_3_1",
                            "title": "Build GMAS graph topology",
                            "goal": (
                                "Implement economy, diplomacy, and military GMAS "
                                "agents with a graph topology and decision reducer."
                            ),
                            "files_to_create": [
                                "src/civgame/ai/graph.py",
                                "tests/test_agent_graph.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_agent_graph.py -q"
                            ),
                        },
                        {
                            "id": "st_3_4",
                            "title": "Persist multi-turn agent memory",
                            "goal": (
                                "Use AgentMemory or SharedMemoryPool so LLM bot "
                                "state persists across turns."
                            ),
                            "files_to_create": [
                                "src/civgame/ai/memory.py",
                                "tests/test_agent_memory.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_agent_memory.py -q"
                            ),
                        },
                        {
                            "id": "st_4_2",
                            "title": "Stream turns over WebSocket",
                            "goal": (
                                "Implement WebSocket streaming for game turns and "
                                "frontend error handling."
                            ),
                            "files_to_create": [
                                "src/civgame/api/websockets.py",
                                "frontend/src/hooks/useWebSocket.ts",
                                "tests/test_websocket_stream.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_websocket_stream.py -q"
                            ),
                        },
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Phase 3 (st_3_1): Clarify GMAS graph topology - Specify how "
                "economy/diplomacy/military agents interact in the graph "
                "(chain, parallel, or hub-and-spoke) and how decisions converge "
                "into a single game action."
            ),
            (
                "Phase 3 (st_3_4): Add explicit multi-turn session management - "
                "GMAS uses AgentMemory and SharedMemoryPool for maintaining "
                "state across turns."
            ),
            (
                "Phase 4 (st_4_2): Add WebSocket reconnection handling - "
                "Specify handling for connection drops, backoff strategy, and "
                "error propagation to the frontend."
            ),
        ],
        notes=(
            "These are execution details observed during review of an otherwise "
            "executable plan."
        ),
    )

    assert result.startswith("ERROR:"), result
    assert "verdict=ok notes" in result


def test_plan_review_allows_revise_for_policy_detected_final_proof_gap(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "phase_web_883b9f7e",
                "plan_id": "phase_plan:docs-env",
                "plan": {
                    "subtasks": [
                        {
                            "id": "integration-e2e",
                            "title": "Test full game loop with real AI decisions",
                            "goal": (
                                "Create integration proof that simulates player "
                                "turn and AI decisions."
                            ),
                            "files_to_create": [
                                "tests/integration/test_game_loop.py"
                            ],
                            "success_test": (
                                "python -m pytest "
                                "tests/integration/test_game_loop.py -q"
                            ),
                        },
                        {
                            "id": "final-verification",
                            "title": "Verify localhost game deployment",
                            "goal": (
                                "Start FastAPI backend and React frontend "
                                "locally, simulate a player turn, and verify "
                                "WebSocket behavior."
                            ),
                            "files_to_change": ["workspace.toml"],
                            "success_test": (
                                "python -m pytest "
                                "tests/integration/test_game_loop.py -q"
                            ),
                        },
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (logs / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_883b9f7e:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "In subtask final-verification, replace the repeated "
                "success_test with a distinct localhost verification proof "
                "owned by that leaf."
            )
        ],
        notes=(
            "The submitted plan is otherwise executable, but final-verification "
            "does not prove its stated deployment goal."
        ),
    )

    assert result.startswith("OK: Micro-review submitted: revise"), result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_plan_review_rejects_protective_fallback_clarification_loop_with_linear_label(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "plan_id": "phase_plan:civilization",
                "plan": {
                    "subtasks": [
                        {
                            "id": "llm_agents",
                            "title": "Implement live LLM agents",
                            "goal": (
                                "Resolve LLM_API_KEY, "
                                "LLM_BASE_URL, and "
                                "LLM_MODEL, then call live LLM "
                                "agents for economic and diplomatic decisions. "
                                "NEVER use deterministic fallback."
                            ),
                            "files_to_create": [
                                "src/civgame/ai/agents.py",
                                "tests/test_live_llm_agents.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_live_llm_agents.py -q"
                            ),
                        }
                    ],
                    "risk_mitigation": {
                        "llm_latency": (
                            "Retry transient errors, pause the game on persistent "
                            "LLM failure, and NEVER use deterministic fallback."
                        )
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "In risk_mitigation.llm_latency, clarify that LLM errors must "
                "surface explicitly and pause the game - ensure the implementation "
                "does not include ANY caching or graceful degradation logic that "
                "would substitute cached responses for fresh LLM outputs"
            ),
            (
                "Add explicit note in architecture documentation that LLM agents "
                "must always call the live LLM API for economic/diplomatic "
                "decisions without any response caching or pre-computed fallback paths"
            ),
        ],
        notes=(
            "Plan is fundamentally sound; this is a clarification to prevent "
            "ambiguity, not a blocking plan defect."
        ),
    )

    assert result.startswith("ERROR:"), result
    assert "verdict=ok notes" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_plan_review_rejects_captured_execution_detail_revise_for_sound_llm_plan(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_2bb43a8f",
                "plan_id": "phase_plan:phase_1_engine",
                "plan": {
                    "title": "LLM-Powered Civilization Game",
                    "objective": (
                        "Build a simplified strategy game with LLM-driven AI "
                        "opponents for economy, diplomacy, and decisions."
                    ),
                    "phases": [
                        {
                            "id": "phase_1_engine",
                            "subtasks": [
                                {
                                    "id": "st_1_models",
                                    "title": "Domain Models and Game State",
                                    "description": (
                                        "Implement Tile, Map, City, Unit, "
                                        "DiplomaticState, Player, and GameState."
                                    ),
                                    "files_to_create": [
                                        "src/civgame/models/game_state.py",
                                        "tests/test_models.py",
                                    ],
                                    "success_test": (
                                        "python -m pytest tests/test_models.py -q"
                                    ),
                                }
                            ],
                        },
                        {
                            "id": "phase_2_agents",
                            "subtasks": [
                                {
                                    "id": "st_2_bot_agent",
                                    "title": "Bot Agent Graph Construction",
                                    "description": (
                                        "Use GMAS GraphBuilder to create Economic, "
                                        "Military, Diplomat, and Commander agents "
                                        "with personas and workflow edges."
                                    ),
                                    "files_to_create": [
                                        "src/civgame/agents/bot_graph.py",
                                        "tests/test_bot_graph.py",
                                    ],
                                    "success_test": (
                                        "python -m pytest tests/test_bot_graph.py -q"
                                    ),
                                },
                                {
                                    "id": "st_2_llm_integration",
                                    "title": "LLM Runner and Tool Integration",
                                    "description": (
                                        "Configure MACPRunner with ToolRegistry, "
                                        "real runtime env aliases, streaming bot "
                                        "turn execution, retry, timeout, and "
                                        "explicit surfaced errors."
                                    ),
                                    "files_to_create": [
                                        "src/civgame/agents/llm_runner.py",
                                        "tests/test_llm_runner.py",
                                    ],
                                    "success_test": (
                                        "python -m pytest tests/test_llm_runner.py -q"
                                    ),
                                },
                            ],
                        },
                        {
                            "id": "phase_3_api",
                            "subtasks": [
                                {
                                    "id": "st_3_bot_integration",
                                    "title": "Bot Turn Integration",
                                    "description": (
                                        "Connect real LLM bots to game turns and "
                                        "propagate LLM errors to the UI."
                                    ),
                                    "files_to_create": [
                                        "src/civgame/api/bot_integration.py",
                                        "tests/test_bot_integration.py",
                                    ],
                                    "success_test": (
                                        "python -m pytest "
                                        "tests/test_bot_integration.py -q"
                                    ),
                                }
                            ],
                        },
                        {
                            "id": "phase_4_frontend",
                            "subtasks": [
                                {
                                    "id": "st_4_websocket_client",
                                    "title": "WebSocket Client",
                                    "description": (
                                        "Implement connection lifecycle and game "
                                        "state updates in the React client."
                                    ),
                                    "files_to_create": [
                                        "frontend/src/hooks/useGameSocket.ts",
                                        "tests/verify_frontend_build.py",
                                    ],
                                    "success_test": (
                                        "python -m pytest "
                                        "tests/verify_frontend_build.py -q"
                                    ),
                                }
                            ],
                        },
                        {
                            "id": "phase_5_verification",
                            "subtasks": [
                                {
                                    "id": "st_5_integration",
                                    "title": "Integration Tests",
                                    "description": (
                                        "Run human actions through WebSocket and "
                                        "spawn real LLM bots using inherited env."
                                    ),
                                    "files_to_create": [
                                        "tests/test_integration.py"
                                    ],
                                    "success_test": (
                                        "python -m pytest "
                                        "tests/test_integration.py -q"
                                    ),
                                }
                            ],
                        },
                        {
                            "id": "phase_6_docs",
                            "subtasks": [
                                {
                                    "id": "st_6_architecture",
                                    "title": "Architecture Documentation",
                                    "description": (
                                        "Document GMAS agent topology and data flow."
                                    ),
                                    "files_to_create": [
                                        "docs/architecture.md",
                                        "docs/agent_topology.md",
                                        "tests/verify_docs_exist.py",
                                    ],
                                    "success_test": (
                                        "python -m pytest "
                                        "tests/verify_docs_exist.py -q"
                                    ),
                                }
                            ],
                        },
                    ],
                    "decision_policy": {
                        "llm_intelligence": (
                            "Bots use real LLM via MACPRunner with "
                            "LLM_API_KEY, "
                            "LLM_BASE_URL, and "
                            "LLM_MODEL. No mock or fallback."
                        )
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_2bb43a8f:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add explicit subtask for agent prompt definition creation: "
                "After st_2_bot_agent, add st_2_agent_prompts to create "
                "src/civgame/agents/prompts.py with structured prompt templates "
                "for EconomicAdvisor, MilitaryAdvisor, Diplomat, Commander."
            ),
            (
                "Refine st_2_llm_integration success_test to be testable: "
                "Change from pytest tests/test_llm_runner.py -v to specific "
                "test targets."
            ),
            (
                "Add missing LLM error handling subtask: After "
                "st_2_llm_integration, add st_2_error_handling with retry "
                "logic, timeout handling, and explicit error propagation to UI "
                "via WebSocket."
            ),
            (
                "Expand bot behavior testing in st_5_integration: Add explicit "
                "test requirement for multi-bot interaction and decision variety."
            ),
            (
                "Specify hex grid algorithm detail in st_1_models: Add precise "
                "hex coordinate system."
            ),
            (
                "Add concurrent action handling policy in st_6_architecture; "
                "document state mutation thread-safety guarantees."
            ),
            (
                "Refine st_4_websocket_client success_test to a specific "
                "connection lifecycle test."
            ),
        ],
        notes=(
            "The plan is well-structured with clear phases, subtasks, and a "
            "strong decision policy around real LLM usage. Critical gaps "
            "requiring revision are implementation details and scenario coverage."
        ),
    )

    assert result.startswith("ERROR:"), result
    assert "verdict=ok notes" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_plan_review_rejects_captured_gmas_tool_permission_detail_loop(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_ec31ee7c",
                "plan_id": "phase_web_ec31ee7c:plan",
                "plan": {
                    "description": (
                        "Build a simplified Civilization game with LLM-powered "
                        "AI bots, FastAPI, React/TypeScript, and GMAS agents."
                    ),
                    "subtasks": [
                        {
                            "id": "setup_fastapi_backend",
                            "title": (
                                "Create FastAPI backend with WebSocket support"
                            ),
                            "goal": (
                                "Implement API endpoints and WebSocket for "
                                "real-time game communication using "
                                "OUROBOROS_LLM_*/LLM_* env vars."
                            ),
                            "files_to_create": [
                                "src/civilization/api/main.py",
                                "src/civilization/api/websocket.py",
                                "tests/test_api_config.py",
                            ],
                            "success_test": (
                                "pytest tests/test_api_config.py::"
                                "test_fastapi_app_creation -q"
                            ),
                        },
                        {
                            "id": "implement_gmas_bot_agents",
                            "title": (
                                "Implement GMAS-based AI bot agents with tools"
                            ),
                            "goal": (
                                "Create LLM-powered bot agents with tools for "
                                "game actions using GMAS."
                            ),
                            "tools_allowed": [
                                "update_workspace_seed",
                                "get_gmas_context",
                                "search_gmas_knowledge",
                            ],
                            "files_to_create": [
                                "src/civilization/agents/tools.py",
                                "src/civilization/agents/bot.py",
                                "tests/test_bot_tools.py",
                            ],
                            "success_test": "pytest tests/test_bot_tools.py -q",
                        },
                        {
                            "id": "implement_game_logic",
                            "title": (
                                "Implement core game mechanics and rules"
                            ),
                            "goal": (
                                "Create game loop with turn processing, "
                                "resource calculation, conflict resolution, "
                                "and GMAS bot turn execution."
                            ),
                            "files_to_create": [
                                "src/civilization/game/engine.py",
                                "tests/test_game_engine.py",
                            ],
                            "success_test": (
                                "pytest tests/test_game_engine.py::"
                                "test_engine_instantiation -q"
                            ),
                        },
                        {
                            "id": "create_react_frontend",
                            "title": "Create React/TypeScript frontend",
                            "goal": (
                                "Build interactive UI and WebSocket client for "
                                "real-time game updates."
                            ),
                            "files_to_create": [
                                "frontend/src/App.tsx",
                                "tests/test_frontend_build.py",
                            ],
                            "success_test": (
                                "pytest tests/test_frontend_build.py -q"
                            ),
                        },
                        {
                            "id": "integrate_llm_communication",
                            "title": (
                                "Integrate natural language LLM communication"
                            ),
                            "goal": (
                                "Enable bots to communicate with the player "
                                "through natural language for diplomacy and "
                                "trade using GMAS tools."
                            ),
                            "tools_allowed": [
                                "update_workspace_seed",
                                "search_gmas_knowledge",
                                "get_gmas_context",
                            ],
                            "files_to_create": [
                                "src/civilization/agents/llm_communication.py",
                                "tests/test_llm_communication.py",
                            ],
                            "success_test": (
                                "pytest tests/test_llm_communication.py::"
                                "test_llm_config_loaded -q"
                            ),
                        },
                        {
                            "id": "implement_economy_diplomacy",
                            "title": (
                                "Implement LLM-driven economy and diplomacy"
                            ),
                            "goal": (
                                "Create sophisticated AI where bots evaluate "
                                "and negotiate based on game state."
                            ),
                            "files_to_create": [
                                "src/civilization/game/economics.py",
                                "tests/test_economics.py",
                            ],
                            "success_test": (
                                "pytest tests/test_economics.py::"
                                "test_economy_system_instantiation -q"
                            ),
                        },
                        {
                            "id": "test_integration_end_to_end",
                            "title": (
                                "Integration testing and end-to-end gameplay "
                                "verification"
                            ),
                            "goal": (
                                "Verify complete gameplay loop through bot AI "
                                "interactions and WebSocket synchronization."
                            ),
                            "files_to_create": ["tests/test_integration.py"],
                            "success_test": (
                                "pytest tests/test_integration.py::"
                                "test_basic_game_flow -q"
                            ),
                        },
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_ec31ee7c:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add `get_gmas_context` and `search_gmas_knowledge` to "
                "tools_allowed for subtask 6 (implement_game_logic) to enable "
                "proper GMAS integration for turn processing and conflict "
                "resolution"
            ),
            (
                "Add specific GMAS tool creation pattern to subtask 5 "
                "(implement_gmas_bot_agents): specify using `@tool` decorator "
                "or `create_openai_caller()` pattern from GMAS examples for "
                "bot action tools (propose_trade, declare_war, build_structure, "
                "move_unit)"
            ),
            (
                "Add `get_gmas_context` and `search_gmas_knowledge` to "
                "tools_allowed for subtask 9 (implement_economy_diplomacy) to "
                "enable LLM-driven negotiation system design"
            ),
            (
                "Insert a mid-plan checkpoint subtask after subtask 7 "
                "(create_react_frontend) or merge with existing verification: "
                "add a new subtask 'verify_backend_components' with success "
                "test 'pytest tests/verify_backend.py -q' to ensure core game "
                "engine, FastAPI backend, and GMAS bot tools work before "
                "proceeding to LLM integration"
            ),
            (
                "Clarify in subtask 8 (integrate_llm_communication) that tools "
                "will use GMAS `get_registry().register()` pattern and native "
                "function calling via MACPRunner, referencing "
                "agent_with_tools_example.py pattern for tool creation and "
                "agent configuration"
            ),
            (
                "Add explicit verification step for WebSocket communication in "
                "subtask 4 (setup_fastapi_backend): success test should include "
                "'pytest tests/test_websocket.py -q' to verify WebSocket "
                "connections work end-to-end with message serialization"
            ),
            (
                "Add a new test file specification in subtask 11 "
                "(test_integration_end_to_end): ensure 'tests/test_integration.py' "
                "includes at least one test that verifies an actual GMAS agent "
                "tool call (not just state checks) to prove LLM integration works"
            ),
        ],
        notes=(
            "The phase plan has a solid structure and complete verification "
            "path, but tool permissions are under-constrained for complex "
            "LLM-driven subtasks. The GMAS framework integration needs explicit "
            "tool patterns specified in the documentation, and a mid-plan "
            "checkpoint would reduce risk of discovering integration issues "
            "late in the pipeline. The revisions address these gaps while "
            "maintaining the overall architecture."
        ),
    )

    assert result.startswith("ERROR:"), result
    assert "blocking plan defects" in result
    assert "verdict=ok notes" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_plan_review_rejects_captured_package_e2e_detail_revise_loop(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_4a0129c3",
                "plan_id": "phase_plan:project-setup",
                "plan": {
                    "llm_runtime_contract": {
                        "failure_handling": (
                            "Retry bounded transient LLM call errors, pause "
                            "affected bot turn on persistent failure, and never "
                            "replace required bot decisions with static logic."
                        ),
                        "required_env_aliases": [
                            "OUROBOROS_LLM_API_KEY or LLM_API_KEY",
                            "OUROBOROS_LLM_BASE_URL or LLM_BASE_URL",
                            "OUROBOROS_MODEL or LLM_MODEL",
                        ],
                    },
                    "subtasks": [
                        {
                            "id": "project-setup",
                            "title": (
                                "Initialize project structure and documentation"
                            ),
                            "goal": (
                                "Create src/civgame, docs, tests, and frontend "
                                "package scaffolding for the playable app."
                            ),
                            "files_to_create": [
                                "pyproject.toml",
                                "src/civgame/__init__.py",
                                "docs/architecture.md",
                                "docs/agent_topology.md",
                                "frontend/package.json",
                                "tests/test_setup.py",
                            ],
                            "success_test": "python -m pytest tests/test_setup.py -q",
                        },
                        {
                            "id": "frontend-scaffold",
                            "title": "Initialize frontend project structure",
                            "goal": (
                                "Create Vite React/TypeScript frontend "
                                "structure and package commands."
                            ),
                            "files_to_create": [
                                "frontend/package.json",
                                "frontend/tsconfig.json",
                                "frontend/src/main.tsx",
                            ],
                            "success_test": "cd frontend && npm run type-check",
                        },
                        {
                            "id": "gmas-bots",
                            "title": "Implement GMAS-based AI bot agents",
                            "goal": (
                                "Use GMAS and inherited runtime env aliases for "
                                "LLM-driven diplomatic and economic bot turns."
                            ),
                            "files_to_create": [
                                "src/civgame/ai/bots.py",
                                "tests/test_bot_agents.py",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_bot_agents.py -q"
                            ),
                        },
                        {
                            "id": "e2e-gameplay",
                            "title": (
                                "Verify end-to-end gameplay and local "
                                "development environment"
                            ),
                            "goal": (
                                "Start backend/frontend and verify full game "
                                "loop works locally with player and AI bot turns."
                            ),
                            "files_to_create": [
                                "tests/test_e2e_gameplay.py",
                                "README.md",
                            ],
                            "success_test": (
                                "python -m pytest tests/test_e2e_gameplay.py -q"
                            ),
                        },
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_4a0129c3:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "In subtask 'frontend-scaffold', add 'scripts' field to "
                "package.json with explicit definitions: 'type-check': "
                "'tsc --noEmit', 'build': 'vite build', 'test:unit': "
                "'vitest run --coverage'. The success_test references "
                "'npm run type-check' but package.json only shows basic "
                "files to create, not the scripts section."
            ),
            (
                "In subtask 'e2e-gameplay', add explicit verification "
                "requirement: E2E test must simulate a multi-turn gameplay "
                "scenario with LLM-driven diplomatic/economic decisions and "
                "assert game state changes."
            ),
            (
                "In subtask 'e2e-gameplay', revise goal to create dev scripts "
                "that launch backend and frontend, and add success_test "
                "verification that dev.sh/dev.bat start both services."
            ),
        ],
        notes=(
            "Overall plan structure is solid. LLM runtime contract correctly "
            "specifies OUROBOROS_LLM_* aliases with no mock/fake LLM fallbacks. "
            "The blocking issues are frontend scripts referenced but not "
            "defined, e2e needs explicit scenario validation, and dev scripts "
            "must prove services launch."
        ),
    )

    assert result.startswith("ERROR:"), result
    assert "verdict=ok notes" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_plan_review_rejects_captured_python_c_success_test_rewrite_loop(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_f0899788",
                "plan_id": "phase_plan:1",
                "plan": {
                    "subtasks": [
                        {
                            "id": "1.1",
                            "title": "Create game state and GMAS graph",
                            "goal": (
                                "Build core game state and agent graph "
                                "structure for LLM-driven bots."
                            ),
                            "files_to_create": [
                                "src/civgame/game_state.py",
                                "src/civgame/ai_graph.py",
                                "tests/test_game_state.py",
                            ],
                            "success_test": (
                                "pytest tests/test_game_state.py -v "
                                "-k test_core_structures"
                            ),
                        },
                        {
                            "id": "1.2",
                            "title": "Create AI tool registry",
                            "goal": "Expose economy, diplomacy, and military tools.",
                            "files_to_create": [
                                "src/civgame/ai_tools.py",
                                "tests/test_ai_engine.py",
                            ],
                            "success_test": (
                                "pytest tests/test_ai_engine.py -v "
                                "-k test_ai_tools_available"
                            ),
                        },
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_f0899788:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Subtask 1.1: Change success_test from "
                "'pytest tests/test_game_state.py -v -k test_core_structures' "
                "to 'python -c \"from civgame.game_state import GameState; "
                "from civgame.ai_graph import build_agent_graph; "
                "print('Import successful')\"' to avoid testing non-existent tests"
            ),
            (
                "Subtask 1.2: Change success_test from "
                "'pytest tests/test_ai_engine.py -v -k test_ai_tools_available' "
                "to 'python -c \"from civgame.ai_tools import "
                "get_available_tools; tools = get_available_tools(); "
                "assert len(tools) > 3\"' to verify tools exist before testing them"
            ),
            (
                "Add new subtask 1.4 before Phase 2: 'Verify end-to-end AI "
                "agent execution' with success_test that runs a minimal GMAS "
                "graph with LLM and proves agents can make decisions."
            ),
        ],
        notes=(
            "The plan has good structure and architecture but fails the review "
            "gate on circular test dependencies where success_tests reference "
            "test files created in the same subtask."
        ),
    )

    assert result.startswith("ERROR:"), result
    assert "python -c" in result
    assert "circular" in result


def test_plan_review_revise_still_allows_blocking_success_test_gap(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "plan_id": "phase_plan:civilization",
                "plan": {
                    "subtasks": [
                        {
                            "id": "st_4_2",
                            "title": "Stream turns over WebSocket",
                            "goal": "Implement WebSocket streaming.",
                            "files_to_create": ["src/civgame/api/websockets.py"],
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "subtask st_4_2 is missing success_test for WebSocket behavior."
        ],
        notes="The plan cannot be verified without a runnable command.",
    )

    assert result.startswith("OK:"), result


def test_plan_review_revise_allows_malformed_success_test_blocker(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "run_id": "phase_web_b34a047f",
                "plan_id": "phase_plan:project-setup-docs",
                "plan": {
                    "subtasks": [
                        {
                            "id": "project-setup-docs",
                            "title": (
                                "Create project structure with docs and "
                                "scaffolding"
                            ),
                            "goal": (
                                "Set up src/civlite package, frontend build "
                                "configs, and durable docs under docs/."
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
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_b34a047f:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "project-setup-docs: Fix malformed Windows-incompatible "
                "success_test. Replace the shell-redirection command with a "
                "single checked-in proof command."
            )
        ],
        notes=(
            "The plan structure is otherwise solid, but this success_test is a "
            "blocking verification defect because it masks command failure."
        ),
    )

    assert result.startswith("OK:"), result


def test_submit_micro_review_rejects_hardcoded_llm_config_fallback_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add explicit fallback mechanism in run_server.py: check "
                "LLM_API_KEY, then fall back to hardcoded localhost defaults."
            )
        ],
        notes="Plan needs config handling.",
    )

    assert result.startswith("ERROR:")
    assert "cannot request hardcoded/static/default fallback" in result


def test_submit_micro_review_rejects_cached_llm_fallback_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add LLM error handling note to risk assessment: document "
                "fallback strategy for API failures (retry, cached decisions, "
                "graceful degradation)."
            )
        ],
        notes="Plan needs LLM failure handling.",
    )

    assert result.startswith("ERROR:")
    assert "cached/graceful-degradation" in result


def test_submit_micro_review_rejects_fallback_model_strategy_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add to docs/LLM_CONFIG.md: specify recommended models, cost "
                "estimates per 100 turns, and fallback model strategy."
            )
        ],
        notes="Plan docs need provider configuration.",
    )

    assert result.startswith("ERROR:")
    assert "fallback behavior" in result


def test_submit_micro_review_rejects_hyphenated_fallback_behavior_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Specify LLM failure handling: add retry policy or fall-back "
                "behavior for bot turns."
            )
        ],
        notes="Plan needs explicit LLM failure handling.",
    )

    assert result.startswith("ERROR:")
    assert "fallback behavior" in result


def test_submit_micro_review_rejects_mock_llm_factory_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add setup_test_infrastructure subtask to create pytest "
                "fixtures and a mock factory for LLM responses."
            )
        ],
        notes="Plan needs tests.",
    )

    assert result.startswith("ERROR:")
    assert "mock/fake/dry-run LLM" in result


def test_submit_micro_review_rejects_provider_specific_model_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add docs/LLM_CONFIG.md and specify recommended models "
                "gpt-4o-mini for economy diplomacy and gpt-4o for critical decisions."
            )
        ],
        notes="Plan docs need model guidance.",
    )

    assert result.startswith("ERROR:")
    assert "provider-specific model" in result


def test_submit_micro_review_rejects_memory_drawer_edit_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Update all research/hall_events architecture documents "
                "(drawer_abc123 arch-civ-game-v1) to remove stale architecture claims."
            )
        ],
        notes="Stale memory should not be edited by the plan phase.",
    )

    assert result.startswith("ERROR:")
    assert "memory/research hall artifacts" in result


def test_submit_micro_review_rejects_nonportable_shell_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Update frontend verification with "
                "'timeout 5 npm --prefix frontend run dev || true' and "
                "use grep -q to check output."
            )
        ],
        notes="Plan needs a dev server smoke check.",
    )

    assert result.startswith("ERROR:")
    assert "non-portable Unix shell" in result


def test_submit_micro_review_rejects_no_test_tampering_removal_feedback(tmp_path):
    drive = tmp_path / "workspaces" / "calculator" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "proof_scope_mismatch",
                "severity": "blocking",
                "phase": "plan",
                "subtask_id": "integration-smoke",
                "message": (
                    "Either add production files to changed_files_expected or "
                    "remove no_test_tampering from this test-only subtask."
                ),
                "evidence_refs": [],
            }
        ],
        required_plan_changes=[
            "Remove no_test_tampering from integration-smoke required_properties."
        ],
        notes="The review found a blocking scope mismatch.",
    )

    assert result.startswith("ERROR:"), result
    assert "cannot request removing `no_test_tampering`" in result


def test_submit_micro_review_allows_ok_notes_describing_absent_shell_masks(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes=(
            "Checked proof commands: no Unix grep requirement, no timeout wrapper, "
            "no ps/pkill cleanup, and no failure masking with `|| true`."
        ),
    )

    assert result.startswith("OK: Micro-review submitted: ok")


def test_submit_micro_review_rejects_nonportable_shell_notes_feedback(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        notes=(
            "Required verification: run `timeout 5 npm run dev || true` and "
            "then use grep -q on the output."
        ),
    )

    assert result.startswith("ERROR:")
    assert "non-portable Unix shell" in result


def test_subtask_completion_accepts_exit_code_success_clause(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:execute",
                "tool": "shell",
                "args": {
                    "workspace_id": "civilization",
                    "command": ["pytest", "tests/test_models.py", "-v"],
                },
                "result_preview": json.dumps(
                    {
                        "command": ["pytest", "tests/test_models.py", "-v"],
                        "exit_code": 0,
                        "output": "================ 22 passed in 0.13s ================",
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:execute"
    current_phase = {
        "id": "execute",
        "subtasks": [
            {
                "id": "1.1",
                "status": "pending",
                "success_test": {
                    "value": "pytest tests/test_models.py -v exits with code 0"
                },
            }
        ],
    }

    result = _phase_subtask_completion_issue(
        ctx,
        current_phase=current_phase,
        subtask_id="1.1",
    )

    assert result == ""


def test_subtask_completion_matches_python_m_pytest_equivalent(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:execute",
                "tool": "shell",
                "args": {
                    "workspace_id": "civilization",
                    "command": [
                        str(tmp_path / ".venv" / "Scripts" / "python.exe"),
                        "-m",
                        "pytest",
                        "tests/test_models.py",
                        "-v",
                    ],
                },
                "result_preview": json.dumps(
                    {
                        "command": [
                            str(tmp_path / ".venv" / "Scripts" / "python.exe"),
                            "-m",
                            "pytest",
                            "tests/test_models.py",
                            "-v",
                        ],
                        "exit_code": 0,
                        "output": "================ 22 passed in 0.13s ================",
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:execute"
    current_phase = {
        "id": "execute",
        "subtasks": [
            {
                "id": "1.1",
                "status": "pending",
                "success_test": {
                    "value": "pytest tests/test_models.py -v exits with code 0"
                },
            }
        ],
    }

    result = _phase_subtask_completion_issue(
        ctx,
        current_phase=current_phase,
        subtask_id="1.1",
    )

    assert result == ""


def test_subtask_completion_accepts_command_label_success_test(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:execute",
                "tool": "shell",
                "args": {
                    "workspace_id": "civilization",
                    "command": "python -m pytest "
                    "tests/test_setup.py::test_project_structure "
                    "tests/test_setup.py::test_package_imports -v",
                },
                "result_preview": json.dumps(
                    {
                        "command": [
                            str(tmp_path / ".venv" / "Scripts" / "python.exe"),
                            "-m",
                            "pytest",
                            "tests/test_setup.py::test_project_structure",
                            "tests/test_setup.py::test_package_imports",
                            "-v",
                        ],
                        "exit_code": 0,
                        "output": (
                            "tests/test_setup.py::test_project_structure PASSED\n"
                            "tests/test_setup.py::test_package_imports PASSED\n"
                            "================ 2 passed in 1.86s ================"
                        ),
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:execute"
    current_phase = {
        "id": "execute",
        "subtasks": [
            {
                "id": "subtask_001",
                "status": "pending",
                "success_test": {
                    "value": (
                        "Command: python -m pytest "
                        "tests/test_setup.py::test_project_structure "
                        "tests/test_setup.py::test_package_imports -v"
                    )
                },
            }
        ],
    }

    result = _phase_subtask_completion_issue(
        ctx,
        current_phase=current_phase,
        subtask_id="subtask_001",
    )

    assert result == ""


def test_subtask_completion_allows_future_gmas_verify_failure(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    success_test = "python -m pytest tests/test_game_engine.py -q"
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_651c6791:execute",
                "tool": "shell",
                "args": {
                    "argv": [
                        "python",
                        "-m",
                        "pytest",
                        "tests/test_game_engine.py",
                        "-q",
                    ],
                },
                "result_preview": json.dumps(
                    {"exit_code": 0, "output": "39 passed in 0.14s"}
                ),
            }
        )
        + "\n"
        + json.dumps(
            {
                "task_id": "phase_web_651c6791:execute",
                "tool": "run_workspace_verify",
                "args": {"workspace_id": "civilization"},
                "result_preview": json.dumps(
                    {
                        "passed": False,
                        "failed_step_count": 1,
                        "summary": (
                            "Verification: **FAIL** (7/8 required steps passed)\n"
                            "- [required] `pytest:tests` (shell) -> ok exit=0\n"
                            "- [required] `skill_runtime:multi_agent_gmas_app_imports` "
                            "(import_check) -> failed\n"
                            "  missing GMAS import for future agent integration"
                        ),
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_651c6791:execute"
    current_phase = {
        "id": "execute",
        "subtasks": [
            {
                "id": "st_004",
                "title": "Build game engine",
                "goal": "Core game loop: process city growth and resources",
                "status": "pending",
                "files_to_create": [
                    "src/civsim/game/engine.py",
                    "tests/test_game_engine.py",
                ],
                "success_test": {"kind": "cmd", "value": success_test},
            },
            {
                "id": "st_005",
                "title": "Create GMAS game tools",
                "goal": "Build custom GMAS tools for LLM bot decisions",
                "status": "pending",
                "files_to_create": ["src/civsim/ai/tools/game_tools.py"],
                "success_test": "python -m pytest tests/test_game_tools.py -q",
            },
        ],
    }

    result = _phase_subtask_completion_issue(
        ctx,
        current_phase=current_phase,
        subtask_id="st_004",
    )

    assert result == ""


def test_subtask_completion_blocks_relevant_verify_failure_in_touched_file(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    success_test = "python -m pytest tests/test_game_engine.py -q"
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_651c6791:execute",
                "tool": "shell",
                "args": {
                    "argv": [
                        "python",
                        "-m",
                        "pytest",
                        "tests/test_game_engine.py",
                        "-q",
                    ],
                },
                "result_preview": json.dumps(
                    {"exit_code": 0, "output": "39 passed in 0.14s"}
                ),
            }
        )
        + "\n"
        + json.dumps(
            {
                "task_id": "phase_web_651c6791:execute",
                "tool": "run_workspace_verify",
                "args": {"workspace_id": "civilization"},
                "result_preview": json.dumps(
                    {
                        "passed": False,
                        "failed_step_count": 1,
                        "summary": (
                            "Verification: **FAIL** (6/7 required steps passed)\n"
                            "- [required] `source_policy:mock_scaffold_scan` "
                            "(source_policy) -> failed\n"
                            "  tests/conftest.py: mock helper"
                        ),
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_651c6791:execute"
    current_phase = {
        "id": "execute",
        "subtasks": [
            {
                "id": "st_004",
                "title": "Build game engine",
                "goal": "Core game loop",
                "status": "pending",
                "files_to_create": [
                    "src/civsim/game/engine.py",
                    "tests/test_game_engine.py",
                ],
                "contract_migration_files": ["tests/conftest.py"],
                "success_test": {"kind": "cmd", "value": success_test},
            }
        ],
    }

    result = _phase_subtask_completion_issue(
        ctx,
        current_phase=current_phase,
        subtask_id="st_004",
    )

    assert result.startswith("ERROR: mark_subtask_complete rejected")
    assert "relevant to this subtask" in result


def test_mark_subtask_complete_rejects_captured_ouroboros_only_alias_memory(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir()
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "phase_plan:foundation_docs",
                "workspace_id": "civilization",
                "run_id": "phase_web_40737336",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "llm_agent_system",
                                "status": "pending",
                                "success_test": (
                                    "python -m pytest tests/test_agents.py -v"
                                ),
                            }
                        ],
                    }
                ],
                "edits_log": [],
            }
        ),
        encoding="utf-8",
    )
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_40737336:execute",
                "tool": "shell",
                "result_preview": json.dumps(
                    {
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_agents.py",
                            "-v",
                        ],
                        "exit_code": 0,
                        "output": "11 passed in 1.93s",
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_40737336:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {
        "phase_node": {"id": "execute", "manifest_id": "execute"}
    }
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _mark_subtask_complete(
        ctx,
        subtask_id="llm_agent_system",
        summary=(
            "Created agents.py with get_llm_runtime_config() supporting "
            "OUROBOROS_LLM_* variables exclusively."
        ),
        evidence=[
            (
                "All 11 tests in tests/test_agents.py passed, including "
                "test_get_llm_runtime_config_fallback_to_legacy."
            )
        ],
    )

    assert result.startswith("ERROR: mark_subtask_complete rejected")
    assert "LLM_API_KEY" in result
    updated = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    assert updated["nodes"][0]["subtasks"][0]["status"] == "pending"
    assert not (state / "phase_control_signals.jsonl").exists()


def test_mark_subtask_complete_accepts_supported_llm_alias_memory(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir()
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "phase_plan:foundation_docs",
                "workspace_id": "civilization",
                "run_id": "phase_web_40737336",
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [
                            {
                                "id": "llm_agent_system",
                                "status": "pending",
                                "success_test": (
                                    "python -m pytest tests/test_agents.py -v"
                                ),
                            }
                        ],
                    }
                ],
                "edits_log": [],
            }
        ),
        encoding="utf-8",
    )
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_40737336:execute",
                "tool": "shell",
                "result_preview": json.dumps(
                    {
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_agents.py",
                            "-v",
                        ],
                        "exit_code": 0,
                        "output": "11 passed in 1.93s",
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_40737336:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {
        "phase_node": {"id": "execute", "manifest_id": "execute"}
    }
    ctx.loop_state_view = {"phase_label": "linear"}
    from umbrella.contracts.hashing import hash_value, workspace_hash
    from umbrella.enforcement.ledger import append_supervisor_ledger_event

    workspace = tmp_path / "workspaces" / "civilization"
    ws_hash = workspace_hash(workspace)
    report_hash = hash_value(
        {
            "subtask_id": "llm_agent_system",
            "passed": True,
            "exit_code": 0,
            "proof_kind": "pytest",
            "workspace_hash": ws_hash,
            "diff_hash": "",
            "skip_only": False,
        }
    )
    ledger_result = {
        "report_hash": report_hash,
        "passed": True,
        "workspace_hash": ws_hash,
        "diff_hash": "",
    }
    proof_ledger = append_supervisor_ledger_event(
        repo_root=tmp_path,
        workspace_id="civilization",
        actor="verifier",
        phase="execute",
        tool="run_subtask_proof",
        args={"subtask_id": "llm_agent_system"},
        result=ledger_result,
    )
    proof_ref = {
        "ref_type": "ledger_event",
        "ref_id": proof_ledger.event_id,
        "hash": proof_ledger.event_hash,
        "produced_by": "verifier",
        "phase": "execute",
        "subtask_id": "llm_agent_system",
    }

    result = _mark_subtask_complete(
        ctx,
        subtask_id="llm_agent_system",
        completion_contract={
            "subtask_id": "llm_agent_system",
            "status": "done",
            "changed_files": [],
            "completed_claims": [
                {
                    "claim_id": "llm_agent_system.proof",
                    "text": "tests/test_agents.py passed.",
                    "proof_refs": [proof_ref],
                }
            ],
            "evidence_refs": [proof_ref],
            "verification_report": {
                "report_id": proof_ledger.event_id,
                "report_hash": report_hash,
                "workspace_hash": ws_hash,
                "diff_hash": "",
                "produced_after_event_id": "",
                "verifier_id": "run_subtask_proof",
                "passed": True,
                "ledger_hash": proof_ledger.event_hash,
            },
            "notes": "Verifier-backed completion.",
        },
        summary=(
            "Created get_llm_runtime_config() supporting "
            "LLM_API_KEY, "
            "LLM_BASE_URL, and "
            "LLM_MODEL."
        ),
        evidence=["All 11 tests in tests/test_agents.py passed."],
    )

    assert result == "OK: Subtask 'llm_agent_system' marked complete"
    updated = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    assert updated["nodes"][0]["subtasks"][0]["status"] == "done"


def test_submit_micro_review_allows_protective_no_fallback_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add environment validation and tests that reject hardcoded "
                "fallback defaults for LLM configuration."
            )
        ],
        notes="No fallback to hardcoded rules is allowed.",
    )

    assert result.startswith("OK: Micro-review submitted: revise")


def test_submit_micro_review_allows_never_fallback_static_decisions_notes(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 1779172087.9174476,
                "run_id": "phase_web_32c354c9",
                "notes": "GMAS FastAPI React architecture with real LLM runtime.",
            }
        ),
        encoding="utf-8",
    )
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-19T06:28:34+00:00",
                "task_id": "phase_web_32c354c9:research_review",
                "tool": "read_file",
                "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
                "result_preview": "{}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_32c354c9:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes=(
            "LLM credentials must use Umbrella env vars with explicit error "
            "on missing - never fallback to static decisions."
        ),
    )

    assert result.startswith("OK: Micro-review submitted: ok")


def test_research_review_rejects_summary_with_unbacked_source_labels(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 1779277577.3371508,
                "run_id": "phase_web_779c6ad4",
                "architecture_id": "arch-civilization-fastapi-gmas-react-v1",
                "findings_ids": [
                    "finding-fastapi",
                    "finding-webgame",
                    "finding-llm",
                ],
                "notes": (
                    "Architecture uses public LLM_API_KEY, LLM_BASE_URL, and "
                    "LLM_MODEL.\n"
                    "**Source**: deep_search:fastapi react full stack tutorial\n"
                    "**Source**: deep_search:github isadri transcendence\n"
                    "**Source**: deep_search:GMAS early_stop_example.py"
                ),
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "phase_web_779c6ad4:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-fastapi",
                    "kind": "research_finding",
                    "source_path": "deep_search:web game python fastapi typescript react tutorial",
                }
            ),
        },
        {
            "task_id": "phase_web_779c6ad4:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-webgame",
                    "kind": "research_finding",
                    "source_path": "deep_search:github isadri transcendence civilization strategy game",
                }
            ),
        },
        {
            "task_id": "phase_web_779c6ad4:research",
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-llm",
                    "kind": "research_finding",
                    "source_path": "deep_search:LLM-powered game bot economy diplomacy framework",
                }
            ),
        },
        {
            "task_id": "phase_web_779c6ad4:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_779c6ad4:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research has viable architecture and should proceed.",
    )

    assert result.startswith("ERROR:")
    assert "source label(s) not backed by the cited accepted findings" in result
    assert "Research review cannot accept ok" in result


def test_submit_micro_review_allows_runtime_env_alias_fallback_notes(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "run-1",
                "plan_id": "safe-plan",
                "plan": {
                    "subtasks": [
                        {
                            "id": "runtime",
                            "title": "Runtime aliases",
                            "goal": "Use inherited LLM runtime aliases.",
                                "files_to_create": [
                                    "src/demo/runtime.py",
                                    "tests/test_runtime.py",
                                ],
                            "success_test": "python -m pytest tests/test_runtime.py -q",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:plan_review",
                "tool": "read_file",
                "args": {
                    "file_path": ".memory/drive/state/phase_plan_submitted_latest.json"
                },
                "result_preview": "{}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes=(
            "Plan approved. LLM config supports OUROBOROS_LLM_* with "
            "fallback to LLM_* env vars, then explicit errors for missing "
            "credentials; no heuristic fallbacks are allowed."
        ),
    )

    assert result.startswith("OK: Micro-review submitted: ok")


def test_submit_micro_review_allows_runtime_env_alias_fallback_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Support runtime credential alias fallback from "
                "OUROBOROS_LLM_API_KEY/OUROBOROS_LLM_BASE_URL/"
                "OUROBOROS_MODEL to LLM_API_KEY/LLM_BASE_URL/LLM_MODEL, "
                "then raise an explicit missing configuration error."
            )
        ],
        notes="This is env alias resolution, not replacement AI behavior.",
    )

    assert result.startswith("OK: Micro-review submitted: revise")


def test_submit_micro_review_allows_parenthetical_runtime_env_alias_fallback(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Keep runtime env priority as OUROBOROS_LLM_* first, then "
                "LLM_* (fallback); missing credentials must raise a clear "
                "configuration error and never create replacement bot actions."
            )
        ],
    )

    assert result.startswith("OK: Micro-review submitted: revise")


def test_submit_micro_review_allows_protective_no_mock_llm_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add an integration proof that rejects mock/fake LLM paths and "
                "requires inherited runtime env for GMAS bot decisions."
            )
        ],
        notes="No mock LLM behavior should be accepted as the core proof.",
    )

    assert result.startswith("OK: Micro-review submitted: revise")


def test_submit_micro_review_allows_captured_prohibition_on_mock_fake_decisions(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 1779220016.0,
                "run_id": "phase_web_fe6f7d1b",
                "finding_ids": ["finding-1"],
                "architecture_id": "architecture-civilization-gmas-web-v1",
            }
        ),
        encoding="utf-8",
    )
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-19T19:46:58+00:00",
                "task_id": "phase_web_fe6f7d1b:research_review",
                "tool": "read_file",
                "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
                "result_preview": "{}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_fe6f7d1b:research_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes=(
            "Research is sufficient with prohibition on mock/fake decisions "
            "and real inherited LLM runtime required for GMAS bot behavior."
        ),
    )

    assert result.startswith("OK: Micro-review submitted: ok")


def test_submit_micro_review_allows_captured_no_openai_no_gpt_review_checklist(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_fe6f7d1b:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "No required OPENAI_API_KEY, no gpt-* model defaults, and no "
                "OpenAI-only recommendation. Require "
                "LLM_API_KEY, "
                "LLM_BASE_URL, and "
                "LLM_MODEL instead."
            )
        ],
        notes="This is provider-neutral runtime policy, not provider guidance.",
    )

    assert result.startswith("OK: Micro-review submitted: revise")


def test_submit_micro_review_allows_reject_gpt_default_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add tests that reject gpt-4o-mini defaults and require the "
                "configured runtime model from LLM_MODEL."
            )
        ],
    )

    assert result.startswith("OK: Micro-review submitted: revise")


def test_submit_micro_review_provider_model_error_uses_task_phase_when_label_linear(
    tmp_path,
):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "phase_web_fe6f7d1b:plan_review"
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add docs/LLM_CONFIG.md and specify recommended models "
                "gpt-4o-mini for economy diplomacy and gpt-4o for critical decisions."
            )
        ],
        notes="Plan docs need model guidance.",
    )

    assert result.startswith("ERROR:")
    assert "(phase: plan_review)" in result


def test_submit_micro_review_allows_retry_pause_error_revision(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            (
                "Add LLM failure handling note: retry failed API calls, pause "
                "the affected bot turn, and surface explicit configuration or "
                "runtime errors."
            )
        ],
        notes="No replacement decision path is requested.",
    )

    assert result.startswith("OK: Micro-review submitted: revise")


def test_loop_back_rejects_hardcoded_fallback_reason(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "workspace_id": "civilization",
                "nodes": [{"id": "plan", "status": "done"}],
                "version": 0,
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:plan_review"
    ctx.loop_state_view = {"phase_label": "plan_review"}

    result = _loop_back_to(
        ctx,
        phase="plan",
        reason="Add fallback to hardcoded localhost defaults for missing LLM config.",
    )

    assert result.startswith("ERROR:")
    assert "cannot request hardcoded/static/default fallback" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_research_review_revise_rejects_external_files_cannot_read_loopback(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "External files cannot be read via read_file because they are outside workspace root.",
            "The workspace is intentionally empty and findings are sufficient for planning.",
        ],
        notes="GMAS was validated via get_gmas_context; ready for planning.",
    )

    assert result.startswith("ERROR:")
    assert "verdict=ok" in result


@pytest.mark.xfail(reason="semantic regex gates removed; use typed micro_review/declaration", strict=False)
def test_research_review_revise_rejects_implementation_owned_details(tmp_path):
    drive = tmp_path / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "Extract full code snippets for tool implementations.",
            "Document exact Pydantic model definitions and WebSocket message protocols.",
        ],
        notes="The research has a viable architecture but needs implementation details.",
    )

    assert result.startswith("ERROR:")
    assert "plan/execute/verify" in result


def test_research_review_revise_rejects_stale_claim_in_revisions(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "Resolve import failure: cannot import name "
            "get_game_state_tool from backend.bots.bot_tools."
        ],
        notes="Review needs one concrete revision.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_research_review_revise_rejects_not_available_for_import_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "Fix bot_tools.py to export get_game_state_tool function "
            "(defined but not available for import)."
        ],
        notes="Review asks for an import/export fix.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_research_review_revise_rejects_existing_file_missing_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n    pass\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "main.py imports from game_engine but no game_engine.py file "
            "exists in workspace."
        ],
        notes="Review asks for a missing file fix.",
    )

    assert result.startswith("ERROR:")
    assert "missing/nonexistent" in result


def test_research_review_revise_rejects_constructor_param_wording(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        pass\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "Fix the GameEngine constructor integration issue "
            "(missing ai_controller parameter)."
        ],
        notes="Review asks for a constructor fix.",
    )

    assert result.startswith("ERROR:")
    assert "ai_controller" in result


def test_research_review_revise_rejects_missing_param_in_target_wording(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        pass\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "HTTP 500 on /api/game/create due to missing 'ai_controller' "
            "parameter in GameEngine.__init__."
        ],
        notes="Review carries a runtime blocker.",
    )

    assert result.startswith("ERROR:")
    assert "ai_controller" in result


def test_research_review_revise_rejects_direct_missing_param_wording(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (workspace / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        self.ai_controller = ai_controller\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "API endpoint returns 500 on game creation "
            "(GameEngine missing ai_controller argument)."
        ],
        notes="Review carries a runtime blocker.",
    )

    assert result.startswith("ERROR:")
    assert "ai_controller" in result


def test_research_review_revise_rejects_missing_import_label(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=["Missing import: get_game_state_tool from backend.bots.bot_tools"],
        notes="Review asks for an import fix.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_research_review_revise_rejects_ambiguous_basename_missing_symbol(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    backend_tools = workspace / "backend" / "bots"
    backend_tools.mkdir(parents=True)
    (backend_tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    core_tools = workspace / "game_core"
    core_tools.mkdir(parents=True)
    (core_tools / "bot_tools.py").write_text(
        "def other_tool():\n    return 'other'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "pytest imports fail: missing 'get_game_state_tool' from bot_tools.py."
        ],
        notes="Review carries a current import blocker.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_research_review_revise_rejects_negated_stale_import_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "The import error cannot import name 'get_game_state_tool' is "
            "NOT due to stale verification; add exports."
        ],
        notes="Review carries a current import blocker.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_research_review_revise_rejects_symbol_is_missing_claim(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    tools = workspace / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        revisions=[
            "pytest collection fails because "
            "`backend.bots.bot_tools.get_game_state_tool` is missing."
        ],
        notes="Review carries a current import blocker.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_research_review_ok_requires_referenced_files_read(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "main.py").write_text("def create_game():\n    pass\n", encoding="utf-8")
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "notes": "main.py wires /api/game/create.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research is verified.",
    )

    assert result.startswith("ERROR:")
    assert "main.py" in result


def test_research_review_ok_accepts_after_summary_and_source_reads(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "main.py").write_text("def create_game():\n    pass\n", encoding="utf-8")
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "notes": "main.py wires /api/game/create.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": "main.py"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research is verified against current files.",
    )

    assert result.startswith("OK: Micro-review submitted: ok")


def test_research_review_ok_counts_workspace_charter_as_file_read(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "workspace.toml").write_text("[skills]\n", encoding="utf-8")
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "notes": "workspace.toml records the selected skill surface.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research_review",
            "tool": "read_workspace_charter",
            "args": {"workspace_id": "mini_game"},
            "result_preview": json.dumps(
                {
                    "workspace_id": "mini_game",
                    "files": {"TASK_MAIN.md": "Build it", "workspace.toml": "[skills]\n"},
                }
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research is verified against the charter.",
    )

    assert result.startswith("OK: Micro-review submitted: ok")


def test_research_review_ok_accepts_read_tsx_file(tmp_path):
    workspace = tmp_path / "workspaces" / "mini_game"
    drive = workspace / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (workspace / "frontend" / "src").mkdir(parents=True)
    (workspace / "frontend" / "src" / "App.tsx").write_text(
        "export default function App() { return null; }\n",
        encoding="utf-8",
    )
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "notes": "frontend/src/App.tsx renders the main game UI.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
        {
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": "frontend/src/App.tsx"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Verified frontend/src/App.tsx in this phase.",
    )

    assert result.startswith("OK: Micro-review submitted: ok")


def test_research_review_ok_rejects_stale_missing_symbol_summary(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "run-1",
                "notes": "backend/bots/bot_tools.py missing get_game_state_tool export.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
        {
            "ts": "1970-01-01T00:02:01Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": json.dumps(
                {"content": "def get_game_state_tool(game_state, player_id):\n    pass\n"}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research is verified against current files.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_research_review_ok_rejects_resolve_import_symbol_summary(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "run-1",
                "notes": "Next steps: resolve bot_tools import get_game_state_tool.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
        {
            "ts": "1970-01-01T00:02:01Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": "backend/bots/bot_tools.py"},
            "result_preview": json.dumps(
                {"content": "def get_game_state_tool(game_state, player_id):\n    pass\n"}
            ),
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research is verified against current files.",
    )

    assert result.startswith("ERROR:")
    assert "contains that symbol" in result


def test_research_review_ok_ignores_reads_before_latest_summary(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "run-1",
                "notes": "main.py wires /api/game/create.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:01:00Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
        {
            "ts": "1970-01-01T00:01:01Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": "main.py"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research is verified against stale reads.",
    )

    assert result.startswith("ERROR:")
    assert "research_summary_latest.json" in result


def test_research_review_ok_accepts_reads_after_latest_summary(tmp_path):
    drive = tmp_path / "workspaces" / "mini_game" / ".memory" / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    (drive / "state").mkdir()
    (drive / "state" / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "created_at": 100.0,
                "run_id": "run-1",
                "notes": "main.py wires /api/game/create.",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "1970-01-01T00:02:00Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": ".memory/drive/state/research_summary_latest.json"},
            "result_preview": "{}",
        },
        {
            "ts": "1970-01-01T00:02:01Z",
            "task_id": "run-1:research_review",
            "tool": "read_file",
            "args": {"file_path": "main.py"},
            "result_preview": "{}",
        },
    ]
    (logs / "tools.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.task_id = "run-1:research_review"
    ctx.loop_state_view = {"phase_label": "research_review"}

    result = _submit_micro_review(
        ctx,
        verdict="ok",
        notes="Research is verified against fresh reads.",
    )

    assert result.startswith("OK: Micro-review submitted: ok")

