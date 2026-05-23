"""Fast integration tests using committed ``workspaces/test`` fixture.

Unlike ``test_proactive_memory_e2e`` (compiler/prompt-only on tmp_path), this
module exercises ``build_phase_task``, drive artifacts, ``palace_add``,
``save_umbrella_memory``, and Ouroboros dedup against a realistic workspace layout.
Unlike live ``civilization`` runs, there is no LLM and no full phase runner loop.

Set ``UMBRELLA_TEST_HARNESS_VERBOSE=1`` to print injection report / audit on failure.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.paths import workspace_memory_root
from umbrella.orchestrator.worker import build_phase_task
from umbrella.phases.base import PhaseNode
from umbrella.phases.loader import load_manifest

pytestmark = pytest.mark.workspace_live

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


def _drive_root(repo: Path, workspace_id: str) -> Path:
    drive = repo / "workspaces" / workspace_id / ".memory" / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    return drive


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


def _assert_single_always_loaded_block(prompt: str) -> None:
    assert (
        len(re.findall(r"^## \[ALWAYS-LOADED MEMORY\]", prompt, flags=re.MULTILINE))
        == 1
    )
    assert "## [/ALWAYS-LOADED MEMORY]" in prompt


def _assert_memory_injection_contract(task: dict[str, Any]) -> dict[str, Any]:
    overlays = task.get("context_overlays") or {}
    contract = overlays.get("memory_injection_contract")
    assert isinstance(contract, dict), "missing memory_injection_contract"
    assert contract.get("mode") == "umbrella_owned"
    assert contract.get("proactive_overlay_injected") is True
    assert contract.get("proactive_overlay_hash")
    assert contract.get("retrieval_is_supplemental_only") is True
    assert overlays.get("prevent_ouroboros_auto_core_overlay") is True
    return contract


def _load_drive_artifacts(drive: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle_path = drive / "state" / "llm_input_bundle_latest.json"
    report_path = drive / "state" / "memory_injection_report_latest.json"
    assert bundle_path.is_file(), f"missing bundle: {bundle_path}"
    assert report_path.is_file(), f"missing report: {report_path}"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return bundle, report


def _skipped_ids(report: dict[str, Any]) -> set[str]:
    return {
        str(row.get("id"))
        for row in (report.get("skipped") or [])
        if isinstance(row, dict) and row.get("id")
    }


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
    drive_root: Path | None = None,
) -> dict:
    manifest = load_manifest(Path(f"umbrella/phases/manifests/{manifest_name}.yaml"))
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
            drive_root=drive_root,
        )
    finally:
        palace.close()


def test_live_build_phase_task_injection_contract_research_verify(
    test_workspace_copy,
) -> None:
    repo, ws = test_workspace_copy
    for manifest_name in ("research", "verify"):
        task = _build_task(repo, ws, manifest_name=manifest_name, phase_id=manifest_name)
        _assert_memory_injection_contract(task)
        prompt = str(task.get("input") or "")
        _assert_single_always_loaded_block(prompt)
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
    """Per-phase BKB filtering: research injects workspace rule; others skip it."""
    repo, ws = test_workspace_copy
    drive = _drive_root(repo, ws)
    task = _build_task(
        repo,
        ws,
        manifest_name=phase_id,
        phase_id=phase_id,
        drive_root=drive,
    )
    contract = _assert_memory_injection_contract(task)
    prompt = str(task.get("input") or "")
    _assert_single_always_loaded_block(prompt)

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

    skipped = _skipped_ids(report)
    assert expect_skipped_ids <= skipped, (
        f"phase={phase_id} expected skipped {expect_skipped_ids}, got {skipped}"
    )

    included_ids = set(audit.get("included_bkb_ids") or [])
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
    assert memory_items, f"bundle has no memory_items for phase {phase_id}"
    directive_items = [m for m in memory_items if m.get("directive")]
    assert directive_items, "expected at least one directive proactive memory item"


def test_live_injection_report_included_reasons(test_workspace_copy) -> None:
    repo, ws = test_workspace_copy
    drive = _drive_root(repo, ws)
    _build_task(repo, ws, manifest_name="research", phase_id="research", drive_root=drive)
    _bundle, report = _load_drive_artifacts(drive)
    reasons = _included_reasons(report)
    assert reasons, "report.included should list memory items"
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
    drive = _drive_root(repo, ws)
    task = _build_task(
        repo,
        ws,
        manifest_name="research",
        phase_id="research",
        drive_root=drive,
    )
    contract = _assert_memory_injection_contract(task)
    bundle, report = _load_drive_artifacts(drive)

    assert report.get("proactive_overlay_hash") == contract.get("proactive_overlay_hash")
    assert report.get("llm_input_bundle_hash") == (
        task.get("context_overlays") or {}
    ).get("llm_input_bundle_hash")
    assert isinstance(report.get("included"), list)
    per_phase = drive / "state" / "llm_input_bundle_research.json"
    assert per_phase.is_file(), "per-phase bundle snapshot missing"

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
    drive = _drive_root(repo, ws)
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
        matches = [
            h
            for h in palace.list_all(n=200, stores=[store])
            if h.get("id") == node_id
        ]
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
    drive = _drive_root(repo, ws)
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
    assert node.get("surface") == "supplemental_evidence" or node.get("surface")


def test_live_ouroboros_dedup_with_injection_contract(test_workspace_copy) -> None:
    from ouroboros.context import build_llm_messages
    from ouroboros.memory_hooks import init_loop_memory

    repo, ws = test_workspace_copy
    task = _build_task(repo, ws, manifest_name="verify", phase_id="verify")
    messages = [{"role": "user", "content": task["input"]}]
    ctx = SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=_drive_root(repo, ws),
        context_overlays=task.get("context_overlays") or {},
        umbrella_managed=True,
    )
    init_loop_memory(messages, ctx)
    assert len(messages) == 1

    class _Env:
        repo_dir = repo
        drive_root = repo / "workspaces" / ws / ".memory" / "drive"

        def repo_path(self, rel: str) -> Path:
            return self.repo_dir / rel

        def drive_path(self, rel: str) -> Path:
            return self.drive_root / rel

    class _Mem:
        def ensure_files(self) -> None:
            return None

    with (
        patch("ouroboros.context._safe_read", return_value=""),
        patch("ouroboros.context._build_memory_sections", return_value=[]),
        patch("ouroboros.context._build_recent_sections", return_value=[]),
        patch("ouroboros.context._build_runtime_section", return_value=""),
        patch("ouroboros.context._build_health_invariants", return_value=""),
        patch("ouroboros.context.use_anthropic_style_cache_extensions", return_value=False),
    ):
        llm_messages, _ = build_llm_messages(env=_Env(), memory=_Mem(), task=task)

    system_text = "\n".join(
        str(m.get("content") or "")
        for m in messages + llm_messages
        if m.get("role") == "system"
    )
    assert "[ALWAYS-ON CONTEXT]" not in system_text
    assert "[PHASE: verify]" not in system_text
    assert "## [ALWAYS-LOADED MEMORY]" not in system_text
    task_input = str(task.get("input") or "")
    assert (
        len(re.findall(r"^## \[ALWAYS-LOADED MEMORY\]", task_input, flags=re.MULTILINE))
        == 1
    )


def test_live_path_hints_no_nested_workspaces_memory(test_workspace_copy) -> None:
    from ouroboros.tools.phase_contract import _palace_add
    from ouroboros.tools.registry import ToolContext

    repo, ws = test_workspace_copy
    drive = _drive_root(repo, ws)
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
