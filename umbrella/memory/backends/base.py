"""Protocol for durable memory backends."""

from typing import Any, Protocol

from umbrella.memory.kernel.models import MemoryEvent


class BackendEventDict(Protocol):
    content: str
    metadata: dict[str, Any]


class MemoryQuery(Protocol):
    query: str
    limit: int


class DurableMemoryBackend(Protocol):
    def retain_event(self, event: MemoryEvent | dict[str, Any]) -> str: ...
    def retain_lesson(self, lesson: MemoryEvent | dict[str, Any]) -> str: ...
    def recall_evidence(self, query: MemoryQuery) -> list[dict[str, Any]]: ...
    def reflect_candidates(self, query: dict[str, Any]) -> list[dict[str, Any]]: ...
    def health(self) -> dict[str, Any]: ...
