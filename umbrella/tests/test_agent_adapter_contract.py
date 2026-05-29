from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pytest

from umbrella.contracts import PhaseExitDecision
from umbrella.orchestrator.agent_adapter import (
    AgentAdapter,
    AgentAdapterContractError,
    AgentAdapterController,
    AgentMessage,
    AgentTurnResult,
    CompiledAgentContext,
    PhaseTerminatedError,
    TrajectorySummary,
)


@dataclass
class _FakeAdapter:
    name: str
    summary_refs: tuple[str, ...] = ()
    interrupted_with: PhaseExitDecision | None = None
    turns: int = 0

    def compile_context(
        self,
        phase_context: Mapping[str, Any],
        tool_envelope: Mapping[str, Any],
        memory_scope: Mapping[str, Any],
        evidence_requirements: Mapping[str, Any],
    ) -> CompiledAgentContext:
        return CompiledAgentContext(
            messages=(AgentMessage(role="system", content=self.name),),
            evidence_requirements=evidence_requirements,
        )

    def run_turn(self, context: CompiledAgentContext) -> AgentTurnResult:
        self.turns += 1
        return AgentTurnResult(
            messages=(AgentMessage(role="assistant", content="proposal"),),
            evidence_refs=("verify:1",),
        )

    def parse_tool_calls(self, result: AgentTurnResult) -> tuple:
        return result.tool_calls

    def record_trajectory(self, result: AgentTurnResult) -> None:
        return None

    def interrupt(self, decision: PhaseExitDecision) -> None:
        self.interrupted_with = decision

    def summarize_trajectory(self, required_evidence_refs: Sequence[str] = ()) -> TrajectorySummary:
        return TrajectorySummary(text=f"{self.name} summary", evidence_refs=self.summary_refs)

    def handoff_artifacts(self) -> tuple[str, ...]:
        return ()


def _loop_back_decision() -> PhaseExitDecision:
    return PhaseExitDecision(
        phase_id="final_review",
        task_id="final_review",
        outcome="loop_back",
        target_phase="execute",
        evidence_refs=("verify:1",),
        source_tool_call_id="toolu_final",
    )


@pytest.mark.parametrize("adapter_name", ["ouroboros", "hermes"])
def test_agent_adapters_obey_terminal_phase_exit(adapter_name: str) -> None:
    adapter = _FakeAdapter(name=adapter_name)
    assert isinstance(adapter, AgentAdapter)

    controller = AgentAdapterController(adapter)
    context = adapter.compile_context({}, {}, {}, {"refs": ["verify:1"]})
    assert controller.run_turn(context).evidence_refs == ("verify:1",)

    decision = _loop_back_decision()
    controller.accept_phase_exit(decision)

    assert adapter.interrupted_with == decision
    with pytest.raises(PhaseTerminatedError):
        controller.run_turn(context)
    assert adapter.turns == 1


def test_trajectory_compression_preserves_required_evidence_refs() -> None:
    adapter = _FakeAdapter(name="hermes", summary_refs=("verify:1", "artifact:plan"))
    controller = AgentAdapterController(adapter)

    summary = controller.summarize_trajectory(["verify:1"])
    assert summary.evidence_refs == ("verify:1", "artifact:plan")


def test_trajectory_compression_cannot_drop_required_evidence_refs() -> None:
    adapter = _FakeAdapter(name="ouroboros", summary_refs=("artifact:plan",))
    controller = AgentAdapterController(adapter)

    with pytest.raises(AgentAdapterContractError, match="verify:1"):
        controller.summarize_trajectory(["verify:1"])
