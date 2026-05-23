"""Canonical MemPalace durable backend."""

from pathlib import Path
from typing import Any

from umbrella.memory.kernel.models import MemoryEvent, memory_event_from_tool_write
from umbrella.memory.kernel.writer import write_memory_event
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.tiers import Scope, Tier


def _event_from_backend_dict(
    payload: MemoryEvent | dict[str, Any],
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
    data = dict(payload)
    verified = bool(data.get("verified", False))
    return memory_event_from_tool_write(
        content=str(data.get("content") or ""),
        title=str(data.get("title") or ""),
        memory_kind=str(data.get("kind") or data.get("memory_kind") or default_kind),
        workspace_id=workspace_id,
        tags=data.get("tags") or [],
        scope=str(data.get("scope") or default_scope),
        tier=str(data.get("tier") or Tier.WARM),
        trust_level=str(data.get("trust_level") or default_trust),
        evidence_refs=data.get("evidence_refs") or [],
        lifecycle=str(data.get("lifecycle") or default_lifecycle),
        surface=str(data.get("surface") or "supplemental_evidence"),
        source_backend=str(data.get("source_backend") or "canonical_mempalace"),
        verified=verified,
        phase_id=str(data.get("phase") or ""),
        run_id=str(data.get("run_id") or ""),
        subtask_id=str(data.get("subtask_id") or ""),
        source_path=str(data.get("source_path") or ""),
        palace_store=str(data.get("store") or default_store),
        metadata=dict(data.get("metadata") or {}),
    )


class CanonicalMemoryBackend:
    def __init__(self, repo_root: Path, workspace_id: str = "") -> None:
        self._repo_root = repo_root
        self._workspace_id = workspace_id
        self._palace = MemPalace(repo_root, workspace_id or None)

    def retain_event(self, event: MemoryEvent | dict[str, Any]) -> str:
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
            workspace_id=self._workspace_id,
        )
        return result.canonical_id if result.saved else ""

    def retain_lesson(self, lesson: MemoryEvent | dict[str, Any]) -> str:
        payload = lesson if isinstance(lesson, dict) else {}
        verified = (
            bool(payload.get("verified", True))
            if isinstance(lesson, dict)
            else lesson.verified
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
            workspace_id=self._workspace_id,
        )
        return result.canonical_id if result.saved else ""

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
