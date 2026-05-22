"""Regression: Umbrella phase_run must not duplicate proactive memory in LLM prompt."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def test_ouroboros_context_does_not_duplicate_umbrella_recall_bundle_for_phase_run():
    from ouroboros.context import build_llm_messages, _umbrella_phase_prompt_owned_by_task

    class _Env:
        repo_dir = Path("/tmp/repo")
        drive_root = Path("/tmp/drive")

        def repo_path(self, rel: str) -> Path:
            return self.repo_dir / rel

        def drive_path(self, rel: str) -> Path:
            return self.drive_root / rel

    task = {
        "type": "phase_run",
        "id": "run-1:verify",
        "input": "# Phase: verify\n## [ALWAYS-LOADED MEMORY]\n### BKB\nrule",
        "context_overlays": {
            "phase_prompt_rendered_by_umbrella": True,
            "recall_bundle": {
                "always_on": [{"content": "duplicate always-on snippet"}],
            },
            "phase_manifest": {"id": "verify", "description": "verify phase"},
        },
    }
    assert _umbrella_phase_prompt_owned_by_task(task)

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
        messages, _cap = build_llm_messages(env=_Env(), memory=_Mem(), task=task)

    system_text = ""
    for msg in messages:
        if msg.get("role") == "system":
            system_text += str(msg.get("content") or "")
    assert "[ALWAYS-ON CONTEXT]" not in system_text
    assert "[PHASE: verify]" not in system_text


def test_umbrella_phase_prompt_has_single_always_loaded_memory_block(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces").mkdir()
    core = tmp_path / ".umbrella" / "memory" / "core"
    core.mkdir(parents=True)
    (core / "00_identity.md").write_text("# Identity\nVerify always.\n", encoding="utf-8")
    (core / "10_operating_principles.md").write_text("# Principles\n", encoding="utf-8")
    (core / "bkb.yaml").write_text("rules: []\n", encoding="utf-8")

    from umbrella.phases.loader import load_manifest
    from umbrella.phases.base import PhaseNode
    from umbrella.memory.palace.facade import MemPalace
    from umbrella.orchestrator.worker import build_phase_task
    from ouroboros.memory_hooks import init_loop_memory
    from ouroboros.context import build_llm_messages

    manifest = load_manifest(Path("umbrella/phases/manifests/verify.yaml"))
    palace = MemPalace(tmp_path, "ws1")
    palace.close()
    phase_node = PhaseNode(id="verify", manifest_id="verify", status="running")
    task = build_phase_task(
        phase_node=phase_node,
        manifest=manifest,
        workspace_id="ws1",
        run_id="run-1",
        palace=MemPalace(tmp_path, "ws1"),
        repo_root=tmp_path,
    )

    messages = [{"role": "user", "content": task["input"]}]
    ctx = SimpleNamespace(
        host_repo_root=tmp_path,
        repo_dir=tmp_path,
        drive_root=tmp_path / "drive",
        context_overlays=task.get("context_overlays") or {},
        umbrella_managed=True,
    )
    init_loop_memory(messages, ctx)

    class _Env:
        repo_dir = tmp_path
        drive_root = tmp_path / "drive"

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

    combined = task["input"] + "\n" + "\n".join(
        str(m.get("content") or "") for m in messages + llm_messages if m.get("role") == "system"
    )
    assert combined.count("[ALWAYS-LOADED MEMORY]") == 1
