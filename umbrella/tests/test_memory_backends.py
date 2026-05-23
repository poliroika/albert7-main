"""Durable backend factory, Hindsight adapter, and dual-write behavior."""

from unittest.mock import MagicMock

import pytest

from umbrella.contracts import EvidenceRef
from umbrella.enforcement.ledger import append_supervisor_ledger_event
from umbrella.memory.backends.base import (
    DurableLesson,
    MemoryQuery,
    ReflectionQuery,
)
from umbrella.memory.backends.canonical import CanonicalMemoryBackend
from umbrella.memory.backends.dual_write import DualWriteDurableBackend, create_durable_backend
from umbrella.memory.backends.hindsight import HindsightBackend
from umbrella.memory.hindsight.config import HindsightConfig
from umbrella.memory.hindsight.errors import HindsightPolicyError
from umbrella.memory.kernel.models import MemoryEvent


class FakeBanks:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.missions: list[dict] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return {"ok": True}

    def set_mission(self, **kwargs):
        self.missions.append(kwargs)
        return {"ok": True}

    def list(self):
        return []


class FakeHindsightClient:
    def __init__(self) -> None:
        self.banks = FakeBanks()
        self.retained: list[dict] = []
        self.recall_rows: list[dict] = []
        self.reflect_payload = {"candidates": []}

    def retain(self, **kwargs):
        self.retained.append(kwargs)
        return {"id": kwargs.get("document_id"), "status": "ok"}

    def recall(self, **kwargs):
        return {"results": list(self.recall_rows)}

    def reflect(self, **kwargs):
        self.reflect_kwargs = kwargs
        return self.reflect_payload


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces" / "ws1").mkdir(parents=True)
    return tmp_path


def _verified_ref(repo):
    ledger = append_supervisor_ledger_event(
        repo_root=repo,
        workspace_id="ws1",
        actor="verifier",
        phase="verify",
        tool="pytest",
        result={"passed": True},
    )
    return {
        "ref_type": "ledger_event",
        "ref_id": ledger.event_id,
        "hash": ledger.event_hash,
        "produced_by": "verifier",
    }


def test_hindsight_disabled_by_default(repo, monkeypatch) -> None:
    monkeypatch.delenv("UMBRELLA_HINDSIGHT_ENABLED", raising=False)
    backend = HindsightBackend.from_env(repo_root=repo, workspace_id="ws1")
    assert backend.health()["enabled"] is False


def test_factory_modes(repo, monkeypatch) -> None:
    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "canonical")
    backend = create_durable_backend(repo, workspace_id="ws1")
    assert isinstance(backend, CanonicalMemoryBackend)
    backend.close()

    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "hindsight")
    backend = create_durable_backend(repo, workspace_id="ws1")
    assert isinstance(backend, HindsightBackend)

    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "dual")
    backend = create_durable_backend(repo, workspace_id="ws1")
    assert isinstance(backend, DualWriteDurableBackend)
    backend.close()


def test_hindsight_retain_lesson_requires_verified_trust(repo) -> None:
    fake = FakeHindsightClient()
    backend = HindsightBackend(
        repo_root=repo,
        workspace_id="ws1",
        config=HindsightConfig(enabled=True),
        client=fake,
    )
    with pytest.raises(HindsightPolicyError):
        backend.retain_lesson(
            DurableLesson(
                lesson_id="l1",
                kind="lesson",
                title="bad",
                content="agent claim",
                workspace_id="ws1",
                trust_level="agent_reported",
                evidence_refs=[_verified_ref(repo)],
            )
        )


def test_hindsight_retain_lesson_requires_evidence(repo) -> None:
    fake = FakeHindsightClient()
    backend = HindsightBackend(
        repo_root=repo,
        workspace_id="ws1",
        config=HindsightConfig(enabled=True),
        client=fake,
    )
    with pytest.raises(HindsightPolicyError):
        backend.retain_lesson(
            DurableLesson(
                lesson_id="l1",
                kind="lesson",
                title="bad",
                content="missing evidence",
                workspace_id="ws1",
                trust_level="public_verified",
            )
        )


def test_hindsight_retain_lesson_payload_tags_metadata(repo) -> None:
    fake = FakeHindsightClient()
    backend = HindsightBackend(
        repo_root=repo,
        workspace_id="ws1",
        config=HindsightConfig(enabled=True, retain_async=False),
        client=fake,
    )
    result = backend.retain_lesson(
        DurableLesson(
            lesson_id="l1",
            kind="accepted_bkb_rule",
            title="Accepted",
            content="Rule body token=secret-value",
            workspace_id="ws1",
            run_id="run1",
            phase_id="reflexion",
            trust_level="supervisor_verified",
            evidence_refs=[_verified_ref(repo)],
            metadata={"bkb_rule_id": "r1", "ignored_env": "OPENAI_API_KEY"},
        )
    )
    assert result["ok"] is True
    retained = fake.retained[0]
    assert retained["document_id"] == "umbrella:lesson:l1"
    assert "workspace:ws1" in retained["tags"]
    assert retained["metadata"]["bkb_rule_id"] == "r1"
    assert "ignored_env" not in retained["metadata"]
    assert "secret-value" not in retained["content"]


def test_hindsight_recall_returns_memory_hits(repo) -> None:
    fake = FakeHindsightClient()
    fake.recall_rows = [{"content": "evidence hint", "id": "m1", "score": 0.7}]
    backend = HindsightBackend(
        repo_root=repo,
        workspace_id="ws1",
        config=HindsightConfig(enabled=True),
        client=fake,
    )
    hits = backend.recall_evidence(MemoryQuery(query="hint", workspace_id="ws1"))
    assert hits[0].text == "evidence hint"
    assert hits[0].source == "m1"


def test_hindsight_reflect_candidates_parses_structured_output(repo) -> None:
    fake = FakeHindsightClient()
    fake.reflect_payload = {
        "candidates": [
            {
                "kind": "anti_pattern",
                "title": "Do not store transient drafts",
                "content": "Reject draft notes as durable lessons.",
                "scope": "workspace",
                "confidence": 0.82,
                "evidence_refs": [_verified_ref(repo)],
                "why_durable": "Repeated across runs.",
                "risk_if_wrong": "Over-filtering.",
            }
        ]
    }
    backend = HindsightBackend(
        repo_root=repo,
        workspace_id="ws1",
        config=HindsightConfig(enabled=True, reflect_enabled=True),
        client=fake,
    )
    candidates = backend.reflect_candidates(
        ReflectionQuery(question="propose", workspace_id="ws1", max_candidates=3)
    )
    assert len(candidates) == 1
    assert candidates[0].source_backend == "hindsight"
    assert candidates[0].evidence_refs


def test_dual_write_canonical_success_hindsight_failure_warns(repo, monkeypatch) -> None:
    monkeypatch.setenv("UMBRELLA_HINDSIGHT_ENABLED", "1")
    canonical = CanonicalMemoryBackend(repo, "ws1")
    secondary = MagicMock()
    secondary.health.return_value = {"ok": True, "enabled": True}
    secondary.retain_lesson.side_effect = RuntimeError("server down")
    backend = DualWriteDurableBackend(primary=canonical, secondary=secondary)
    result = backend.retain_lesson(
        {
            "content": "Verified lesson body",
            "title": "Lesson",
            "trust_level": "public_verified",
            "evidence_refs": [_verified_ref(repo)],
        }
    )
    canonical.close()
    assert result["ok"] is True
    assert result["hindsight"]["best_effort"] is True


def test_dual_write_no_hindsight_call_when_disabled(repo) -> None:
    canonical = CanonicalMemoryBackend(repo, "ws1")
    secondary = MagicMock()
    secondary.health.return_value = {"ok": False, "enabled": False}
    backend = DualWriteDurableBackend(primary=canonical, secondary=secondary)
    result = backend.retain_event(
        MemoryEvent(content="run observation", title="event", workspace_id="ws1")
    )
    canonical.close()
    assert result["ok"] is True
    secondary.retain_event.assert_not_called()
