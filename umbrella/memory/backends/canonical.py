"""Canonical MemPalace durable backend."""

from pathlib import Path
from typing import Any

from umbrella.memory.backends.base import DurableEvent, DurableLesson
from umbrella.memory.kernel.models import MemoryEvent, memory_event_from_tool_write
from umbrella.memory.kernel.writer import write_memory_event
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.tiers import Scope, Tier


def _canonical_trust(trust_level: str) -> str:
    trust = str(trust_level or "").strip()
    if trust in {
        "workspace_verified",
        "supervisor_verified",
        "public_verified",
        "mutation_verified",
        "hidden_verified",
        "adversarial_verified",
    }:
        return "public_verified"
    return "agent_claim"


def _event_from_backend_dict(
    payload: MemoryEvent | DurableEvent | DurableLesson | dict[str, Any],
    *,
    workspace_id: str,
    default_kind: str,
    default_store: str,
    default_scope: str,
    default_trust: str = "agent_claim",
    default_lifecycle: str = "candidate",
) -> MemoryEvent:
    if isinstance(payload, MemoryEvent):
        return payload
    if isinstance(payload, DurableLesson):
        return memory_event_from_tool_write(
            content=payload.content,
            title=payload.title,
            memory_kind="bkb_rule" if "bkb" in payload.kind else "lesson",
            workspace_id=payload.workspace_id or workspace_id,
            tags=payload.tags,
            scope=Scope.CROSS_RUN_DURABLE,
            tier=Tier.WARM,
            trust_level=_canonical_trust(str(payload.trust_level)),
            evidence_refs=payload.evidence_refs,
            lifecycle="active",
            surface="supplemental_evidence",
            source_backend="canonical_mempalace",
            verified=True,
            phase_id=payload.phase_id,
            run_id=payload.run_id,
            palace_store="palace.lesson",
            metadata=dict(payload.metadata),
        )
    if isinstance(payload, DurableEvent):
        durable_kind = (
            "verification_report"
            if payload.kind == "verification_report"
            else "durable"
            if payload.kind in {"run_summary", "architecture_decision", "durable_promotion"}
            else default_kind
        )
        durable_store = (
            "palace.durable"
            if durable_kind in {"verification_report", "durable"}
            else default_store
        )
        durable_scope = (
            Scope.CROSS_RUN_DURABLE
            if durable_store == "palace.durable"
            else default_scope
        )
        return memory_event_from_tool_write(
            content=payload.content,
            title=payload.kind,
            memory_kind=durable_kind,
            workspace_id=payload.workspace_id or workspace_id,
            tags=payload.tags,
            scope=durable_scope,
            tier=Tier.WARM,
            trust_level=_canonical_trust(str(payload.trust_level)),
            evidence_refs=payload.evidence_refs,
            lifecycle="active" if durable_store == "palace.durable" else "candidate",
            surface="supplemental_evidence",
            source_backend="canonical_mempalace",
            verified=True,
            phase_id=payload.phase_id,
            run_id=payload.run_id,
            subtask_id=payload.subtask_id,
            palace_store=durable_store,
            metadata=dict(payload.metadata),
        )
    data = dict(payload)
    verified = bool(data.get("verified", False))
    return memory_event_from_tool_write(
        content=str(data.get("content") or ""),
        title=str(data.get("title") or ""),
        memory_kind=str(data.get("kind") or data.get("memory_kind") or default_kind),
        workspace_id=str(data.get("workspace_id") or workspace_id),
        tags=data.get("tags") or [],
        scope=str(data.get("scope") or default_scope),
        tier=str(data.get("tier") or Tier.WARM),
        trust_level=_canonical_trust(
            str(data.get("trust_level") or default_trust)
        ),
        evidence_refs=data.get("evidence_refs") or [],
        lifecycle=str(data.get("lifecycle") or default_lifecycle),
        surface=str(data.get("surface") or "supplemental_evidence"),
        source_backend=str(data.get("source_backend") or "canonical_mempalace"),
        verified=verified,
        phase_id=str(data.get("phase") or data.get("phase_id") or ""),
        run_id=str(data.get("run_id") or ""),
        subtask_id=str(data.get("subtask_id") or ""),
        source_path=str(data.get("source_path") or ""),
        palace_store=str(data.get("store") or default_store),
        metadata=dict(data.get("metadata") or {}),
    )


def _result_payload(result: Any) -> dict[str, Any]:
    return {
        "ok": bool(result.saved),
        "saved": bool(result.saved),
        "canonical_id": str(result.canonical_id or ""),
        "store": str(result.store or ""),
        "backend": str(result.backend or "canonical_mempalace"),
        "error": str(result.error or ""),
        "policy_issues": list(result.policy_issues or ()),
        "skipped_duplicate": bool(result.skipped_duplicate),
    }


class CanonicalMemoryBackend:
    def __init__(self, repo_root: Path, workspace_id: str = "") -> None:
        self._repo_root = repo_root
        self._workspace_id = workspace_id
        self._palace = MemPalace(repo_root, workspace_id or None)

    def ensure_banks(self, *, workspace_id: str = "") -> dict[str, Any]:
        return {
            "ok": True,
            "backend": "canonical_mempalace",
            "workspace_id": workspace_id or self._workspace_id,
        }

    def retain_event(
        self, event: MemoryEvent | DurableEvent | dict[str, Any]
    ) -> dict[str, Any]:
        memory_event = _event_from_backend_dict(
            event,
            workspace_id=self._workspace_id,
            default_kind="observation",
            default_store="palace.run",
            default_scope=Scope.RUN_SCOPED,
        )
        result = write_memory_event(
            self._repo_root,
            memory_event,
            workspace_id=memory_event.workspace_id or self._workspace_id,
        )
        return _result_payload(result)

    def retain_lesson(
        self, lesson: MemoryEvent | DurableLesson | dict[str, Any]
    ) -> dict[str, Any]:
        payload = lesson if isinstance(lesson, dict) else {}
        verified = (
            bool(payload.get("verified", True))
            if isinstance(lesson, dict)
            else getattr(lesson, "verified", True)
        )
        memory_event = _event_from_backend_dict(
            lesson,
            workspace_id=self._workspace_id,
            default_kind="lesson",
            default_store="palace.lesson",
            default_scope=Scope.CROSS_RUN_DURABLE,
            default_trust="public_verified",
            default_lifecycle="active" if verified else "candidate",
        )
        result = write_memory_event(
            self._repo_root,
            memory_event,
            workspace_id=memory_event.workspace_id or self._workspace_id,
        )
        return _result_payload(result)

    def recall_evidence(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return self._palace.search(
            str(query.get("query") or ""),
            stores=query.get("stores"),
            n=int(query.get("limit") or 10),
        )

    def reflect_candidates(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def health(self) -> dict[str, Any]:
        return self._palace.health()

    def close(self) -> None:
        self._palace.close()
