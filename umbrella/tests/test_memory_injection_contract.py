"""Tests for umbrella-owned memory injection contract overlays."""

import pytest

from umbrella.memory.palace.facade import MemPalace
from umbrella.orchestrator.worker import build_phase_task
from umbrella.phases.base import PhaseNode
from umbrella.phases.loader import load_manifest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces" / "ws1").mkdir(parents=True)
    core = tmp_path / ".umbrella" / "memory" / "core"
    core.mkdir(parents=True)
    (core / "00_identity.md").write_text("# Identity\n", encoding="utf-8")
    (core / "10_operating_principles.md").write_text("# Principles\n", encoding="utf-8")
    (core / "bkb.yaml").write_text("rules: []\n", encoding="utf-8")
    return tmp_path


def test_memory_injection_contract_present_in_phase_task(repo) -> None:
    from pathlib import Path

    manifest = load_manifest(Path("umbrella/phases/manifests/plan.yaml"))
    phase_node = PhaseNode(id="plan-1", manifest_id="plan")
    palace = MemPalace(repo, "ws1")
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id="ws1",
            run_id="run-1",
            palace=palace,
            repo_root=repo,
        )
    finally:
        palace.close()

    overlays = task.get("context_overlays") or {}
    contract = overlays.get("memory_injection_contract")
    assert isinstance(contract, dict)
    assert contract.get("mode") == "umbrella_owned"
    assert contract.get("proactive_overlay_injected") is True
    assert contract.get("proactive_overlay_hash")
    assert contract.get("retrieval_is_supplemental_only") is True
    assert overlays.get("prevent_ouroboros_auto_core_overlay") is True


def test_phase_task_id_includes_started_at_attempt_suffix(repo) -> None:
    from pathlib import Path

    manifest = load_manifest(Path("umbrella/phases/manifests/execute.yaml"))
    phase_node = PhaseNode(id="execute", manifest_id="execute", started_at=123.456)
    palace = MemPalace(repo, "ws1")
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id="ws1",
            run_id="run-1",
            palace=palace,
            repo_root=repo,
        )
    finally:
        palace.close()

    assert task["id"] == "run-1:execute:123456"


def test_memory_injection_contract_directive_sections_from_fixture_workspace(
    test_workspace_copy,
) -> None:
    from pathlib import Path

    repo, ws = test_workspace_copy
    manifest = load_manifest(Path("umbrella/phases/manifests/research.yaml"))
    phase_node = PhaseNode(id="research-1", manifest_id="research")
    palace = MemPalace(repo, ws)
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id=ws,
            run_id="run-fixture-1",
            palace=palace,
            repo_root=repo,
        )
    finally:
        palace.close()

    contract = (task.get("context_overlays") or {}).get("memory_injection_contract") or {}
    sections = contract.get("directive_sections") or []
    assert sections
    prompt = str(task.get("input") or "")
    assert "provenance" in prompt.lower() or "source_id" in prompt.lower()
    assert "inject me please" not in prompt


def test_research_phase_defaults_to_light_depth(repo) -> None:
    from pathlib import Path

    manifest = load_manifest(Path("umbrella/phases/manifests/research.yaml"))
    phase_node = PhaseNode(id="research-1", manifest_id="research")
    palace = MemPalace(repo, "ws1")
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id="ws1",
            run_id="run-research-light",
            palace=palace,
            repo_root=repo,
        )
    finally:
        palace.close()

    overlays = task.get("context_overlays") or {}
    assert overlays.get("research_depth") == "light"
    assert "Umbrella selected `light`" in str(task.get("input") or "")


def test_research_phase_respects_explicit_full_depth(repo) -> None:
    from pathlib import Path

    manifest = load_manifest(Path("umbrella/phases/manifests/research.yaml"))
    phase_node = PhaseNode(
        id="research-1",
        manifest_id="research",
        overlay={"research_depth": "full"},
    )
    palace = MemPalace(repo, "ws1")
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id="ws1",
            run_id="run-research-full",
            palace=palace,
            repo_root=repo,
        )
    finally:
        palace.close()

    overlays = task.get("context_overlays") or {}
    assert overlays.get("research_depth") == "full"


@pytest.mark.parametrize("manifest_id", ["preflight", "verify"])
def test_gate_phase_recall_bundle_has_no_warm_when_manifest_disables_it(
    repo, manifest_id: str
) -> None:
    from pathlib import Path

    manifest = load_manifest(
        Path(f"umbrella/phases/manifests/{manifest_id}.yaml")
    )
    phase_node = PhaseNode(id=f"{manifest_id}-1", manifest_id=manifest_id)
    palace = MemPalace(repo, "ws1")
    try:
        task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id="ws1",
            run_id="run-gate-1",
            palace=palace,
            repo_root=repo,
        )
    finally:
        palace.close()

    recall = (task.get("context_overlays") or {}).get("recall_bundle") or {}
    assert recall.get("warm") == []
