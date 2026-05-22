"""Compile typed phase-plan contracts into PlanIR."""

from __future__ import annotations

from typing import Any

from umbrella.contracts.models import (
    ContractIssue,
    PlanIR,
    ProofSpec,
    SubtaskIR,
)


def _tuple_str(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


_PLAN_CHILD_KEYS = {"subtasks", "steps", "phases", "tasks", "items", "children"}


def _child_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _leaf_payloads(item: dict[str, Any]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for key, value in item.items():
        if str(key).lower() in _PLAN_CHILD_KEYS:
            for child in _child_dicts(value):
                children.extend(_leaf_payloads(child))
    if children:
        return children
    return [item]


def _iter_subtask_payloads(raw_plan: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("subtasks", "steps", "phases"):
        value = raw_plan.get(key)
        if isinstance(value, (list, dict)):
            leaves: list[dict[str, Any]] = []
            for item in _child_dicts(value):
                leaves.extend(_leaf_payloads(item))
            return leaves
    plan_obj = raw_plan.get("plan")
    if isinstance(plan_obj, dict):
        return _iter_subtask_payloads(plan_obj)
    return []


def compile_phase_plan(
    raw_plan: dict[str, Any], *, run_id: str = "", workspace_id: str = ""
) -> tuple[PlanIR | None, list[ContractIssue]]:
    """Compile the v1 phase plan into PlanIR."""

    if not isinstance(raw_plan, dict):
        return None, [
            ContractIssue(
                code="invalid_plan_contract",
                severity="blocking",
                message="Phase plan contract must be an object.",
            )
        ]
    issues: list[ContractIssue] = []
    subtasks: list[SubtaskIR] = []
    effective_run_id = str(raw_plan.get("run_id") or run_id or "")
    effective_workspace_id = str(raw_plan.get("workspace_id") or workspace_id or "")
    for idx, item in enumerate(_iter_subtask_payloads(raw_plan), start=1):
        subtask_id = str(
            item.get("id") or item.get("subtask_id") or item.get("name") or f"subtask_{idx}"
        )
        proof_payload = item.get("proof")
        proof = None
        if isinstance(proof_payload, dict):
            proof = ProofSpec.from_mapping(proof_payload)
        elif "success_test" in item:
            issues.append(
                ContractIssue(
                    code="legacy_contract_used",
                    severity="blocking",
                    subtask_id=subtask_id,
                    message="Contract v1 rejects legacy `success_test`; provide a typed `proof` object.",
                )
            )
        else:
            issues.append(
                ContractIssue(
                    code="missing_proof",
                    severity="blocking",
                    subtask_id=subtask_id,
                    message="Subtask must provide a typed `proof` object.",
                )
            )
        subtasks.append(
            SubtaskIR(
                id=subtask_id,
                title=str(item.get("title") or item.get("name") or subtask_id),
                goal=str(item.get("goal") or item.get("description") or ""),
                files_to_change=_tuple_str(
                    item.get("files_to_change")
                    or item.get("files_to_modify")
                    or item.get("files_affected")
                ),
                files_to_create=_tuple_str(item.get("files_to_create") or item.get("new_files")),
                dependencies=_tuple_str(item.get("dependencies")),
                proof=proof,
                acceptance_claims=_tuple_str(
                    item.get("acceptance_claims") or item.get("acceptance_criteria")
                ),
            )
        )
    if not subtasks:
        issues.append(
            ContractIssue(
                code="missing_plan_subtasks",
                severity="blocking",
                message="Phase plan must contain a non-empty typed subtask list.",
            )
        )
    return (
        PlanIR(
            run_id=effective_run_id,
            workspace_id=effective_workspace_id,
            subtasks=tuple(subtasks),
        ),
        issues,
    )
