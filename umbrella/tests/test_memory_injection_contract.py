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
