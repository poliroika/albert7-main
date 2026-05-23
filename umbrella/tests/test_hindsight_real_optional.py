"""Optional live Hindsight smoke tests.

Run with:
UMBRELLA_HINDSIGHT_REAL_TESTS=1 UMBRELLA_HINDSIGHT_ENABLED=1 pytest -m hindsight
"""

import os

import pytest

from umbrella.memory.backends.base import DurableLesson, MemoryQuery
from umbrella.memory.backends.hindsight import HindsightBackend


pytestmark = pytest.mark.hindsight


def _enabled() -> bool:
    return os.environ.get("UMBRELLA_HINDSIGHT_REAL_TESTS") == "1"


@pytest.mark.skipif(not _enabled(), reason="live Hindsight tests disabled")
def test_real_hindsight_health(tmp_path, monkeypatch) -> None:
    pytest.importorskip("hindsight_client")
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_ENABLED", "1")
    backend = HindsightBackend.from_env(repo_root=tmp_path, workspace_id="live")
    health = backend.health()
    assert health["enabled"] is True
    assert "ok" in health


@pytest.mark.skipif(not _enabled(), reason="live Hindsight tests disabled")
def test_real_hindsight_retain_and_recall_verified_lesson(tmp_path, monkeypatch) -> None:
    pytest.importorskip("hindsight_client")
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_ENABLED", "1")
    backend = HindsightBackend.from_env(repo_root=tmp_path, workspace_id="live")
    if not backend.health().get("ok"):
        pytest.skip("Hindsight server unavailable")
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
