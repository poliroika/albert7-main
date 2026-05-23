"""Canonical MemPalace durable backend."""

from pathlib import Path
from typing import Any

from umbrella.memory.kernel.models import memory_event_from_tool_write
from umbrella.memory.kernel.writer import write_memory_event
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.tiers import Scope, Tier


class CanonicalMemoryBackend:
    def __init__(self, repo_root: Path, workspace_id: str = "") -> None:
        self._repo_root = repo_root
        self._workspace_id = workspace_id
        self._palace = MemPalace(repo_root, workspace_id or None)

    def retain_event(self, event: dict[str, Any]) -> str:
        memory_event = memory_event_from_tool_write(
            content=str(event.get("content") or ""),
            memory_kind=str(event.get("kind") or "observation"),
            workspace_id=self._workspace_id,
            tags=event.get("tags") or [],
            scope=str(event.get("scope") or Scope.RUN_SCOPED),
            tier=str(event.get("tier") or Tier.WARM),
            phase_id=str(event.get("phase") or ""),
            run_id=str(event.get("run_id") or ""),
            verified=bool(event.get("verified", False)),
            palace_store=str(event.get("store") or "palace.run"),
        )
        result = write_memory_event(
            self._repo_root,
            memory_event,
            workspace_id=self._workspace_id,
        )
        return result.canonical_id if result.saved else ""

    def retain_lesson(self, lesson: dict[str, Any]) -> str:
        memory_event = memory_event_from_tool_write(
            content=str(lesson.get("content") or ""),
            memory_kind="lesson",
            workspace_id=self._workspace_id,
            tags=lesson.get("tags") or ["lesson"],
            scope=Scope.CROSS_RUN_DURABLE,
            tier=Tier.WARM,
            verified=bool(lesson.get("verified", True)),
            palace_store="palace.lesson",
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
