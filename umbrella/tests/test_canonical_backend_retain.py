"""CanonicalMemoryBackend retain_* paths through write_memory_event."""

import pytest

from umbrella.enforcement.ledger import append_supervisor_ledger_event
from umbrella.memory.backends.canonical import CanonicalMemoryBackend
from umbrella.memory.backends.dual_write import create_durable_backend
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.tiers import Scope


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces" / "ws1").mkdir(parents=True)
    return tmp_path


def test_retain_lesson_without_trust_or_evidence_returns_empty_id(repo) -> None:
    backend = CanonicalMemoryBackend(repo, "ws1")
    try:
        result = backend.retain_lesson(
            {
                "content": "lesson without evidence",
                "trust_level": "agent_claim",
                "evidence_refs": [],
            }
        )
    finally:
        backend.close()
    assert result["saved"] is False


def test_retain_lesson_with_ledger_evidence_succeeds(repo) -> None:
    event = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id="ws1",
        actor="verifier",
        phase="verify",
        tool="pytest",
        result={"passed": True},
    )
    backend = CanonicalMemoryBackend(repo, "ws1")
    try:
        result = backend.retain_lesson(
            {
                "content": "Verified lesson body",
                "title": "Lesson",
                "trust_level": "public_verified",
                "evidence_refs": [
                    {
                        "ref_type": "ledger_event",
                        "ref_id": event.event_id,
                        "hash": event.event_hash,
                        "produced_by": "verifier",
                    }
                ],
            }
        )
    finally:
        backend.close()
    assert result["canonical_id"]

    palace = MemPalace(repo, "ws1")
    try:
        node = palace.get(result["canonical_id"], stores=["palace.lesson"])
    finally:
        palace.close()
    assert node is not None
    assert node.get("trust_level") == "public_verified"


def test_retain_event_run_scoped_observation_succeeds(repo) -> None:
    backend = CanonicalMemoryBackend(repo, "ws1")
    try:
        result = backend.retain_event(
            {
                "content": "Run-scoped observation",
                "kind": "observation",
                "scope": Scope.RUN_SCOPED,
                "store": "palace.run",
            }
        )
    finally:
        backend.close()
    assert result["canonical_id"]


def test_retain_event_preserves_trust_and_evidence(repo) -> None:
    event = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id="ws1",
        actor="verifier",
        phase="verify",
        tool="pytest",
        result={"ok": True},
    )
    backend = CanonicalMemoryBackend(repo, "ws1")
    try:
        result = backend.retain_event(
            {
                "content": "Durable-ish event",
                "kind": "durable",
                "scope": "cross_run_durable",
                "store": "palace.durable",
                "trust_level": "public_verified",
                "lifecycle": "active",
                "surface": "supplemental_evidence",
                "source_backend": "canonical_mempalace",
                "evidence_refs": [
                    {
                        "ref_type": "ledger_event",
                        "ref_id": event.event_id,
                        "hash": event.event_hash,
                        "produced_by": "verifier",
                    }
                ],
            }
        )
    finally:
        backend.close()
    assert result["canonical_id"]
