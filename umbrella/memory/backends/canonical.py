"""Canonical MemPalace durable backend."""

from pathlib import Path
from typing import Any

from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.tiers import Scope, Tier


class CanonicalMemoryBackend:
    def __init__(self, repo_root: Path, workspace_id: str = "") -> None:
        self._repo_root = repo_root
        self._workspace_id = workspace_id
        self._palace = MemPalace(repo_root, workspace_id or None)

    def retain_event(self, event: dict[str, Any]) -> str:
        return self._palace.add(
            store=str(event.get("store") or "palace.run"),
            content=str(event.get("content") or ""),
            tier=str(event.get("tier") or Tier.WARM),
            scope=str(event.get("scope") or Scope.RUN_SCOPED),
            tags=event.get("tags") or [],
            verified=bool(event.get("verified", False)),
            run_id=str(event.get("run_id") or ""),
            phase=str(event.get("phase") or ""),
            kind=str(event.get("kind") or ""),
        )

    def retain_lesson(self, lesson: dict[str, Any]) -> str:
        return self._palace.add(
            store="palace.lesson",
            content=str(lesson.get("content") or ""),
            tier=Tier.WARM,
            scope=Scope.CROSS_RUN_DURABLE,
            tags=lesson.get("tags") or ["lesson"],
            verified=bool(lesson.get("verified", True)),
            kind="lesson",
        )

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
