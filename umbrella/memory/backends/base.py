"""Protocol and transport-neutral models for durable memory backends."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


TrustLevel = Literal[
    "untrusted",
    "agent_reported",
    "workspace_verified",
    "public_verified",
    "supervisor_verified",
    "agent_claim",
]


@dataclass(frozen=True)
class DurableEvent:
    event_id: str
    kind: str
    content: str
    workspace_id: str = ""
    run_id: str = ""
    phase_id: str = ""
    subtask_id: str = ""
    agent: str = ""
    trust_level: TrustLevel | str = "workspace_verified"
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: str = ""


@dataclass(frozen=True)
class DurableLesson:
    lesson_id: str
    kind: str
    title: str
    content: str
    workspace_id: str = ""
    run_id: str = ""
    phase_id: str = ""
    trust_level: TrustLevel | str = "workspace_verified"
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryQuery:
    query: str
    workspace_id: str = ""
    run_id: str = ""
    phase_id: str = ""
    tags: list[str] = field(default_factory=list)
    max_tokens: int = 2048
    budget: str = "low"


@dataclass(frozen=True)
class MemoryHit:
    text: str
    source: str
    score: float | None = None
    kind: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReflectionQuery:
    question: str
    workspace_id: str = ""
    run_id: str = ""
    phase_id: str = "reflexion"
    tags: list[str] = field(default_factory=list)
    max_candidates: int = 3
    budget: str = "mid"


@dataclass(frozen=True)
class ReflectionCandidate:
    candidate_id: str
    kind: str
    title: str
    content: str
    confidence: float
    scope: str
    evidence_refs: list[dict[str, Any]]
    source_backend: str = "hindsight"
    metadata: dict[str, Any] = field(default_factory=dict)


class DurableMemoryBackend(Protocol):
    def health(self) -> dict[str, Any]: ...

    def ensure_banks(self, *, workspace_id: str = "") -> dict[str, Any]: ...

    def retain_event(self, event: DurableEvent | Any) -> dict[str, Any]: ...

    def retain_lesson(self, lesson: DurableLesson | Any) -> dict[str, Any]: ...

    def recall_evidence(self, query: MemoryQuery | dict[str, Any]) -> list[MemoryHit]: ...

    def reflect_candidates(
        self, query: ReflectionQuery | dict[str, Any]
    ) -> list[ReflectionCandidate]: ...
