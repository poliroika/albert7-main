"""Backend-neutral deep-agent adapter contract.

Umbrella owns phase state, evidence contracts, permissions, and phase exits.
Agent backends own proposal generation and trajectory recording.  This module
keeps that boundary explicit so Ouroboros, Hermes-style agents, or test doubles
can be swapped without changing phase semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from umbrella.contracts import PhaseExitDecision


PhaseContextInput = Mapping[str, Any]
ToolEnvelopeInput = Mapping[str, Any]
MemoryScopeInput = Mapping[str, Any]
EvidenceRequirementsInput = Mapping[str, Any]


class AgentAdapterContractError(RuntimeError):
    """Raised when an agent backend violates the Umbrella adapter contract."""


class PhaseTerminatedError(AgentAdapterContractError):
    """Raised when a backend tries to continue after a terminal phase exit."""


@dataclass(frozen=True)
class AgentMessage:
    role: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentToolSchema:
    name: str
    schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompiledAgentContext:
    messages: tuple[AgentMessage, ...] = ()
    tools: tuple[AgentToolSchema, ...] = ()
    evidence_requirements: Mapping[str, Any] = field(default_factory=dict)
    included_refs: tuple[str, ...] = ()
    omitted_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTurnResult:
    messages: tuple[AgentMessage, ...] = ()
    tool_calls: tuple[AgentToolCall, ...] = ()
    raw: Any = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrajectorySummary:
    text: str
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentAdapter(Protocol):
    """Backend-neutral contract every deep-agent runtime must satisfy."""

    def compile_context(
        self,
        phase_context: PhaseContextInput,
        tool_envelope: ToolEnvelopeInput,
        memory_scope: MemoryScopeInput,
        evidence_requirements: EvidenceRequirementsInput,
    ) -> CompiledAgentContext:
        ...

    def run_turn(self, context: CompiledAgentContext) -> AgentTurnResult:
        ...

    def parse_tool_calls(self, result: AgentTurnResult) -> tuple[AgentToolCall, ...]:
        ...

    def record_trajectory(self, result: AgentTurnResult) -> None:
        ...

    def interrupt(self, decision: PhaseExitDecision) -> None:
        ...

    def summarize_trajectory(self, required_evidence_refs: Sequence[str] = ()) -> TrajectorySummary:
        ...

    def handoff_artifacts(self) -> tuple[str, ...]:
        ...


@dataclass
class AgentAdapterController:
    """Small control wrapper that enforces Umbrella-owned phase exits."""

    adapter: AgentAdapter
    accepted_exit_decision: PhaseExitDecision | None = None

    def accept_phase_exit(self, decision: PhaseExitDecision) -> None:
        self.accepted_exit_decision = decision
        self.adapter.interrupt(decision)

    def run_turn(self, context: CompiledAgentContext) -> AgentTurnResult:
        if self.accepted_exit_decision is not None:
            decision = self.accepted_exit_decision
            raise PhaseTerminatedError(
                "agent backend cannot continue after accepted phase exit "
                f"{decision.phase_id}:{decision.task_id}:{decision.outcome}"
            )
        return self.adapter.run_turn(context)

    def summarize_trajectory(self, required_evidence_refs: Sequence[str] = ()) -> TrajectorySummary:
        summary = self.adapter.summarize_trajectory(required_evidence_refs)
        missing = tuple(ref for ref in required_evidence_refs if ref not in summary.evidence_refs)
        if missing:
            raise AgentAdapterContractError(
                "trajectory compression dropped required evidence refs: "
                + ", ".join(missing)
            )
        return summary


__all__ = [
    "AgentAdapter",
    "AgentAdapterContractError",
    "AgentAdapterController",
    "AgentMessage",
    "AgentToolCall",
    "AgentToolSchema",
    "AgentTurnResult",
    "CompiledAgentContext",
    "EvidenceRequirementsInput",
    "MemoryScopeInput",
    "PhaseContextInput",
    "PhaseTerminatedError",
    "ToolEnvelopeInput",
    "TrajectorySummary",
]
