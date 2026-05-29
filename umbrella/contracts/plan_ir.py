"""Compile typed phase-plan contracts into PlanIR."""


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


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


_PLAN_CHILD_KEYS = {"subtasks", "steps", "phases", "tasks", "items", "children"}
_PLAN_META_KEYS = ("plan_id", "run_id", "workspace_id")
_PROOF_TOP_LEVEL_KEYS = {
    "execution",
    "oracle",
    "scope",
    "anti_gaming",
    "harness",
    "harness_profile",
    "harness_id",
    "harness_options",
    "generated_test_contract",
    "required_capabilities",
    "human_claims",
    "evidence_refs",
    # Legacy planner output occasionally nested this here; execute still knows
    # how to lift it, but new plans should prefer the subtask-level field.
    "memory_scope",
}


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


def canonicalize_phase_plan(raw_plan: dict[str, Any]) -> dict[str, Any]:
    """Return a storage-safe phase plan with a canonical `subtasks` array."""

    if not isinstance(raw_plan, dict):
        return {}
    source = raw_plan.get("plan") if isinstance(raw_plan.get("plan"), dict) else raw_plan
    canonical: dict[str, Any] = {
        str(key): value
        for key, value in source.items()
        if str(key).lower() not in _PLAN_CHILD_KEYS and str(key) != "plan"
    }
    for key in _PLAN_META_KEYS:
        if key not in canonical and raw_plan.get(key) is not None:
            canonical[key] = raw_plan[key]
    canonical["subtasks"] = [dict(item) for item in _iter_subtask_payloads(source)]
    return canonical


def _proof_shape_issues(
    proof_payload: dict[str, Any],
    *,
    subtask_id: str,
) -> list[ContractIssue]:
    unknown = sorted(
        str(key) for key in proof_payload if str(key) not in _PROOF_TOP_LEVEL_KEYS
    )
    if not unknown:
        return []
    return [
        ContractIssue(
            code="invalid_plan_contract",
            severity="blocking",
            subtask_id=subtask_id,
            message=(
                "Unknown proof field(s) "
                + ", ".join(f"`{key}`" for key in unknown[:8])
                + "; use the typed proof contract fields exactly."
            ),
        )
    ]


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
        generated_contract = _dict_value(
            item.get("generated_test_contract")
            or (
                proof_payload.get("generated_test_contract")
                if isinstance(proof_payload, dict)
                else None
            )
        )
        if isinstance(proof_payload, dict):
            issues.extend(_proof_shape_issues(proof_payload, subtask_id=subtask_id))
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
                generated_test_contract=generated_contract,
                acceptance_claims=_tuple_str(
                    item.get("acceptance_claims") or item.get("acceptance_criteria")
                ),
                memory_scope=_dict_value(item.get("memory_scope")),
                allowed_tools=_tuple_str(item.get("allowed_tools") or item.get("tools")),
                allowed_skills=_tuple_str(item.get("allowed_skills") or item.get("skills")),
                codeptr_refs=_tuple_str(item.get("codeptr_refs")),
                mcp_refs=_tuple_str(item.get("mcp_refs")),
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
            notes=str(
                raw_plan.get("notes")
                or raw_plan.get("rationale")
                or raw_plan.get("summary")
                or ""
            ),
        ),
        issues,
    )
