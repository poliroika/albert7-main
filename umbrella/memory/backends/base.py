"""Protocol for durable memory backends."""

from typing import Any, Protocol


class MemoryEvent(Protocol):
    content: str
    metadata: dict[str, Any]


class MemoryQuery(Protocol):
    query: str
    limit: int


class DurableMemoryBackend(Protocol):
    def retain_event(self, event: dict[str, Any]) -> str: ...
    def retain_lesson(self, lesson: dict[str, Any]) -> str: ...
    def recall_evidence(self, query: MemoryQuery) -> list[dict[str, Any]]: ...
    def reflect_candidates(self, query: dict[str, Any]) -> list[dict[str, Any]]: ...
    def health(self) -> dict[str, Any]: ...
