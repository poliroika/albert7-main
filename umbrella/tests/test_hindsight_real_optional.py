"""Optional live Hindsight smoke tests.

See docs/memory-durable-backends.md (Live / release smoke tests).
"""

import os
from pathlib import Path

import pytest

from umbrella.memory.backends.base import DurableLesson, MemoryQuery, ReflectionQuery
from umbrella.memory.backends.hindsight import HindsightBackend
from umbrella.memory.hindsight.candidates import (
    build_reflection_question,
    proposal_queue_dir,
    write_hindsight_candidates_as_pending_proposals,
)
from umbrella.memory.paths import workspace_core_root


pytestmark = pytest.mark.hindsight


def _enabled() -> bool:
    return os.environ.get("UMBRELLA_HINDSIGHT_REAL_TESTS") == "1"


def _live_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HindsightBackend:
    pytest.importorskip("hindsight_client")
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_ENABLED", "1")
    backend = HindsightBackend.from_env(repo_root=tmp_path, workspace_id="live")
    if not backend.health().get("ok"):
        pytest.skip("Hindsight server unavailable")
    return backend


@pytest.mark.skipif(not _enabled(), reason="live Hindsight tests disabled")
def test_real_hindsight_health(tmp_path, monkeypatch) -> None:
    backend = _live_backend(tmp_path, monkeypatch)
    health = backend.health()
    assert health["enabled"] is True
    assert "ok" in health


@pytest.mark.skipif(not _enabled(), reason="live Hindsight tests disabled")
def test_real_hindsight_retain_and_recall_verified_lesson(tmp_path, monkeypatch) -> None:
    backend = _live_backend(tmp_path, monkeypatch)
    evidence = [
        {
            "ref_type": "artifact",
            "ref_id": "live-test",
            "produced_by": "supervisor",
        }
    ]
    backend.retain_lesson(
        DurableLesson(
            lesson_id="live_test_lesson",
            kind="verified_lesson",
            title="Live test lesson",
            content="Live Hindsight retain smoke test.",
            workspace_id="live",
            trust_level="supervisor_verified",
            evidence_refs=evidence,
        )
    )
    hits = backend.recall_evidence(MemoryQuery(query="Live Hindsight retain", workspace_id="live"))
    assert isinstance(hits, list)


@pytest.mark.skipif(not _enabled(), reason="live Hindsight tests disabled")
def test_real_hindsight_retain_recall_reflect_candidate_queue(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_REFLECT_ENABLED", "1")
    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "dual")
    backend = _live_backend(tmp_path, monkeypatch)
    workspace_id = "live"
    drive_root = tmp_path / "workspaces" / workspace_id / ".memory" / "drive"
    drive_root.mkdir(parents=True, exist_ok=True)

    evidence = [
        {
            "ref_type": "artifact",
            "ref_id": "live-e2e-smoke",
            "produced_by": "supervisor",
        }
    ]
    lesson_content = "Live Hindsight E2E retain recall reflect smoke."
    backend.retain_lesson(
        DurableLesson(
            lesson_id="live_e2e_lesson",
            kind="verified_lesson",
            title="Live E2E lesson",
            content=lesson_content,
            workspace_id=workspace_id,
            trust_level="supervisor_verified",
            evidence_refs=evidence,
        )
    )

    hits = backend.recall_evidence(
        MemoryQuery(query="Live Hindsight E2E", workspace_id=workspace_id, limit=5)
    )
    assert hits, "recall_evidence returned no hits after retain"

    candidates = backend.reflect_candidates(
        ReflectionQuery(
            question=build_reflection_question(max_candidates=2),
            workspace_id=workspace_id,
            run_id="live-e2e-run",
            phase_id="reflexion",
            max_candidates=2,
        )
    )
    if not candidates:
        pytest.skip("Hindsight reflect returned no candidates (server/content dependent)")

    queue_result = write_hindsight_candidates_as_pending_proposals(
        drive_root=drive_root,
        repo_root=tmp_path,
        workspace_id=workspace_id,
        run_id="live-e2e-run",
        phase_id="reflexion",
        candidates=candidates,
    )
    assert queue_result.get("queued", 0) >= 1

    queue_dir = proposal_queue_dir(drive_root)
    assert any(queue_dir.glob("*.candidate.json"))

    bkb_path = workspace_core_root(tmp_path, workspace_id) / "bkb.yaml"
    assert not bkb_path.is_file(), "candidates must stay in queue, not auto-written to BKB"
