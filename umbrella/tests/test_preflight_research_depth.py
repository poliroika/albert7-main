"""Preflight research_depth is the source of truth for research phase depth."""

import json
from pathlib import Path

from umbrella.orchestrator.preflight_depth import read_preflight_research_depth
from umbrella.orchestrator.worker import build_phase_task
from umbrella.memory.palace.facade import MemPalace
from umbrella.phases.base import PhaseNode


def test_submit_preflight_report_requires_depth_when_ready() -> None:
    status = "ready"
    depth = ""
    assert status == "ready"
    assert depth not in {"none", "light", "full"}


def test_submit_preflight_report_rejects_invalid_depth() -> None:
    depth = "heavy"
    assert depth not in {"none", "light", "full"}


def test_read_preflight_research_depth_from_signals(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    row = {
        "kind": "submit_preflight_report",
        "created_at": 10.0,
        "run_id": "run-preflight-depth",
        "payload": {"status": "ready", "research_depth": "full", "blockers": []},
    }
    (state / "phase_control_signals.jsonl").write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )
    assert read_preflight_research_depth(drive, run_id="run-preflight-depth") == "full"


def test_research_depth_from_preflight_signal(test_workspace_copy) -> None:
    repo, ws = test_workspace_copy
    from pathlib import Path

    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(
        Path(__file__).parent.parent / "phases" / "manifests" / "research.yaml"
    )
    drive = repo / "workspaces" / ws / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    row = {
        "kind": "submit_preflight_report",
        "created_at": 10.0,
        "run_id": "run-research-full",
        "payload": {"status": "ready", "research_depth": "full", "blockers": []},
    }
    (state / "phase_control_signals.jsonl").write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )
    phase_node = PhaseNode(id="research-1", manifest_id="research")
    palace = MemPalace(repo, ws)
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id=ws,
            run_id="run-research-full",
            palace=palace,
            repo_root=repo,
            drive_root=drive,
        )
    finally:
        palace.close()

    overlays = task.get("context_overlays") or {}
    assert overlays.get("research_depth") == "full"
    assert "Umbrella selected `full`" in str(task.get("input") or "")
    allowed = set(task.get("tool_filter", {}).get("allow") or [])
    assert {"get_gmas_context", "search_gmas_knowledge"} <= allowed


def test_research_depth_defaults_light_without_preflight(test_workspace_copy) -> None:
    repo, ws = test_workspace_copy
    from pathlib import Path

    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(
        Path(__file__).parent.parent / "phases" / "manifests" / "research.yaml"
    )
    phase_node = PhaseNode(id="research-1", manifest_id="research")
    palace = MemPalace(repo, ws)
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id=ws,
            run_id="run-research-light",
            palace=palace,
            repo_root=repo,
        )
    finally:
        palace.close()

    overlays = task.get("context_overlays") or {}
    assert overlays.get("research_depth") == "light"
    prompt = str(task.get("input") or "")
    assert "Call `palace_add` at least 1 time(s)" in prompt
    allowed = set(task.get("tool_filter", {}).get("allow") or [])
    assert "get_gmas_context" not in allowed
    assert "search_gmas_knowledge" not in allowed
    assert "gmas-overview" not in set(overlays.get("effective_allowed_skills") or [])
    prerequisites = task.get("tool_filter", {}).get("completion_prerequisites", {})
    palace_writes = prerequisites.get("palace_writes") or []
    assert palace_writes
    assert palace_writes[0]["n"] == 1


def test_light_depth_plan_retry_uses_compact_contract_without_gmas_tools(
    test_workspace_copy,
) -> None:
    repo, ws = test_workspace_copy
    from pathlib import Path

    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(
        Path(__file__).parent.parent / "phases" / "manifests" / "plan.yaml"
    )
    drive = repo / "workspaces" / ws / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    row = {
        "kind": "submit_preflight_report",
        "created_at": 10.0,
        "run_id": "run-plan-light",
        "payload": {"status": "ready", "research_depth": "light", "blockers": []},
    }
    (state / "phase_control_signals.jsonl").write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )
    phase_node = PhaseNode(
        id="plan",
        manifest_id="plan",
        overlay={"retry_reason": "Fix proof scope only."},
    )
    palace = MemPalace(repo, ws)
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id=ws,
            run_id="run-plan-light",
            palace=palace,
            repo_root=repo,
            drive_root=drive,
        )
    finally:
        palace.close()

    overlays = task.get("context_overlays") or {}
    allowed = set(task.get("tool_filter", {}).get("allow") or [])
    skills = set(overlays.get("effective_allowed_skills") or [])
    prompt = str(task.get("input") or "")
    assert overlays.get("research_depth") == "light"
    assert not (allowed & {"get_gmas_context", "search_gmas_knowledge"})
    assert not (skills & {"gmas-overview", "gmas-pattern-author"})
    assert "Active retry/revision contract" in prompt
    assert "Do not read raw phase-control ledgers" in prompt


def test_research_depth_none_removes_finding_floor(test_workspace_copy) -> None:
    repo, ws = test_workspace_copy
    from pathlib import Path

    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(
        Path(__file__).parent.parent / "phases" / "manifests" / "research.yaml"
    )
    phase_node = PhaseNode(
        id="research-1",
        manifest_id="research",
        overlay={"research_depth": "none"},
    )
    palace = MemPalace(repo, ws)
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id=ws,
            run_id="run-research-none",
            palace=palace,
            repo_root=repo,
        )
    finally:
        palace.close()

    prompt = str(task.get("input") or "")
    assert "Required palace writes before completion" not in prompt
    prerequisites = task.get("tool_filter", {}).get("completion_prerequisites", {})
    assert prerequisites.get("palace_writes") == []
