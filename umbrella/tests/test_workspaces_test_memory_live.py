"""Fast integration tests using committed ``workspaces/test`` fixture.

Delegates core contract checks to Memory Scenario Harness helpers. Exercises
``build_phase_task``, drive artifacts, ``palace_add``, ``save_umbrella_memory``,
and Ouroboros dedup against a realistic workspace layout. No LLM, no full phase runner.

Set ``UMBRELLA_TEST_HARNESS_VERBOSE=1`` to print injection report / audit on failure.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from umbrella.evals.memory_scenarios.assertions import (
    assert_memory_injection_contract,
    assert_single_always_loaded_block,
    included_bkb_ids_from_audit,
    skipped_bkb_ids,
)
from umbrella.evals.memory_scenarios.fake_ouroboros import run_ouroboros_dedup_check
from umbrella.evals.memory_scenarios.fixtures import drive_root
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.paths import workspace_memory_root
from umbrella.orchestrator.worker import build_phase_task
from umbrella.phases.base import PhaseNode
from umbrella.phases.loader import load_manifest
from umbrella.evals.memory_scenarios.fixtures import manifest_path

pytestmark = [pytest.mark.workspace_live, pytest.mark.memory_live]

PHASE_MATRIX = (
    pytest.param(
        "research",
        True,
        {"test_candidate_only"},
        id="research-active-bkb",
    ),
    pytest.param(
        "verify",
        False,
        {"test_candidate_only", "test_active_provenance"},
        id="verify-no-research-bkb",
    ),
    pytest.param(
        "plan",
        False,
        {"test_candidate_only", "test_active_provenance"},
        id="plan-no-research-bkb",
    ),
    pytest.param(
        "execute",
        False,
        {"test_candidate_only", "test_active_provenance"},
        id="execute-no-research-bkb",
    ),
)


def _harness_verbose() -> bool:
    return str(os.environ.get("UMBRELLA_TEST_HARNESS_VERBOSE", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _debug_dump(label: str, payload: Any) -> None:
    if not _harness_verbose():
        return
    print(f"\n--- harness {label} ---\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n")


def _load_drive_artifacts(drive: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle_path = drive / "state" / "llm_input_bundle_latest.json"
    report_path = drive / "state" / "memory_injection_report_latest.json"
    assert bundle_path.is_file(), f"missing bundle: {bundle_path}"
    assert report_path.is_file(), f"missing report: {report_path}"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return bundle, report


def _included_reasons(report: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in report.get("included") or []:
        if isinstance(row, dict) and row.get("id"):
            out[str(row["id"])] = str(row.get("reason") or "")
    return out


def _build_task(
    repo: Path,
    workspace_id: str,
    *,
    manifest_name: str,
    phase_id: str,
    run_id: str = "run-test-1",
    drive_root_path: Path | None = None,
) -> dict:
    manifest = load_manifest(manifest_path(manifest_name))
    phase_node = PhaseNode(id=phase_id, manifest_id=manifest_name, status="running")
    palace = MemPalace(repo, workspace_id)
    try:
        return build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id=workspace_id,
            run_id=run_id,
            palace=palace,
            repo_root=repo,
            drive_root=drive_root_path,
        )
    finally:
        palace.close()


def test_live_smoke_scenario_harness(tmp_path) -> None:
    from umbrella.evals.memory_scenarios.runner import run_scenario_by_id

    result = run_scenario_by_id("00_smoke_phase_matrix", report_root=tmp_path / "smoke")
    assert result.ok, result.summary_text


def test_live_build_phase_task_injection_contract_research_verify(
    test_workspace_copy,
) -> None:
    repo, ws = test_workspace_copy
    for manifest_name in ("research", "verify"):
        task = _build_task(repo, ws, manifest_name=manifest_name, phase_id=manifest_name)
        _, errs = assert_memory_injection_contract(task)
        assert not errs
        prompt = str(task.get("input") or "")
        assert not assert_single_always_loaded_block(prompt)
        if "Supplemental" in prompt:
            assert "NON-DIRECTIVE" in prompt


@pytest.mark.parametrize(
    ("phase_id", "expect_provenance_rule", "expect_skipped_ids"),
    PHASE_MATRIX,
)
def test_live_phase_matrix_bkb_and_injection_audit(
    test_workspace_copy,
    phase_id: str,
    expect_provenance_rule: bool,
    expect_skipped_ids: set[str],
) -> None:
    repo, ws = test_workspace_copy
    drive = drive_root(repo, ws)
    task = _build_task(
        repo,
        ws,
        manifest_name=phase_id,
        phase_id=phase_id,
        drive_root_path=drive,
    )
    contract, _ = assert_memory_injection_contract(task)
    prompt = str(task.get("input") or "")
    assert not assert_single_always_loaded_block(prompt)

    proactive = (task.get("context_overlays") or {}).get("proactive_memory") or {}
    audit = proactive.get("injection_audit") or proactive.get("telemetry", {}).get(
        "injection_audit"
    )
    assert isinstance(audit, dict), "proactive_memory missing injection_audit"

    bundle, report = _load_drive_artifacts(drive)
    _debug_dump(f"{phase_id}-report", report)
    _debug_dump(f"{phase_id}-audit", audit)

    assert report.get("schema_version") == "1"
    assert report.get("phase_id") == phase_id
    assert report.get("proactive_overlay_hash") == contract.get("proactive_overlay_hash")

    skipped = skipped_bkb_ids(report)
    assert expect_skipped_ids <= skipped

    included_ids = included_bkb_ids_from_audit(task)
    if expect_provenance_rule:
        assert "test_active_provenance" in included_ids
        assert "provenance" in prompt.lower() or "source_id" in prompt.lower()
    else:
        assert "test_active_provenance" not in included_ids
        assert "inject me please" not in prompt

    audit_skipped = {
        str(row.get("id"))
        for row in (audit.get("skipped_bkb") or [])
        if isinstance(row, dict) and row.get("id")
    }
    assert audit_skipped == skipped

    memory_items = bundle.get("memory_items") or []
    assert memory_items
    directive_items = [m for m in memory_items if m.get("directive")]
    assert directive_items


def test_live_injection_report_included_reasons(test_workspace_copy) -> None:
    repo, ws = test_workspace_copy
    drive = drive_root(repo, ws)
    _build_task(repo, ws, manifest_name="research", phase_id="research", drive_root_path=drive)
    _bundle, report = _load_drive_artifacts(drive)
    reasons = _included_reasons(report)
    assert reasons
    assert all(reason in {"directive_proactive", "supplemental_recall"} for reason in reasons.values())
    assert any(reason == "directive_proactive" for reason in reasons.values())


def test_live_workspace_charter_and_lessons_in_overlay(test_workspace_copy) -> None:
    repo, ws = test_workspace_copy
    task = _build_task(repo, ws, manifest_name="execute", phase_id="execute")
    prompt = str(task.get("input") or "").lower()
    assert "evidence" in prompt or "charter" in prompt or "workspace" in prompt
    assert "deploy without running tests" in prompt or "forbidden" in prompt


def test_live_drive_artifacts_and_injection_report(test_workspace_copy) -> None:
    repo, ws = test_workspace_copy
    drive = drive_root(repo, ws)
    task = _build_task(
        repo,
        ws,
        manifest_name="research",
        phase_id="research",
        drive_root_path=drive,
    )
    contract, _ = assert_memory_injection_contract(task)
    bundle, report = _load_drive_artifacts(drive)

    assert report.get("proactive_overlay_hash") == contract.get("proactive_overlay_hash")
    assert report.get("llm_input_bundle_hash") == (
        task.get("context_overlays") or {}
    ).get("llm_input_bundle_hash")
    assert isinstance(report.get("included"), list)
    per_phase = drive / "state" / "llm_input_bundle_research.json"
    assert per_phase.is_file()

    skipped = report.get("skipped") or []
    assert any(
        row.get("id") == "test_candidate_only" and row.get("reason") == "status_candidate"
        for row in skipped
        if isinstance(row, dict)
    )
    assert bundle.get("phase_id") == "research"
    assert bundle.get("workspace_id") == ws


def test_live_palace_add_single_canonical_write(test_workspace_copy) -> None:
    from ouroboros.tools.phase_contract import _palace_add
    from ouroboros.tools.registry import ToolContext

    repo, ws = test_workspace_copy
    drive = drive_root(repo, ws)
    ctx = ToolContext(repo_dir=repo, host_repo_root=repo, drive_root=drive)
    ctx.task_id = "run-test-1:plan"
    ctx.loop_state_view = {"phase_label": "plan", "active_workspace_id": ws}

    result = _palace_add(
        ctx,
        title="Harness unique title",
        content="Harness unique body for single-write check",
        kind="observation",
        workspace_id=ws,
        tags="observation,plan",
    )
    payload = json.loads(result)
    assert payload.get("saved") is True
    node_id = payload.get("id") or payload.get("canonical_id")
    store = str(payload.get("store") or "palace.idea")

    palace = MemPalace(repo, ws)
    try:
        node = palace.get(node_id, stores=[store])
        matches = [h for h in palace.list_all(n=200, stores=[store]) if h.get("id") == node_id]
    finally:
        palace.close()
    assert node is not None
    assert len(matches) == 1


def test_live_save_umbrella_memory_skips_duplicate_canonical_id(
    test_workspace_copy,
) -> None:
    from umbrella.deep_agent_tools.memory import save_umbrella_memory

    repo, ws = test_workspace_copy
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.task_id = "run-test-1:plan"

    first = json.loads(
        save_umbrella_memory(
            ctx,
            palace_path=f"workspaces/{ws}/plan",
            title="Dup title",
            content="Dup body content",
            kind="observation",
            workspace_id=ws,
            tags="observation",
        )
    )
    assert first.get("saved") is True
    canonical_id = first.get("canonical_id")
    assert canonical_id

    second = json.loads(
        save_umbrella_memory(
            ctx,
            palace_path=f"workspaces/{ws}/plan",
            title="Dup title",
            content="Dup body content",
            kind="observation",
            workspace_id=ws,
            tags="observation",
            metadata_extra={"canonical_id": canonical_id},
        )
    )
    assert second.get("saved") is True
    assert second.get("canonical_id") == canonical_id

    store = str(first.get("store") or "palace.idea")
    palace = MemPalace(repo, ws)
    try:
        assert palace.get(canonical_id, stores=[store]) is not None
        matches = [h for h in palace.list_all(n=200, stores=[store]) if h.get("id") == canonical_id]
    finally:
        palace.close()
    assert len(matches) == 1


def test_live_promote_to_durable_metadata_roundtrip(test_workspace_copy) -> None:
    from umbrella.deep_agent_tools.phase_contract_handlers import _promote_to_durable
    from umbrella.enforcement.ledger import append_supervisor_ledger_event

    repo, ws = test_workspace_copy
    (repo / "umbrella").mkdir(exist_ok=True)
    drive = drive_root(repo, ws)
    event = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id=ws,
        actor="verifier",
        phase="verify",
        tool="pytest_harness",
        result={"passed": True},
    )
    ctx = MagicMock()
    ctx.host_repo_root = str(repo)
    ctx.repo_dir = str(repo)
    ctx.drive_root = str(drive)
    ctx.loop_state_view = {"phase_label": "verify"}
    ctx.task_id = "run-test-1:verify"
    ctx.umbrella_phase_id = "verify"

    result = _promote_to_durable(
        ctx,
        title="Harness verification",
        content="All harness checks green with no unresolved blockers.",
        workspace_id=ws,
        tags="verification_report,durable",
        evidence_refs=[
            {
                "ref_type": "ledger_event",
                "ref_id": event.event_id,
                "hash": event.event_hash,
                "produced_by": "verifier",
            }
        ],
        trust_level="public_verified",
    )
    payload = json.loads(result)
    assert payload.get("saved") is True
    node_id = str(payload.get("durable_node_id") or payload.get("canonical_id") or "")
    assert node_id

    palace = MemPalace(repo, ws)
    try:
        node = palace.get(node_id, stores=["palace.durable"])
    finally:
        palace.close()
    assert node is not None
    assert node.get("trust_level") == "public_verified"


def test_live_ouroboros_dedup_with_injection_contract(test_workspace_copy) -> None:
    repo, ws = test_workspace_copy
    task = _build_task(repo, ws, manifest_name="verify", phase_id="verify")
    errors = run_ouroboros_dedup_check(repo, ws, task)
    assert not errors


@pytest.mark.parametrize("phase_id", ["research", "plan", "execute", "verify"])
def test_live_init_loop_uses_memory_contract_workspace_across_phases(
    test_workspace_copy,
    phase_id: str,
) -> None:
    from ouroboros.memory_hooks import init_loop_memory

    repo, ws = test_workspace_copy
    drive = drive_root(repo, ws)
    task = _build_task(
        repo,
        ws,
        manifest_name=phase_id,
        phase_id=phase_id,
        run_id=f"run-test-contract-{phase_id}",
        drive_root_path=drive,
    )
    prompt_with_source_refs = (
        str(task["input"])
        + "\n_Sources: core:workspace:00_workspace_charter.md_"
        + "\n_Sources: core:workspace:30_workspace_antipatterns.md_"
    )
    messages = [{"role": "user", "content": prompt_with_source_refs}]
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        context_overlays=task.get("context_overlays") or {},
        umbrella_managed=True,
    )

    _repo_root, detected_ws = init_loop_memory(messages, ctx)

    assert detected_ws == ws
    assert len(messages) == 1
    assert not (repo / "workspaces" / "00_workspace_ch").exists()


def test_live_path_hints_no_nested_workspaces_memory(test_workspace_copy) -> None:
    from ouroboros.tools.phase_contract import _palace_add
    from ouroboros.tools.registry import ToolContext

    repo, ws = test_workspace_copy
    drive = drive_root(repo, ws)
    ctx = ToolContext(repo_dir=repo, host_repo_root=repo, drive_root=drive)
    ctx.task_id = "run-test-1:research"
    ctx.loop_state_view = {"phase_label": "research", "active_workspace_id": ws}

    result = _palace_add(
        ctx,
        title="Path harness",
        content="Path normalization body",
        kind="observation",
        palace_path=f"workspaces/{ws}/research",
        workspace_id=f"workspaces/{ws}",
        tags="observation,research",
    )
    payload = json.loads(result)
    assert payload.get("saved") is True

    mem_root = workspace_memory_root(repo, ws)
    assert mem_root == (repo / "workspaces" / ws / ".memory").resolve()
    nested = repo / "workspaces" / ws / "workspaces"
    assert not nested.exists()
