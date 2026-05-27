"""Runner/plan helpers built on contract subtask recovery."""

from typing import Any

from umbrella.contracts.models import ReviewContract
from umbrella.contracts.subtask_recovery import (
    review_superseded_by_recovery,
    subtask_passing_recovery_at,
)
from umbrella.phases.base import PhaseNode, PhasePlan, SubtaskCard

__all__ = [
    "review_superseded_by_recovery",
    "subtask_passing_recovery_at",
    "recovery_at_for_plan",
    "execute_node_from_plan",
    "all_execute_subtasks_done",
]


def _recovery_at_from_card(card: SubtaskCard) -> float:
    if card.status != "done":
        return 0.0
    completion = card.completion if isinstance(card.completion, dict) else None
    if not isinstance(completion, dict):
        return 0.0
    report = completion.get("verification_report")
    if isinstance(report, dict) and report.get("passed") is True:
        try:
            return float(completion.get("completed_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    contract = completion.get("completion_contract")
    if isinstance(contract, dict):
        nested = contract.get("verification_report")
        if isinstance(nested, dict) and nested.get("passed") is True:
            try:
                return float(completion.get("completed_at") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def recovery_at_for_plan(
    plan: PhasePlan | None,
    *,
    signal_rows: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    recovery = subtask_passing_recovery_at(signal_rows=signal_rows)
    if plan is None:
        return recovery
    execute = execute_node_from_plan(plan)
    if execute is None or not execute.subtasks:
        return recovery
    for card in execute.subtasks:
        stamp = _recovery_at_from_card(card)
        if stamp > 0:
            recovery[card.id] = max(recovery.get(card.id, 0.0), stamp)
    return recovery


def execute_node_from_plan(plan: PhasePlan | None) -> PhaseNode | None:
    if plan is None:
        return None
    node = plan.get_node("execute")
    if node is not None:
        return node
    return next((n for n in plan.nodes if n.manifest_id == "execute"), None)


def all_execute_subtasks_done(execute: PhaseNode | None) -> bool:
    if execute is None or not execute.subtasks:
        return False
    return all(card.status == "done" for card in execute.subtasks)
