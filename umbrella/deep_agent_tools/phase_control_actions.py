"""Phase-control action handlers exposed as tools."""

import copy
import logging
import subprocess

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools.phase_control_base import *
from umbrella.deep_agent_tools.phase_control_research import *
from umbrella.deep_agent_tools.phase_control_retry import (
    _final_review_e2e_gate,
    _phase_subtask_retry_escalation_block,
    _phase_subtask_retry_state,
    _phase_subtask_retry_watcher_review_payload,
    _supported_llm_alias_memory_claim_issue,
    _tool_row_is_successful_repair_write,
    _tool_row_result_payload,
)
from umbrella.deep_agent_tools.phase_contract_base import _json, _state_dir, _umbrella_phase_id
from umbrella.contracts import (
    CompletionContract,
    ContractBundle,
    ContractIssue,
    ContractValidator,
    EvidenceRef,
    PhaseExitDecision,
    ReviewContract,
    ReviewIssue,
    ResearchSummaryContract,
    VerificationReportRef,
    build_workspace_context,
    canonicalize_phase_plan,
    contract_issues_payload,
    compile_phase_plan,
    diff_hash,
    extract_failed_pytest_node_ids,
    generated_oracle_contract_issues,
    hash_value,
    json_ready,
    validate_completion_materialization,
    validate_done_subtasks_materialized,
    validate_review_contract,
    validate_verification_report_ref,
    workspace_hash,
)
from umbrella.contracts.runtime_probes import (
    effective_runtime_capabilities,
    load_runtime_capabilities,
)
from umbrella.contracts.environments import (
    DEFAULT_EXECUTION_ENVIRONMENT_ID,
    CapabilityBinding,
    classify_tcl_tk_status,
    find_capability_binding,
    persist_capability_binding,
    persist_environment_record,
    resolve_execution_environment,
)
from umbrella.contracts.contract_paths import (
    InvalidContractPath,
    normalize_contract_path,
    validate_delta_path,
)
from umbrella.deep_agent_tools.phase_control_retry import (
    _completion_llm_memory_claim_issue,
    _phase_subtask_completion_issue,
)
from umbrella.deep_agent_tools.phase_contract_policy import (
    _phase_plan_revision_contract_issues,
)

log = logging.getLogger(__name__)


def _phase_plan_execute_node(plan: dict[str, Any]) -> dict[str, Any] | None:
    nodes = plan.get("nodes")
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, dict) and str(node.get("id") or "") == "execute":
            return node
    for node in nodes:
        if isinstance(node, dict) and str(node.get("manifest_id") or "") == "execute":
            return node
    return None


_PHASE_PLAN_MERGE_LIST_KEYS = {
    "files_to_create",
    "files_to_change",
    "files_affected",
}
_PHASE_PLAN_LIST_FIELD_BASES = tuple(_PHASE_PLAN_MERGE_LIST_KEYS)


def _phase_plan_norm_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("/")


def _phase_plan_string_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, dict):
        values = [
            value.get(k)
            for k in ("path", "file_path", "file", "target", "value", "name", "id")
            if isinstance(value.get(k), str)
        ]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = []
        for item in value:
            values.extend(_phase_plan_string_items(item))
    else:
        values = [str(value)]

    out: list[str] = []
    for item in values:
        norm = str(item or "").replace("\\", "/").strip().lstrip("/")
        if norm:
            out.append(norm)
    return out


def _merge_phase_plan_string_list(current: Any, incoming: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in [*_phase_plan_string_items(current), *_phase_plan_string_items(incoming)]:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _phase_plan_list_patch_key_ops() -> dict[str, tuple[str, str]]:
    ops: dict[str, tuple[str, str]] = {}
    for base in _PHASE_PLAN_LIST_FIELD_BASES:
        ops[base] = (base, "replace")
        ops[f"merge_{base}"] = (base, "merge")
        ops[f"replace_{base}"] = (base, "replace")
        ops[f"set_{base}"] = (base, "set")
        ops[f"remove_{base}"] = (base, "remove")
    return ops


def _repo_root_from_phase_ctx(ctx: ToolContext) -> pathlib.Path:
    return pathlib.Path(
        getattr(ctx, "host_repo_root", None)
        or getattr(ctx, "repo_dir", None)
        or pathlib.Path(ctx.drive_root).parents[2]
    ).resolve()


def _workspace_root_from_phase_ctx(ctx: ToolContext, workspace_id: str = "") -> pathlib.Path:
    workspace = str(workspace_id or _workspace_id_from_drive(ctx) or "").strip()
    repo_root = _repo_root_from_phase_ctx(ctx)
    if workspace:
        return (repo_root / "workspaces" / workspace).resolve()
    return pathlib.Path(ctx.drive_root).resolve().parents[1]


def _contract_issue_message(prefix: str, issues: list[ContractIssue]) -> str:
    if not issues:
        return ""
    details = "; ".join(
        f"{issue.code}: {issue.message or issue.suggested_action or issue.code}"
        for issue in issues[:6]
    )
    return f"ERROR: {prefix}: {details}"


def _current_phase_node(ctx: ToolContext, plan: dict[str, Any]) -> dict[str, Any] | None:
    overlays = getattr(ctx, "context_overlays", None)
    phase_node = overlays.get("phase_node") if isinstance(overlays, dict) else None
    phase_id = str((phase_node or {}).get("id") or "").strip() if isinstance(phase_node, dict) else ""
    nodes = plan.get("nodes") if isinstance(plan, dict) else None
    if not isinstance(nodes, list):
        return None
    if phase_id:
        for node in nodes:
            if isinstance(node, dict) and str(node.get("id") or "") == phase_id:
                return node
    for node in nodes:
        if isinstance(node, dict) and str(node.get("status") or "") == "running":
            return node
    return None


def _phase_subtasks(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    subtasks = node.get("subtasks") if isinstance(node, dict) else None
    return [item for item in subtasks if isinstance(item, dict)] if isinstance(subtasks, list) else []


def _first_incomplete_subtask(subtasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for subtask in subtasks:
        if str(subtask.get("status") or "pending").lower() != "done":
            return subtask
    return None


def _phase_plan_subtask_contract_issues(
    ctx: ToolContext,
    plan: dict[str, Any],
) -> list[ContractIssue]:
    execute = _phase_plan_execute_node(plan)
    subtasks = execute.get("subtasks") if isinstance(execute, dict) else None
    plan_ir, compile_issues = compile_phase_plan(
        {"subtasks": subtasks if isinstance(subtasks, list) else []},
        run_id=_run_id(ctx),
        workspace_id=_workspace_id_from_drive(ctx),
    )
    workspace_id = _workspace_id_from_drive(ctx)
    context = build_workspace_context(
        repo_root=_repo_root_from_phase_ctx(ctx),
        workspace_root=_workspace_root_from_phase_ctx(ctx, workspace_id),
        workspace_id=workspace_id,
    )
    return ContractValidator.validate(
        ContractBundle(
            run_id=_run_id(ctx),
            workspace_id=workspace_id,
            plan=plan_ir,
            issues=tuple(compile_issues),
        ),
        context=context,
    )


def _completion_contract_payload_is_valid(
    ctx: ToolContext,
    *,
    completion_contract: Any,
    active_subtask: dict[str, Any] | None,
) -> tuple[bool, str]:
    if not isinstance(completion_contract, dict):
        return False, "missing completion_contract"
    try:
        typed = CompletionContract.from_mapping(completion_contract)
    except Exception as exc:
        return False, f"invalid completion_contract: {exc}"
    if typed.verification_report is None:
        return False, "completion_contract.verification_report is required"
    workspace_id = _workspace_id_from_drive(ctx)
    context = build_workspace_context(
        repo_root=_repo_root_from_phase_ctx(ctx),
        workspace_root=_workspace_root_from_phase_ctx(ctx, workspace_id),
        workspace_id=workspace_id,
        changed_files=typed.changed_files,
    )
    issues = ContractValidator.validate(
        ContractBundle(
            run_id=_run_id(ctx),
            workspace_id=workspace_id,
            completions=(typed,),
        ),
        context=context,
    )
    issues.extend(
        validate_completion_materialization(
            typed,
            active_subtask=active_subtask,
            workspace_root=str(context.workspace_root),
            raw_completion=completion_contract,
            phase=_phase_control_phase_id(ctx),
        )
    )
    blocking = [issue for issue in issues if issue.severity in {"error", "blocking", "human_required"}]
    if blocking:
        return False, _contract_issue_message("invalid completion_contract", blocking)
    return True, ""


def _mutated_subtask_proof_issue(
    target: dict[str, Any], patch_item: dict[str, Any], *, subtask_id: str
) -> str:
    if "proof" not in patch_item:
        return ""
    from umbrella.contracts import ProofSpec
    from umbrella.contracts.validators import validate_proof_spec

    merged = dict(target)
    merged["proof"] = _merge_phase_plan_proof_patch(
        target.get("proof"), patch_item.get("proof")
    )
    try:
        proof = ProofSpec.from_mapping(merged.get("proof"))
    except Exception:
        return f"subtask `{subtask_id}` proof patch is not a valid ProofSpec object."
    issues = validate_proof_spec(proof, subtask_id=subtask_id)
    for issue in issues:
        if issue.severity in {"error", "blocking", "human_required"}:
            return f"{issue.code}: {issue.message or issue.code}"
    try:
        previous = ProofSpec.from_mapping(target.get("proof") or {})
    except Exception:
        previous = None
    if previous is not None:
        narrowing_issue = _no_test_tampering_proof_narrowing_issue(
            previous, proof, subtask_id=subtask_id
        )
        if narrowing_issue:
            return narrowing_issue
    return ""


_PYTEST_ARG_VALUE_FLAGS = frozenset(
    {
        "-k",
        "--keyword",
        "-m",
        "-o",
        "--override-ini",
        "--tb",
        "--rootdir",
        "--confcutdir",
        "--basetemp",
        "--junitxml",
        "--cov",
        "--cov-report",
        "--maxfail",
        "--deselect",
        "--ignore",
        "--ignore-glob",
    }
)


def _normalize_pytest_target(value: Any) -> str:
    target = str(value or "").strip().strip("\"'")
    target = target.replace("\\", "/")
    while target.startswith("./"):
        target = target[2:]
    return target.rstrip("/")


def _pytest_command_targets(command: tuple[str, ...]) -> list[str]:
    tail_start: int | None = None
    for index, token in enumerate(command):
        if str(token).strip().lower() == "pytest":
            tail_start = index + 1
            break
    if tail_start is None:
        return []
    targets: list[str] = []
    skip_next = False
    for raw_token in command[tail_start:]:
        token = str(raw_token).strip()
        lowered = token.lower()
        if skip_next:
            skip_next = False
            continue
        if not token:
            continue
        if lowered in _PYTEST_ARG_VALUE_FLAGS:
            skip_next = True
            continue
        if lowered.startswith("-"):
            continue
        normalized = _normalize_pytest_target(token)
        if normalized:
            targets.append(normalized)
    return _dedupe_pytest_targets(targets)


def _dedupe_pytest_targets(targets: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw_target in targets:
        target = _normalize_pytest_target(raw_target)
        if not target or target in seen:
            continue
        seen.add(target)
        result.append(target)
    return result


def _pytest_target_base(target: str) -> str:
    return _normalize_pytest_target(target).split("::", 1)[0]


def _pytest_target_covers(original: str, candidate: str) -> bool:
    original = _normalize_pytest_target(original)
    candidate = _normalize_pytest_target(candidate)
    if not original or not candidate:
        return False
    if candidate == ".":
        return True
    if original == candidate:
        return True
    original_base = _pytest_target_base(original)
    candidate_base = _pytest_target_base(candidate)
    candidate_is_node = "::" in candidate
    if candidate_is_node:
        return False
    if "::" in original and candidate == original_base:
        return True
    return bool(original_base and original_base.startswith(candidate.rstrip("/") + "/"))


def _pytest_targets_not_covered(
    original_targets: list[str], candidate_targets: list[str]
) -> list[str]:
    originals = _dedupe_pytest_targets(original_targets)
    candidates = _dedupe_pytest_targets(candidate_targets)
    if not originals or not candidates:
        return []
    return [
        target
        for target in originals
        if not any(_pytest_target_covers(target, candidate) for candidate in candidates)
    ]


def _no_test_tampering_proof_narrowing_issue(
    previous: Any, updated: Any, *, subtask_id: str
) -> str:
    previous_properties = set(getattr(previous.oracle, "required_properties", ()) or ())
    updated_properties = set(getattr(updated.oracle, "required_properties", ()) or ())
    if "no_test_tampering" in previous_properties and "no_test_tampering" not in updated_properties:
        return (
            "no_test_tampering_removal_forbidden: accepted no_test_tampering "
            f"proof for subtask `{subtask_id}` cannot remove the anti-gaming "
            "property during execute."
        )
    if "no_test_tampering" not in (previous_properties | updated_properties):
        return ""
    previous_kind = str(getattr(previous.execution, "kind", "") or "")
    updated_kind = str(getattr(updated.execution, "kind", "") or "")
    if previous_kind == "pytest" and updated_kind != "pytest":
        updated_caps = {
            str(item).strip().lower()
            for item in (getattr(updated, "required_capabilities", ()) or ())
            if str(item).strip()
        }
        runtime_upgrade = (
            str(getattr(updated, "harness_profile", "") or "") == "desktop_gui_runtime"
            and updated_kind == "command"
            and "desktop_gui_runtime" in updated_caps
        )
        if not runtime_upgrade:
            return (
                "proof_kind_downgrade_forbidden: no_test_tampering pytest proof "
                f"for subtask `{subtask_id}` cannot be replaced with "
                f"`{updated_kind}` during execute; use an equally strong typed "
                "proof or a validated desktop_gui_runtime command contract."
            )
    if previous_kind != "pytest" or updated_kind != "pytest":
        return ""

    previous_scope = _dedupe_pytest_targets(list(previous.scope.pytest_targets))
    updated_scope = _dedupe_pytest_targets(list(updated.scope.pytest_targets))
    missing_scope = (
        previous_scope
        if previous_scope and not updated_scope
        else _pytest_targets_not_covered(previous_scope, updated_scope)
    )
    if missing_scope:
        return (
            "proof_selection_narrowing_forbidden: no_test_tampering pytest proof "
            f"for subtask `{subtask_id}` must preserve or broaden pytest_targets; "
            f"missing coverage for {missing_scope!r}."
        )

    previous_command = _pytest_command_targets(previous.execution.command)
    updated_command = _pytest_command_targets(updated.execution.command)
    previous_contract_targets = _dedupe_pytest_targets(previous_scope + previous_command)
    missing_command = _pytest_targets_not_covered(
        previous_contract_targets, updated_command
    )
    if missing_command:
        return (
            "proof_selection_narrowing_forbidden: no_test_tampering pytest proof "
            f"for subtask `{subtask_id}` cannot narrow the executable pytest "
            f"command target; missing coverage for {missing_command!r}."
        )
    return ""


def _merge_phase_plan_proof_patch(existing: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return patch
    merged: dict[str, Any] = (
        copy.deepcopy(existing) if isinstance(existing, dict) else {}
    )
    for key, value in patch.items():
        if key in {"remove_required_properties", "add_required_properties"}:
            property_target = (
                merged.setdefault("oracle", {})
                if isinstance(merged.get("oracle"), dict)
                or "oracle" in merged
                else merged
            )
            if not isinstance(property_target, dict):
                property_target = merged
            current = _phase_plan_string_items(
                property_target.get("required_properties")
            )
            if key == "remove_required_properties":
                remove_set = set(_phase_plan_string_items(value))
                property_target["required_properties"] = [
                    item for item in current if item not in remove_set
                ]
            else:
                property_target["required_properties"] = _merge_phase_plan_string_list(
                    current,
                    value,
                )
            continue
        if (
            isinstance(value, dict)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = _merge_phase_plan_proof_patch(merged.get(key), value)
        elif key == "required_properties":
            merged[key] = _phase_plan_string_items(value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


_PHASE_PLAN_SEMANTIC_CONTRACT_KEYS = frozenset(
    {
        "proof",
        "proof_contract",
        "generated_test_contract",
        "files_under_test",
        "test_oracle",
        "acceptance_criteria",
    }
)


_BLOCKED_CONTRACT_MIGRATION_KEY_ROOT = "contract" + "_" + "migration"
_BLOCKED_PLAN_REVISION_METADATA_KEYS = frozenset(
    {
        *(
            f"{stem}_{field}"
            for stem in (
                _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT,
                "test_" + _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT,
                "success_test_" + _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT,
            )
            for field in ("reason", "files")
        ),
        _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT,
        "test_" + _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT,
        "success_test_" + _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT,
        _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT + "_id",
        _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT + "_token",
        "plan" + "_" + "mutation" + "_" + "ticket",
        _BLOCKED_CONTRACT_MIGRATION_KEY_ROOT + "_ticket",
    }
)


_PLAN_REVISION_PATCH_KEYS = frozenset(
    {"plan_revision_patch", "required_deltas", "reason_code", "evidence_refs"}
)


def _has_semantic_contract_patch(item: dict[str, Any]) -> bool:
    return any(key in item for key in _PHASE_PLAN_SEMANTIC_CONTRACT_KEYS)


def _semantic_contract_snapshot(subtask: dict[str, Any]) -> Any:
    if not isinstance(subtask, dict):
        return {}
    return json_ready(
        {
            key: copy.deepcopy(subtask.get(key))
            for key in _PHASE_PLAN_SEMANTIC_CONTRACT_KEYS
            if key in subtask
        }
    )


def _semantic_path_values(snapshot: Any, path: str) -> list[Any]:
    path = str(path or "").strip()
    if not path:
        return []
    try:
        path = normalize_contract_path(path).path
    except InvalidContractPath:
        return []
    if path == "proof.oracle.required_properties":
        proof = snapshot.get("proof") if isinstance(snapshot, dict) else None
        values: list[Any] = []
        if isinstance(proof, dict):
            values.extend(_phase_plan_string_items(proof.get("required_properties")))
            oracle = proof.get("oracle")
            if isinstance(oracle, dict):
                values.extend(_phase_plan_string_items(oracle.get("required_properties")))
        proof_contract = (
            snapshot.get("proof_contract") if isinstance(snapshot, dict) else None
        )
        if isinstance(proof_contract, dict):
            values.extend(
                _phase_plan_string_items(proof_contract.get("required_properties"))
            )
            oracle = proof_contract.get("oracle")
            if isinstance(oracle, dict):
                values.extend(_phase_plan_string_items(oracle.get("required_properties")))
        return values

    current = snapshot
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current.get(part)
        else:
            return []
    if isinstance(current, list):
        return list(current)
    return [current]


def _plan_revision_required_deltas(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("required_deltas")
    if raw is None and isinstance(item.get("plan_revision_patch"), dict):
        raw = item["plan_revision_patch"].get("required_deltas")
    return [delta for delta in raw if isinstance(delta, dict)] if isinstance(raw, list) else []


def _normalize_plan_revision_required_deltas(
    item: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    deltas = _plan_revision_required_deltas(item)
    normalized: list[dict[str, Any]] = []
    for delta in deltas:
        try:
            normalized.append(validate_delta_path(delta))
        except InvalidContractPath as exc:
            return [], _invalid_contract_path_message(
                exc,
                source="PlanRevisionPatch.required_deltas",
            )
    return normalized, ""


def _required_deltas_satisfied(
    deltas: list[dict[str, Any]],
    after_snapshot: Any,
) -> str:
    for delta in deltas:
        op = str(delta.get("op") or "").strip()
        raw_path = str(delta.get("path") or "").strip()
        try:
            path = normalize_contract_path(raw_path).path
        except InvalidContractPath as exc:
            return f"required_delta has invalid contract path: {exc}"
        values = set(_phase_plan_string_items(delta.get("values")))
        if not path or not values:
            return "required_deltas entries require path and values"
        current = set(_phase_plan_string_items(_semantic_path_values(after_snapshot, path)))
        if op == "remove":
            remaining = sorted(current & values)
            if remaining:
                return f"required delta remove for {path} still present: {remaining!r}"
        elif op == "add":
            missing = sorted(values - current)
            if missing:
                return f"required delta add for {path} missing: {missing!r}"
        elif op == "replace":
            if current != values:
                return (
                    f"required delta replace for {path} not satisfied: "
                    f"expected {sorted(values)!r}, got {sorted(current)!r}"
                )
        else:
            return "required_deltas op must be remove, replace, or add"
    return ""


def _phase_subtask_materialization_issue(
    ctx: ToolContext,
    *,
    current_phase: dict[str, Any] | None,
    subtask_id: str,
) -> str:
    subtasks = _phase_subtasks(current_phase)
    first = _first_incomplete_subtask(subtasks)
    requested = str(subtask_id or "").strip()
    if first is None or str(first.get("id") or "").strip() != requested:
        return ""
    candidate = dict(first)
    candidate["status"] = "done"
    workspace_id = _workspace_id_from_drive(ctx)
    issues = validate_done_subtasks_materialized(
        subtasks=[candidate],
        workspace_root=str(_workspace_root_from_phase_ctx(ctx, workspace_id)),
        phase=_phase_control_phase_id(ctx),
    )
    if not issues:
        return ""
    return _contract_issue_message("mark_subtask_complete contract rejected", issues)


def _apply_phase_plan_subtask_patch(
    ctx: ToolContext, plan: dict[str, Any], subtask_patches: Any
) -> tuple[list[str], str | None]:
    if not isinstance(subtask_patches, list):
        return [], "patch.subtasks must be a list of subtask patch objects"
    execute = _phase_plan_execute_node(plan)
    if execute is None:
        return [], "execute phase not found in phase_plan.json"
    subtasks = execute.get("subtasks")
    if not isinstance(subtasks, list):
        return [], "execute phase has no mutable subtasks list"
    by_id = {
        str(item.get("id") or ""): item
        for item in subtasks
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    validated: list[tuple[dict[str, Any], dict[str, Any], str, Any, list[dict[str, Any]], bool]] = []
    seen_ids: set[str] = set()
    for item in subtask_patches:
        if not isinstance(item, dict):
            return [], "patch.subtasks entries must be objects"
        subtask_id = str(item.get("id") or "").strip()
        if not subtask_id:
            return [], "patch.subtasks entries must include id"
        if subtask_id in seen_ids:
            return [], f"duplicate patch.subtasks entry for subtask '{subtask_id}'"
        seen_ids.add(subtask_id)
        if "proof_contract" in item and "proof" not in item:
            item = {**item, "proof": item["proof_contract"]}
            item.pop("proof_contract", None)
        target = by_id.get(subtask_id)
        if target is None:
            return [], f"subtask '{subtask_id}' not found in execute phase"
        blocked_metadata_keys = sorted(
            key for key in item if key in _BLOCKED_PLAN_REVISION_METADATA_KEYS
        )
        if blocked_metadata_keys:
            return [], (
                "deprecated plan-revision metadata is not accepted in active "
                "plan mutation; use target_subtask_id plus a typed proof patch "
                "and optional required_deltas. Deprecated key(s): "
                + ", ".join(blocked_metadata_keys)
            )
        required_deltas, delta_error = _normalize_plan_revision_required_deltas(item)
        if delta_error:
            return [], delta_error
        semantic_patch = _has_semantic_contract_patch(item) or bool(required_deltas)
        before_snapshot = _semantic_contract_snapshot(target) if semantic_patch else {}
        requested_status = str(item.get("status") or "").strip().lower()
        if requested_status in {"done", "ok", "complete", "completed"}:
            contract_payload = item.get("completion_contract")
            if not isinstance(contract_payload, dict):
                completion = target.get("completion")
                if isinstance(completion, dict):
                    contract_payload = completion.get("completion_contract")
            ok, issue = _completion_contract_payload_is_valid(
                ctx,
                completion_contract=contract_payload,
                active_subtask=target,
            )
            if not ok:
                return [], (
                    "mutate_phase_plan cannot mark execute subtask "
                    f"`{subtask_id}` done without a valid verifier-backed "
                    f"CompletionContract: {issue}"
                )
        proof_issue = _mutated_subtask_proof_issue(
            target, item, subtask_id=subtask_id
        )
        if proof_issue:
            return [], proof_issue
        validated.append(
            (item, target, subtask_id, before_snapshot, required_deltas, semantic_patch)
        )

    list_patch_ops = _phase_plan_list_patch_key_ops()
    applied: list[str] = []
    for item, target, subtask_id, before_snapshot, required_deltas, semantic_patch in validated:
        item_touched = False
        for key, (base, op) in list_patch_ops.items():
            if key not in item:
                continue
            value = item[key]
            if op == "merge":
                target[base] = _merge_phase_plan_string_list(target.get(base), value)
                item_touched = True
            elif op in {"replace", "set"}:
                target[base] = _phase_plan_string_items(value)
                item_touched = True
            elif op == "remove":
                remove_set = set(_phase_plan_string_items(value))
                target[base] = [
                    path
                    for path in _phase_plan_string_items(target.get(base))
                    if path not in remove_set
                ]
                item_touched = True
        for key, value in item.items():
            if (
                key == "id"
                or key in list_patch_ops
                or key in _PLAN_REVISION_PATCH_KEYS
            ):
                continue
            if key in _PHASE_PLAN_MERGE_LIST_KEYS:
                target[key] = _merge_phase_plan_string_list(target.get(key), value)
            elif key == "proof":
                target[key] = _merge_phase_plan_proof_patch(target.get(key), value)
            else:
                target[key] = value
            item_touched = True
        if not item_touched and not semantic_patch:
            return [], (
                "plan revision patch for subtask "
                f"'{subtask_id}' did not apply any mutable contract fields; "
                "reason/evidence-only revisions are not accepted."
            )
        if semantic_patch:
            after = _semantic_contract_snapshot(target)
            if before_snapshot == after:
                return [], (
                    "plan revision patch for subtask "
                    f"'{subtask_id}' did not change the semantic proof/test/"
                    "oracle contract; metadata-only revisions are not accepted."
                )
            delta_issue = _required_deltas_satisfied(required_deltas, after)
            if delta_issue:
                return [], (
                    f"plan revision patch for subtask '{subtask_id}' "
                    f"incomplete: {delta_issue}"
                )
        applied.append(f"subtasks.{subtask_id}")
    return applied, None


def _apply_phase_plan_mutation(
    ctx: ToolContext,
    *,
    patch: dict[str, Any],
    target_subtask_id: str = "",
    subtask_id: str = "",
    tool_name: str = "mutate_phase_plan",
    signal_kind: str = "mutate_phase_plan",
) -> str:
    if stop := _stop_requested_message(ctx, tool_name):
        return stop
    if not isinstance(patch, dict):
        return f"ERROR: {tool_name} patch must be an object"
    if "subtask_id" in patch or "target_subtask_id" in patch:
        return (
            "ERROR: subtask_id is a selector, not a mutable patch field; pass "
            "it as top-level target_subtask_id or top-level subtask_id."
        )
    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found — cannot mutate"
    selector = (
        str(target_subtask_id or "").strip()
        or str(subtask_id or "").strip()
    )
    if selector:
        if "subtasks" in patch:
            return (
                "ERROR: target_subtask_id cannot be combined with patch.subtasks; "
                "pass subtask fields directly in patch."
            )
        if "proof_contract" in patch and "proof" not in patch:
            patch = {**patch, "proof": patch["proof_contract"]}
            patch.pop("proof_contract", None)
        patch = {"subtasks": [{"id": selector, **patch}]}
    blocked_metadata_keys = sorted(
        key for key in patch if key in _BLOCKED_PLAN_REVISION_METADATA_KEYS
    )
    if blocked_metadata_keys:
        return (
            "ERROR: deprecated plan-revision metadata is not accepted in "
            "active plan mutation; pass target_subtask_id and a typed patch "
            "that changes proof/files_under_test/acceptance_criteria. Deprecated "
            "key(s): "
            + ", ".join(blocked_metadata_keys)
        )
    applied: list[str] = []
    unsupported: list[str] = []
    for k, v in patch.items():
        if k == "subtasks":
            subtask_applied, issue = _apply_phase_plan_subtask_patch(ctx, plan, v)
            if issue:
                return f"ERROR: cannot mutate phase plan: {issue}"
            applied.extend(subtask_applied)
        elif k in ("nodes", "version"):
            plan[k] = v
            applied.append(k)
        else:
            unsupported.append(k)
    if unsupported:
        return (
            f"ERROR: unsupported {tool_name} patch keys: "
            + ", ".join(unsupported)
        )
    if not applied:
        return f"ERROR: {tool_name} patch did not apply any changes"
    touched_subtask_ids = {
        item.removeprefix("subtasks.")
        for item in applied
        if item.startswith("subtasks.")
    }
    subtask_patch_fields_by_id = {
        str(item.get("id") or "").strip(): item
        for item in patch.get("subtasks", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    try:
        from umbrella.deep_agent_tools.phase_contract_tools import _phase_plan_policy_issues

        policy_issues = _phase_plan_policy_issues(
            _phase_plan_policy_payload(
                plan,
                touched_subtask_ids=touched_subtask_ids,
                subtask_patch_fields_by_id=subtask_patch_fields_by_id,
            ),
            ctx=ctx,
        )
    except Exception:
        policy_issues = []
    if policy_issues:
        return (
            "ERROR: cannot mutate phase plan: mutation would violate workspace "
            "policy: "
            + "; ".join(policy_issues)
        )
    plan["version"] = int(plan.get("version") or 0) + 1
    if "edits_log" not in plan or not isinstance(plan.get("edits_log"), list):
        plan["edits_log"] = []
    plan["edits_log"].append(
        {
            "timestamp": time.time(),
            "actor": "worker",
            "patch": patch,
            "applied": applied,
        }
    )
    _write_phase_plan(ctx, plan)
    signal_payload = {
        "patch": patch,
        "applied": applied,
        "version": plan["version"],
    }
    signal_id = _write_control_signal(ctx, signal_kind, signal_payload)
    _mirror_phase_plan_mutation_to_palace(
        ctx,
        plan=plan,
        payload=signal_payload,
        signal_id=signal_id,
    )
    return f"PhasePlan mutated (version {plan['version']}): {applied} (signal: {signal_id})"


def _mutate_phase_plan(
    ctx: ToolContext,
    *,
    patch: dict[str, Any],
    target_subtask_id: str = "",
    subtask_id: str = "",
) -> str:
    return _apply_phase_plan_mutation(
        ctx,
        patch=patch,
        target_subtask_id=target_subtask_id,
        subtask_id=subtask_id,
        tool_name="mutate_phase_plan",
        signal_kind="mutate_phase_plan",
    )


def _apply_plan_revision_patch(
    ctx: ToolContext,
    *,
    target_subtask_id: str,
    patch: dict[str, Any],
    reason_code: str = "",
    deltas: list[dict[str, Any]] | None = None,
    required_deltas: list[dict[str, Any]] | None = None,
    evidence_refs: list[Any] | None = None,
) -> str:
    selector = str(target_subtask_id or "").strip()
    if not selector:
        return "ERROR: apply_plan_revision_patch requires target_subtask_id"
    if not isinstance(patch, dict):
        return "ERROR: apply_plan_revision_patch patch must be an object"
    normalized_patch = copy.deepcopy(patch)
    effective_deltas = (
        required_deltas
        if isinstance(required_deltas, list)
        else deltas if isinstance(deltas, list) else None
    )
    if effective_deltas is not None and "required_deltas" not in normalized_patch:
        normalized_patch["required_deltas"] = copy.deepcopy(effective_deltas)
    if reason_code and "reason_code" not in normalized_patch:
        normalized_patch["reason_code"] = str(reason_code)
    if evidence_refs is not None and "evidence_refs" not in normalized_patch:
        normalized_patch["evidence_refs"] = copy.deepcopy(evidence_refs)
    return _apply_phase_plan_mutation(
        ctx,
        patch=normalized_patch,
        target_subtask_id=selector,
        tool_name="apply_plan_revision_patch",
        signal_kind="apply_plan_revision_patch",
    )


def _request_scope_change(
    ctx: ToolContext,
    *,
    paths: list[str] | None = None,
    rationale: str = "",
) -> str:
    if stop := _stop_requested_message(ctx, "request_scope_change"):
        return stop
    path_list = [
        str(item).strip().replace("\\", "/").lstrip("/")
        for item in (paths or [])
        if str(item).strip()
    ]
    if not path_list:
        return "ERROR: request_scope_change requires at least one workspace-relative path."
    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found — cannot expand scope."
    execute_node = _phase_plan_execute_node(plan)
    if execute_node is None:
        return "ERROR: execute phase node missing from phase plan."
    subtasks = execute_node.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        return "ERROR: no execute subtasks in phase plan."
    active_index = -1
    active: dict[str, Any] | None = None
    for idx, item in enumerate(subtasks):
        if isinstance(item, dict) and str(item.get("status") or "") != "done":
            active_index = idx
            active = item
            break
    if active is None and isinstance(subtasks[0], dict):
        active_index = 0
        active = subtasks[0]
    if not isinstance(active, dict):
        return "ERROR: could not resolve active execute subtask."
    subtask_id = str(active.get("id") or "").strip()
    if not subtask_id:
        return "ERROR: active subtask has no id."
    future_owners: dict[str, str] = {}
    for future in subtasks[active_index + 1 :]:
        if not isinstance(future, dict) or str(future.get("status") or "") == "done":
            continue
        owner = str(future.get("id") or future.get("title") or "").strip()
        if not owner:
            continue
        for key in ("files_to_create", "files_to_change", "files_affected"):
            for future_path in _phase_plan_string_items(future.get(key)):
                norm = _phase_plan_norm_path(future_path)
                if norm:
                    future_owners.setdefault(norm, owner)
    requested_future_hits = {
        path: future_owners[path]
        for path in sorted({_phase_plan_norm_path(path) for path in path_list if _phase_plan_norm_path(path)})
        if path in future_owners
    }
    if requested_future_hits:
        hits = ", ".join(f"{path} -> {owner}" for path, owner in requested_future_hits.items())
        _clear_typed_action_gate(ctx)
        return (
            "Scope change not required for ordinary source edits: the requested "
            f"path(s) also appear on later subtask(s): {hits}. PhasePlan file "
            "ownership is advisory during execute. Read the file fresh and edit "
            "it directly if the active proof genuinely depends on it. Use "
            "apply_plan_revision_patch only when changing proof/oracle/"
            "contract shape, especially for test files."
        )
    existing = set(_phase_plan_string_items(active.get("files_to_create")))
    merged = sorted(existing | set(path_list))
    result = _mutate_phase_plan(
        ctx,
        patch={
            "subtasks": [
                {
                    "id": subtask_id,
                    "files_to_create": merged,
                    "scope_change_rationale": str(rationale or "").strip()[:500],
                }
            ]
        },
    )
    if result.startswith("ERROR:"):
        return result
    _clear_typed_action_gate(ctx)
    return result


def _mirror_phase_plan_mutation_to_palace(
    ctx: ToolContext,
    *,
    plan: dict[str, Any],
    payload: dict[str, Any],
    signal_id: str,
) -> None:
    """Mirror accepted plan mutations into hierarchical memory.

    The PhasePlan remains canonical. This mirror makes subtask-card changes
    visible to later execute retries, final review, and operator inspection
    without replaying raw JSONL logs.
    """

    try:
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            return
        subtask_patches = patch.get("subtasks")
        if not isinstance(subtask_patches, list):
            return
        workspace_id = (
            str(plan.get("workspace_id") or "").strip()
            or _workspace_id_from_drive(ctx)
            or str(_loop_state_view(ctx).get("active_workspace_id") or "").strip()
        )
        if not workspace_id:
            return
        run_id = str(plan.get("run_id") or _run_id(ctx) or "").strip()
        phase_id = str(
            (getattr(ctx, "context_overlays", {}) or {})
            .get("phase_node", {})
            .get("id")
            or "execute"
        ).strip()
        from umbrella.memory.palace.facade import MemPalace

        palace = MemPalace(pathlib.Path(getattr(ctx, "repo_dir", "")), workspace_id)
        for item in subtask_patches:
            if not isinstance(item, dict):
                continue
            subtask_id = str(item.get("id") or "").strip()
            if not subtask_id:
                continue
            memory_doc = {
                "artifact": "phase_plan_mutation",
                "run_id": run_id,
                "workspace_id": workspace_id,
                "phase_id": phase_id,
                "subtask_id": subtask_id,
                "applied": payload.get("applied") or [],
                "patch": item,
                "phase_plan_version": payload.get("version"),
                "signal_id": signal_id,
            }
            tags = ["phase_plan_mutation", "subtask_card"]
            palace.add(
                store="palace.subtask",
                content=json.dumps(memory_doc, ensure_ascii=False, indent=2),
                tier="hot",
                scope="subtask_scoped",
                tags=tags,
                phase=phase_id or "execute",
                subtask_id=subtask_id,
                run_id=run_id,
                verified=True,
                source_path=".memory/drive/state/phase_plan.json",
                extra={
                    "phase_plan_version": payload.get("version"),
                },
            )
    except Exception:
        pass


def _add_phase(ctx: ToolContext, *, after: str, manifest_id: str, description: str = "") -> str:
    if stop := _stop_requested_message(ctx, "add_phase"):
        return stop
    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found"
    nodes = plan.get("nodes", [])
    insert_after = next((i for i, n in enumerate(nodes) if n["id"] == after), -1)
    new_node = {
        "id": f"{manifest_id}_{int(time.time())}",
        "manifest_id": manifest_id,
        "status": "pending",
        "parent_phase_id": after,
    }
    if insert_after >= 0:
        nodes.insert(insert_after + 1, new_node)
    else:
        nodes.append(new_node)
    plan["nodes"] = nodes
    plan["version"] = plan.get("version", 0) + 1
    _write_phase_plan(ctx, plan)
    return f"Added phase '{manifest_id}' after '{after}' (node id: {new_node['id']})"


def _loop_back_to(ctx: ToolContext, *, phase: str, reason: str = "") -> str:
    if stop := _stop_requested_message(ctx, "loop_back_to"):
        return stop
    if policy_issue := _review_revision_policy_issue(ctx, reason=reason):
        return policy_issue
    current_phase = _phase_control_phase_id(ctx)
    if current_phase == "subtask_review" and str(phase or "").split(":", 1)[0] == "plan":
        return (
            "ERROR: subtask_review cannot loop_back_to plan directly. "
            "Use loop_back_target='execute' for implementation repair; plan "
            "revisions require a typed RecoveryDecision/PlanRevisionPatch from "
            "the control plane."
        )
    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found"
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, dict)]
    target_idx = next(
        (idx for idx, node in enumerate(nodes) if str(node.get("id") or "") == phase),
        -1,
    )
    if target_idx < 0:
        return f"ERROR: phase '{phase}' not found in plan"
    current = _current_phase_node(ctx, plan)
    current_idx = (
        next(
            (
                idx
                for idx, node in enumerate(nodes)
                if str(node.get("id") or "") == str(current.get("id") or "")
            ),
            -1,
        )
        if isinstance(current, dict)
        else -1
    )
    if current_idx >= 0 and target_idx > current_idx:
        current_id = str(nodes[current_idx].get("id") or "")
        return (
            "ERROR: loop_back_to can only target the current or an earlier "
            f"phase. Current phase is `{current_id}`; requested forward target "
            f"`{phase}`. Use the proper completion/review/verify signal instead "
            "of loop_back_to for forward progression."
        )
    target = nodes[target_idx]
    target["status"] = "pending"
    target["started_at"] = None
    target["ended_at"] = None
    plan["version"] = plan.get("version", 0) + 1
    _write_phase_plan(ctx, plan)
    signal_id = _write_control_signal(ctx, "loop_back_to", {"phase": phase, "reason": reason})
    return f"Looping back to phase '{phase}' (signal {signal_id})"


def _executable_plan_body_hash(payload: dict[str, Any]) -> str:
    body = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
    if not isinstance(body, dict):
        return ""
    return hash_value(body)


def _read_submitted_phase_plan_hash(ctx: ToolContext) -> str:
    path = _state_dir(ctx) / "phase_plan_submitted_latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return _executable_plan_body_hash(data) if isinstance(data, dict) else ""


def _invalidate_plan_review_after_phase_plan_submit(
    ctx: ToolContext,
    *,
    selected_plan_id: str,
    previous_submitted_hash: str = "",
    new_payload: dict[str, Any] | None = None,
) -> None:
    """A newly submitted plan must be reviewed before execute can consume it."""

    plan = _read_phase_plan(ctx)
    if not isinstance(plan, dict):
        return
    new_hash = _executable_plan_body_hash(new_payload or {})
    if (
        previous_submitted_hash
        and new_hash
        and previous_submitted_hash == new_hash
    ):
        for node in plan.get("nodes", []) or []:
            if not isinstance(node, dict):
                continue
            if str(node.get("id") or "") == "plan_review" and node.get("status") == "done":
                return
    changed = False
    downstream = {"plan_review", "execute", "final_review", "verify"}
    for node in plan.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        if node_id not in downstream:
            continue
        if node.get("status") != "pending":
            node["status"] = "pending"
            changed = True
        for key in ("started_at", "ended_at"):
            if node.get(key) is not None:
                node[key] = None
                changed = True
        if node_id == "plan_review" and node.get("overlay"):
            node["overlay"] = {}
            changed = True
    if not changed:
        return
    plan["version"] = plan.get("version", 0) + 1
    edits = plan.setdefault("edits_log", [])
    if isinstance(edits, list):
        edits.append(
            {
                "timestamp": time.time(),
                "actor": "submit_phase_plan",
                "patch": {
                    "invalidate_downstream_review_for_plan_id": selected_plan_id,
                    "reset": sorted(downstream),
                },
            }
        )
    _write_phase_plan(ctx, plan)


def _submit_research_summary(
    ctx: ToolContext,
    *,
    architecture_id: str,
    findings_ids: list[str],
    notes: str = "",
    coverage_status: str = "",
    source_scarcity_reason: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
) -> str:
    if stop := _stop_requested_message(ctx, "submit_research_summary"):
        return stop
    validation_issue = _research_summary_validation_issue(
        ctx,
        architecture_id=architecture_id,
        findings_ids=findings_ids,
        notes=notes,
        coverage_status=coverage_status,
    )
    if validation_issue:
        return validation_issue
    canonical_findings = _normalise_research_finding_ids(ctx, findings_ids)
    rows = _tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or ""))
    min_findings = _research_summary_min_valid_findings(ctx)
    coverage_report = _research_source_coverage_report(
        rows,
        accepted_count=len(canonical_findings),
        min_findings=min_findings,
    )
    effective_coverage_status = str(coverage_status or "").strip()
    if not effective_coverage_status:
        effective_coverage_status = (
            "complete" if len(canonical_findings) >= min_findings else "source_scarce"
        )
    if effective_coverage_status not in {"complete", "source_scarce", "blocked"}:
        return (
            "ERROR: submit_research_summary contract rejected: coverage_status "
            "must be complete/source_scarce/blocked"
        )
    contract = ResearchSummaryContract(
        architecture_id=str(architecture_id or "").strip(),
        findings_ids=tuple(canonical_findings),
        coverage_status=effective_coverage_status,  # type: ignore[arg-type]
        source_scarcity_reason=str(source_scarcity_reason or ""),
        evidence_refs=tuple(
            EvidenceRef.from_mapping(item)
            for item in (evidence_refs or [])
            if isinstance(item, dict)
        ),
    )
    research_issues: list[ContractIssue] = []
    if not contract.architecture_id:
        research_issues.append(
            ContractIssue(
                code="missing_research_architecture",
                severity="blocking",
                phase="research",
                message="ResearchSummaryContract.architecture_id is required.",
            )
        )
    if len(contract.findings_ids) < min_findings and contract.coverage_status != "source_scarce":
        research_issues.append(
            ContractIssue(
                code="insufficient_research_evidence",
                severity="blocking",
                phase="research",
                message=(
                    f"Research summary has {len(contract.findings_ids)}/"
                    f"{min_findings} accepted findings."
                ),
            )
        )
    if contract.coverage_status == "source_scarce" and not contract.source_scarcity_reason:
        research_issues.append(
            ContractIssue(
                code="insufficient_research_evidence",
                severity="blocking",
                phase="research",
                message="source_scarce requires source_scarcity_reason.",
            )
        )
    if research_issues:
        return _contract_issue_message(
            "submit_research_summary contract rejected", research_issues
        )
    _record_research_summary_artifact(
        ctx,
        architecture_id=architecture_id,
        findings_ids=canonical_findings,
        notes=notes,
        coverage_status=effective_coverage_status,
        coverage_report=coverage_report,
        source_scarcity_reason=source_scarcity_reason,
    )
    signal_id = _write_control_signal(ctx, "submit_research_summary", {
        "architecture_id": architecture_id,
        "findings_ids": canonical_findings,
        "notes": notes,
        "coverage_status": effective_coverage_status,
        "source_scarcity_reason": source_scarcity_reason,
        "evidence_refs": json_ready(contract.evidence_refs),
    })
    return f"OK: Research summary submitted (architecture: {architecture_id}, findings: {len(canonical_findings)}, signal: {signal_id})"


def _invalid_contract_path_message(exc: InvalidContractPath, *, source: str) -> str:
    return (
        "ERROR: invalid_recovery_contract: "
        f"{source} contains invalid contract path `{exc.raw_path}`. "
        f"{exc.message}"
        + (f"; canonical suggestion: `{exc.suggestion}`" if exc.suggestion else "")
    )


_SCOPE_REVIEW_CODES = frozenset({"proof_scope_mismatch", "scope_mismatch"})
_PROOF_STRENGTH_REVIEW_CODES = frozenset({"weak_proof", "manual_proof"})
_PROOF_INFRA_REVIEW_CODES = frozenset(
    {
        "headless_proof_uses_real_gui_root",
        "proof_execution_infra",
        "capability_probe_environment_mismatch",
        "dependency_provision_required",
    }
)
_FILE_TARGET_REVIEW_CODES = frozenset(
    {"missing_proof", "unavailable_proof_target"}
)


def _submitted_plan_subtasks_by_id(ctx: ToolContext) -> dict[str, dict[str, Any]]:
    payload = _submitted_or_latest_phase_plan_payload(ctx)
    raw_plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
    subtasks = raw_plan.get("subtasks") if isinstance(raw_plan, dict) else None
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(subtasks, list):
        for subtask in subtasks:
            if not isinstance(subtask, dict):
                continue
            subtask_id = str(
                subtask.get("id") or subtask.get("subtask_id") or ""
            ).strip()
            if subtask_id:
                by_id[subtask_id] = subtask
    return by_id


def _review_issue_target_id(issue: dict[str, Any]) -> str:
    return str(
        issue.get("target_subtask_id") or issue.get("subtask_id") or ""
    ).strip()


def _review_change_issue_candidates(
    *,
    path: str,
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if path.startswith("proof.scope."):
        codes = _SCOPE_REVIEW_CODES
    elif path in {"files_to_create", "files_to_change", "files_affected"}:
        codes = _FILE_TARGET_REVIEW_CODES
    elif path.startswith("proof."):
        codes = _PROOF_STRENGTH_REVIEW_CODES | _PROOF_INFRA_REVIEW_CODES
    else:
        codes = frozenset()
    return [
        issue
        for issue in issues
        if str(issue.get("code") or "").strip() in codes
        and _review_issue_target_id(issue)
    ]


def _infer_review_change_target(
    *,
    change: dict[str, Any],
    path: str,
    issues: list[dict[str, Any]],
) -> str:
    explicit = str(
        change.get("target_subtask_id")
        or change.get("subtask_id")
        or change.get("target_id")
        or ""
    ).strip()
    if explicit:
        return explicit

    candidates = _review_change_issue_candidates(path=path, issues=issues)
    candidate_targets = list(
        dict.fromkeys(_review_issue_target_id(issue) for issue in candidates)
    )
    if len(candidate_targets) == 1:
        return candidate_targets[0]

    all_targets = list(
        dict.fromkeys(
            _review_issue_target_id(issue)
            for issue in issues
            if _review_issue_target_id(issue)
        )
    )
    if len(all_targets) == 1:
        return all_targets[0]

    return ""


def _review_path_values(root: dict[str, Any], path: str) -> list[Any]:
    try:
        canonical_path = normalize_contract_path(path).path
    except InvalidContractPath:
        return []
    values: list[Any] = [root]
    for token in [part for part in canonical_path.split(".") if part]:
        next_values: list[Any] = []
        for value in values:
            if isinstance(value, dict) and token in value:
                next_values.append(value[token])
        values = next_values
        if not values:
            break
    return values


def _previous_review_path_value(
    *,
    subtasks_by_id: dict[str, dict[str, Any]],
    target_subtask_id: str,
    path: str,
) -> Any:
    subtask = subtasks_by_id.get(target_subtask_id)
    if not isinstance(subtask, dict):
        return None
    values = _review_path_values(subtask, path)
    if len(values) == 1:
        return copy.deepcopy(values[0])
    if values:
        return copy.deepcopy(values)
    return None


def _with_semantic_previous_value(
    *,
    change: dict[str, Any],
    subtasks_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if str(change.get("op") or "").strip() != "semantic_diff":
        return change
    if "previous_value" in change:
        return change
    target_subtask_id = str(change.get("target_subtask_id") or "").strip()
    path = str(change.get("path") or "").strip()
    previous = _previous_review_path_value(
        subtasks_by_id=subtasks_by_id,
        target_subtask_id=target_subtask_id,
        path=path,
    )
    if previous is not None:
        change["previous_value"] = json_ready(previous)
    return change


def _derived_review_required_changes(
    *,
    issues: list[dict[str, Any]],
    subtasks_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "").strip().lower()
        if severity not in {"blocking", "error"}:
            continue
        code = str(issue.get("code") or "").strip()
        target_subtask_id = _review_issue_target_id(issue)
        if not target_subtask_id:
            continue
        if code in _SCOPE_REVIEW_CODES:
            path = "proof.scope"
        elif code in _PROOF_STRENGTH_REVIEW_CODES:
            path = "proof"
        elif code == "headless_proof_uses_real_gui_root":
            path = "proof.scope.pytest_targets"
        elif code in _PROOF_INFRA_REVIEW_CODES:
            path = "proof.execution"
        else:
            continue
        change = {
            "id": f"{code}-{target_subtask_id}",
            "target_subtask_id": target_subtask_id,
            "severity": "blocking",
            "reason_code": code,
            "source": "ReviewIssue",
            "path": path,
            "op": "semantic_diff",
        }
        if isinstance(issue.get("evidence_refs"), list):
            change["evidence_refs"] = issue.get("evidence_refs")
        changes.append(
            _with_semantic_previous_value(
                change=change,
                subtasks_by_id=subtasks_by_id,
            )
        )
    return changes


def _normalize_required_plan_change(
    *,
    change: dict[str, Any],
    issues: list[dict[str, Any]],
    subtasks_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    item = dict(change)
    raw_path = str(item.get("path") or item.get("field") or "").strip()
    if raw_path:
        item["path"] = normalize_contract_path(raw_path).path
    path = str(item.get("path") or "").strip()
    target_subtask_id = _infer_review_change_target(
        change=item,
        path=path,
        issues=issues,
    )
    if target_subtask_id:
        item["target_subtask_id"] = target_subtask_id
    op = str(item.get("op") or item.get("action") or "").strip()
    if op == "replace_applied" and "old_value" not in item and "new_value" not in item:
        item["op"] = "semantic_diff"
        item.pop("value", None)
        item.pop("values", None)
    item = _with_semantic_previous_value(
        change=item,
        subtasks_by_id=subtasks_by_id,
    )
    return item


def _append_unique_review_change(
    changes: list[Any],
    change: dict[str, Any],
) -> None:
    key = json.dumps(json_ready(change), sort_keys=True, ensure_ascii=False)
    for existing in changes:
        if not isinstance(existing, dict):
            continue
        if json.dumps(json_ready(existing), sort_keys=True, ensure_ascii=False) == key:
            return
    changes.append(change)


def _normalize_review_control_paths(
    *,
    ctx: ToolContext,
    issues: list[dict[str, Any]],
    required_plan_changes: list[Any] | None,
) -> tuple[dict[str, Any], str]:
    subtasks_by_id = _submitted_plan_subtasks_by_id(ctx)
    normalized_issues: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        item = dict(issue)
        for key in ("contract_path", "target_path"):
            raw_path = str(item.get(key) or "").strip()
            if not raw_path:
                continue
            try:
                item[key] = normalize_contract_path(raw_path).path
            except InvalidContractPath as exc:
                return {}, _invalid_contract_path_message(
                    exc,
                    source=f"ReviewIssue.{key}",
                )
        raw_deltas = item.get("required_deltas")
        if isinstance(raw_deltas, list):
            normalized_deltas: list[dict[str, Any]] = []
            for delta in raw_deltas:
                if not isinstance(delta, dict):
                    continue
                try:
                    normalized_deltas.append(validate_delta_path(delta))
                except InvalidContractPath as exc:
                    return {}, _invalid_contract_path_message(
                        exc,
                        source="ReviewIssue.required_deltas",
                    )
            item["required_deltas"] = normalized_deltas
        normalized_issues.append(item)

    normalized_changes: list[Any] = []
    for change in _derived_review_required_changes(
        issues=normalized_issues,
        subtasks_by_id=subtasks_by_id,
    ):
        _append_unique_review_change(normalized_changes, change)
    for change in required_plan_changes or []:
        if not isinstance(change, dict):
            normalized_changes.append(change)
            continue
        try:
            item = _normalize_required_plan_change(
                change=change,
                issues=normalized_issues,
                subtasks_by_id=subtasks_by_id,
            )
        except InvalidContractPath as exc:
            return {}, _invalid_contract_path_message(
                exc,
                source="required_plan_changes",
            )
        _append_unique_review_change(normalized_changes, item)
    return {
        "issues": normalized_issues,
        "required_plan_changes": normalized_changes,
    }, ""


def _submit_micro_review(
    ctx: ToolContext,
    *,
    verdict: str,
    issues: list[dict[str, Any]] | None = None,
    revisions: list[str] | None = None,
    loop_back_target: str = "",
    notes: str = "",
    coverage: dict[str, Any] | None = None,
    required_plan_changes: list[Any] | None = None,
) -> str:
    if stop := _stop_requested_message(ctx, "submit_micro_review"):
        return stop
    effective_issues = list(issues) if isinstance(issues, list) else None
    if revisions:
        migrated = [
            {
                "code": "policy_violation",
                "severity": "blocking",
                "message": str(item).strip(),
            }
            for item in revisions
            if str(item).strip()
        ]
        if effective_issues is None:
            effective_issues = migrated
        elif migrated:
            effective_issues = [*effective_issues, *migrated]
    if effective_issues is None:
        effective_issues = []
    normalized_review, path_issue = _normalize_review_control_paths(
        ctx=ctx,
        issues=effective_issues,
        required_plan_changes=required_plan_changes,
    )
    if path_issue:
        return path_issue
    effective_issues = normalized_review["issues"]
    required_plan_changes = normalized_review["required_plan_changes"]
    if not effective_issues and (feedback_issue := _micro_review_feedback_issue(
        verdict=verdict,
        revisions=revisions,
        notes=notes,
    )):
        return feedback_issue
    if policy_issue := _review_revision_policy_issue(
        ctx,
        verdict=verdict,
        revisions=revisions,
        notes=notes,
    ):
        return policy_issue
    plan_review_issue = _plan_review_validation_issue(
        ctx,
        verdict=verdict,
        issues=effective_issues,
        revisions=revisions,
        notes=notes,
        required_plan_changes=required_plan_changes,
    )
    if plan_review_issue:
        return plan_review_issue
    contract = ReviewContract.from_mapping(
        {
            "verdict": verdict,
            "issues": effective_issues,
            "loop_back_target": loop_back_target,
            "notes": notes,
            "coverage": coverage,
            "required_plan_changes": required_plan_changes or [],
        }
    )
    contract_issues = validate_review_contract(
        contract, phase=_phase_control_phase_id(ctx)
    )
    if contract_issues:
        return _contract_issue_message("submit_micro_review contract rejected", contract_issues)
    research_review_issue = _research_review_validation_issue(
        ctx,
        verdict=verdict,
        revisions=revisions,
        notes=notes,
    )
    if research_review_issue:
        return research_review_issue
    current_finding_issue = _research_review_current_finding_revise_issue(
        ctx,
        verdict=verdict,
        issues=list(contract.issues),
        notes=notes,
    )
    if current_finding_issue:
        return current_finding_issue
    if plan_review_ok_issue := _plan_review_ok_artifact_issue(ctx, verdict=verdict):
        return plan_review_ok_issue
    if plan_review_policy_issue := _plan_review_ok_policy_issue(ctx, verdict=verdict):
        return plan_review_policy_issue
    signal_id = _write_control_signal(
        ctx,
        "submit_micro_review",
        json_ready(contract),
    )
    if contract.verdict == "revise" and contract.loop_back_target:
        _write_control_signal(
            ctx,
            "loop_back_to",
            {
                "phase": contract.loop_back_target,
                "reason": (
                    contract.issues[0].message
                    if contract.issues
                    else "typed review requested revision"
                ),
                "source": "submit_micro_review",
            },
        )
    return f"OK: Micro-review submitted: {verdict} (signal: {signal_id})"


def _submit_phase_plan(ctx: ToolContext, *, plan_id: str = "", notes: str = "") -> str:
    if stop := _stop_requested_message(ctx, "submit_phase_plan"):
        return stop
    selected_plan_id = str(plan_id or "").strip() or _latest_phase_plan_id(ctx)
    if not selected_plan_id:
        return (
            "ERROR: submit_phase_plan needs a plan_id or an existing "
            "phase_plan_proposal_latest.json from propose_phase_plan"
        )
    payload = _phase_plan_payload_by_id(ctx, selected_plan_id)
    if not payload:
        return (
            "ERROR: submit_phase_plan can only submit a plan_id from an "
            "accepted propose_phase_plan artifact in this run. "
            f"Unknown plan_id `{selected_plan_id}`; call propose_phase_plan "
            "with the full current executable plan and submit the returned "
            "plan_id."
        )
    if payload:
        plan_payload = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        plan_payload = canonicalize_phase_plan(plan_payload)
        payload = {**payload, "plan": plan_payload}
        contradiction = _negative_claim_contradiction_issue(
            ctx,
            rows=_tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or "")),
            text=json.dumps(
                {"plan": plan_payload, "notes": payload.get("notes") or ""},
                ensure_ascii=False,
            ),
            label="phase plan",
        )
        if contradiction:
            return contradiction + " Submit a corrected phase plan before selecting it."
        revision_issues = _phase_plan_revision_contract_issues(ctx, plan_payload)
        if revision_issues:
            return "ERROR: " + "; ".join(revision_issues)
        plan_ir, compile_issues = compile_phase_plan(
            plan_payload,
            run_id=_run_id(ctx),
            workspace_id=_workspace_id_from_drive(ctx),
        )
        bundle = ContractBundle(
            run_id=_run_id(ctx),
            workspace_id=_workspace_id_from_drive(ctx),
            plan=plan_ir,
            issues=tuple(compile_issues),
        )
        context = build_workspace_context(
            repo_root=_repo_root_from_phase_ctx(ctx),
            workspace_root=_workspace_root_from_phase_ctx(
                ctx, _workspace_id_from_drive(ctx)
            ),
            workspace_id=_workspace_id_from_drive(ctx),
        )
        drive_root = (
            pathlib.Path(ctx.drive_root)
            if getattr(ctx, "drive_root", None)
            else None
        )
        if handoff_issue := _capability_declaration_handoff_issue(ctx):
            return handoff_issue
        contract_issues = ContractValidator.validate(
            bundle,
            context=context,
            runtime_capabilities=(
                effective_runtime_capabilities(drive_root)
                if drive_root is not None
                else {}
            ),
            drive_root=drive_root,
        )
        if contract_issues:
            return _contract_issue_message(
                "phase plan contract rejected", contract_issues
            )
    previous_hash = _read_submitted_phase_plan_hash(ctx)
    _record_submitted_phase_plan_artifact(
        ctx,
        payload=payload,
        plan_id=selected_plan_id,
        notes=notes,
    )
    _invalidate_plan_review_after_phase_plan_submit(
        ctx,
        selected_plan_id=selected_plan_id,
        previous_submitted_hash=previous_hash,
        new_payload=payload if isinstance(payload, dict) else None,
    )
    signal_id = _write_control_signal(ctx, "submit_phase_plan", {
        "plan_id": selected_plan_id,
        "submitted_artifact": ".memory/drive/state/phase_plan_submitted_latest.json",
        "notes": notes,
    })
    return f"OK: Phase plan submitted: {selected_plan_id} (signal: {signal_id})"


def _submit_final_review(
    ctx: ToolContext,
    *,
    outcome: str,
    notes: str = "",
    issues: list[dict[str, Any]] | None = None,
    required_changes: list[dict[str, Any]] | None = None,
) -> str:
    if stop := _stop_requested_message(ctx, "submit_final_review"):
        return stop
    if outcome not in ("ok", "loop_back"):
        return f"ERROR: outcome must be ok or loop_back, got '{outcome}'"
    if outcome == "ok":
        gate = _final_review_e2e_gate(ctx)
        if gate:
            return gate
    target_phase = "execute" if outcome == "loop_back" else None
    typed_issues = tuple(
        ReviewIssue.from_mapping(item)
        for item in (issues or [])
        if isinstance(item, dict)
    )
    typed_required_changes = tuple(
        dict(item) for item in (required_changes or []) if isinstance(item, dict)
    )
    signal_payload = {
        "outcome": outcome,
        "notes": notes,
        "issues": [json_ready(item) for item in typed_issues],
        "required_changes": list(typed_required_changes),
    }
    signal_id = _write_control_signal(ctx, "submit_final_review", signal_payload)
    try:
        view = getattr(ctx, "loop_state_view", {}) or {}
        decision = PhaseExitDecision(
            phase_id=_phase_control_phase_id(ctx),
            task_id=str(getattr(ctx, "task_id", "") or ""),
            outcome=outcome,
            target_phase=target_phase,
            issues=typed_issues,
            required_changes=typed_required_changes,
            verification_summary=str(view.get("verification_summary") or ""),
            source_tool_call_id=signal_id,
        )
        (_state_dir(ctx) / "phase_exit_decision_latest.json").write_text(
            _json(json_ready(decision)),
            encoding="utf-8",
        )
    except Exception:
        pass
    return f"OK: Final review submitted: {outcome} (signal: {signal_id})"


def _submit_verification(
    ctx: ToolContext,
    *,
    status: str,
    verification_report_ref: dict[str, Any] | None = None,
    details: str = "",
) -> str:
    if stop := _stop_requested_message(ctx, "submit_verification"):
        return stop
    if status not in ("pass", "fail"):
        return f"ERROR: status must be pass or fail, got '{status}'"
    report_ref_payload: dict[str, Any] | None = None
    if status == "pass":
        if _mentions_unresolved_pass_blocker(details):
            return (
                "ERROR: submit_verification(status='pass') mentions unresolved "
                "blockers or limitations. Loop back or submit status='fail' "
                "until the blocker is actually resolved."
            )
        if not isinstance(verification_report_ref, dict):
            return (
                "ERROR: submit_verification(status='pass') requires "
                "verification_report_ref with report/hash/workspace/diff data."
            )
        report_ref = VerificationReportRef.from_mapping(verification_report_ref)
        workspace_id = _workspace_id_from_drive(ctx)
        context = build_workspace_context(
            repo_root=_repo_root_from_phase_ctx(ctx),
            workspace_root=_workspace_root_from_phase_ctx(ctx, workspace_id),
            workspace_id=workspace_id,
        )
        contract_issues = validate_verification_report_ref(
            report_ref,
            context=context,
            phase=_phase_control_phase_id(ctx) or "verify",
        )
        if contract_issues:
            return _contract_issue_message(
                "submit_verification contract rejected", contract_issues
            )
        report_ref_payload = json_ready(report_ref)
    signal_payload = {
        "status": status,
        "details": details,
    }
    if report_ref_payload is not None:
        signal_payload["verification_report_ref"] = report_ref_payload
    signal_id = _write_control_signal(ctx, "submit_verification", signal_payload)
    return f"OK: Verification submitted: {status} (signal: {signal_id})"


def _submit_reflection(
    ctx: ToolContext,
    *,
    text: str,
    applies_to_phase: str,
    applies_to_subtask: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
    proposed_bkb_rules: list[dict[str, Any]] | None = None,
) -> str:
    if stop := _stop_requested_message(ctx, "submit_reflection"):
        return stop
    if not evidence_refs:
        return "ERROR: evidence_refs must be non-empty and typed EvidenceRef objects"
    if evidence_refs and isinstance(evidence_refs[0], str):
        return "ERROR: evidence_refs must be typed EvidenceRef objects, not strings"
    typed_refs = tuple(
        EvidenceRef.from_mapping(item)
        for item in evidence_refs
        if isinstance(item, dict)
    )
    if len(typed_refs) != len(evidence_refs):
        return "ERROR: evidence_refs must be typed EvidenceRef objects, not strings"
    signal_payload: dict[str, Any] = {
        "text": text,
        "applies_to_phase": applies_to_phase,
        "applies_to_subtask": applies_to_subtask,
        "evidence_refs": json_ready(typed_refs),
    }
    if proposed_bkb_rules:
        patch_id = f"bkb_patch_{uuid.uuid4().hex[:12]}"
        patch_actor = "supervisor"
        producers = {ref.produced_by for ref in typed_refs}
        if producers == {"verifier"}:
            patch_actor = "verifier"
        patch_doc = {
            "patch_id": patch_id,
            "status": "candidate",
            "actor": patch_actor,
            "rules": proposed_bkb_rules,
            "source_evidence": json_ready(typed_refs),
            "run_id": _run_id(ctx),
            "phase_id": _umbrella_phase_id(ctx) or applies_to_phase,
            "workspace_id": _workspace_id_from_drive(ctx),
        }
        patch_path = _state_dir(ctx) / "proposed_bkb_patch.json"
        patch_path.write_text(
            json.dumps(patch_doc, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        signal_payload["proposed_bkb_patch_id"] = patch_id
    signal_id = _write_control_signal(ctx, "submit_reflection", signal_payload)
    return f"OK: Reflection submitted for phase '{applies_to_phase}' with {len(typed_refs)} citations (signal: {signal_id})"


def _accept_bkb_proposal(
    ctx: ToolContext,
    *,
    patch_id: str = "",
    workspace_id: str = "",
) -> str:
    if stop := _stop_requested_message(ctx, "accept_bkb_proposal"):
        return stop
    patch_path = _state_dir(ctx) / "proposed_bkb_patch.json"
    if not patch_path.is_file():
        return "ERROR: no proposed_bkb_patch.json found — submit_reflection with proposed_bkb_rules first"
    try:
        doc = json.loads(patch_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"ERROR: invalid proposed_bkb_patch.json: {exc}"
    if not isinstance(doc, dict):
        return "ERROR: proposed_bkb_patch.json must be an object"
    if patch_id and str(doc.get("patch_id") or "") != patch_id:
        return f"ERROR: patch_id mismatch (expected {patch_id!r})"
    from umbrella.memory.proactive.promotion import ProposedBkbPatch, accept_bkb_patch

    repo_root = pathlib.Path(getattr(ctx, "host_repo_root", None) or getattr(ctx, "repo_dir", ".")).resolve()
    ws = workspace_id or str(doc.get("workspace_id") or _workspace_id_from_drive(ctx) or "")
    patch = ProposedBkbPatch(
        patch_id=str(doc.get("patch_id") or patch_id or uuid.uuid4().hex[:12]),
        rules=list(doc.get("rules") or []),
        source_evidence=list(doc.get("source_evidence") or []),
        actor=str(doc.get("actor") or "supervisor"),
        run_id=str(doc.get("run_id") or _run_id(ctx)),
        phase_id=str(doc.get("phase_id") or _umbrella_phase_id(ctx) or ""),
        workspace_id=ws,
    )
    try:
        result = accept_bkb_patch(
            repo_root,
            patch,
            target="workspace" if ws else "manager",
        )
    except ValueError as exc:
        return f"ERROR: {exc}"
    return _json(result)


def _submit_preflight_report(
    ctx: ToolContext,
    *,
    status: str,
    blockers: list[str] | None = None,
    research_depth: str = "",
    research_depth_rationale: str = "",
) -> str:
    if stop := _stop_requested_message(ctx, "submit_preflight_report"):
        return stop
    if status not in ("ready", "blocked"):
        return f"ERROR: status must be ready or blocked, got '{status}'"
    depth = str(research_depth or "").strip().lower()
    if status == "ready":
        if depth not in {"none", "light", "full"}:
            return (
                "ERROR: research_depth is required when status=ready "
                "(none, light, or full)."
            )
    elif depth and depth not in {"none", "light", "full"}:
        return f"ERROR: invalid research_depth '{research_depth}'"
    rationale = str(research_depth_rationale or "").strip()
    if status == "ready" and len(rationale) > 500:
        return "ERROR: research_depth_rationale must be at most 500 characters."
    if status == "ready":
        from pathlib import Path

        from umbrella.contracts.capability_declaration import ensure_probe_backed_declaration
        from umbrella.contracts.runtime_probes import (
            load_runtime_capabilities,
            persist_runtime_capabilities,
            probe_runtime_capabilities,
        )

        drive = _drive_state(ctx)
        workspace_id = _workspace_id_from_drive(ctx)
        repo_root = _repo_root_from_phase_ctx(ctx)
        workspace_root = repo_root / "workspaces" / workspace_id
        caps = load_runtime_capabilities(drive)
        if not caps:
            caps = probe_runtime_capabilities(workspace_root)
            persist_runtime_capabilities(Path(drive), caps)
        ensure_probe_backed_declaration(
            drive,
            workspace_root,
            run_id=_run_id(ctx),
            workspace_id=workspace_id,
            actor="harness",
        )
    blocker_list = [str(item) for item in (blockers or []) if str(item).strip()]
    implementation_notes: list[str] = []
    normalized_from = ""
    if status == "blocked" and _preflight_blockers_are_implementation_issues(
        blocker_list
    ):
        normalized_from = status
        status = "ready"
        implementation_notes = blocker_list
        blocker_list = []
    payload: dict[str, Any] = {
        "status": status,
        "blockers": blocker_list,
    }
    if depth:
        payload["research_depth"] = depth
    if rationale:
        payload["research_depth_rationale"] = rationale
    if implementation_notes:
        payload["implementation_notes"] = implementation_notes
        payload["normalized_from"] = normalized_from
    signal_id = _write_control_signal(ctx, "submit_preflight_report", payload)
    extra = (
        f", implementation_notes: {len(implementation_notes)}"
        if implementation_notes
        else ""
    )
    return (
        f"OK: Preflight report: {status} "
        f"(blockers: {len(blocker_list)}{extra}, signal: {signal_id})"
    )


def _mentions_unresolved_pass_blocker(text: str) -> bool:
    return bool(_UNRESOLVED_PASS_BLOCKER_RE.search(str(text or "")))


def _preflight_blockers_are_implementation_issues(blockers: list[str]) -> bool:
    """Return true when a blocked report describes fixable workspace defects.

    Preflight is a platform-readiness gate. Broken app imports, failed tests,
    missing endpoints, and stale mock markers are exactly what later phases are
    supposed to repair, so they should be carried forward as notes instead of
    aborting the phase plan before execute can run.
    """
    if not blockers:
        return False
    saw_implementation_issue = False
    for blocker in blockers:
        text = str(blocker or "")
        if _PREFLIGHT_PLATFORM_BLOCKER_RE.search(text):
            return False
        if _PREFLIGHT_IMPLEMENTATION_ISSUE_RE.search(text):
            saw_implementation_issue = True
        else:
            return False
    return saw_implementation_issue


def _edit_subtask_card(
    ctx: ToolContext, *, subtask_id: str, patch: dict[str, Any]
) -> str:
    if stop := _stop_requested_message(ctx, "edit_subtask_card"):
        return stop
    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found"
    for node in plan.get("nodes", []):
        for subtask in node.get("subtasks") or []:
            if subtask.get("id") == subtask_id:
                for k, v in patch.items():
                    subtask[k] = v
                plan["version"] = plan.get("version", 0) + 1
                _write_phase_plan(ctx, plan)
                return f"Subtask '{subtask_id}' updated: {list(patch.keys())}"
    return f"ERROR: subtask '{subtask_id}' not found"


_MANAGED_RUNTIME_HARNESS_PROFILES = frozenset({"desktop_gui_runtime"})
_RUNTIME_STARTED_ONLY_PROPERTIES = frozenset(
    {"runtime_started", "module_imports", "build_succeeds", "no_test_tampering"}
)


def _proof_uses_managed_runtime(proof: Any) -> bool:
    options = getattr(proof, "harness_options", {}) or {}
    if isinstance(options, dict) and options.get("managed_runtime") is True:
        return True
    profile = str(getattr(proof, "harness_profile", "") or "")
    kind = str(getattr(getattr(proof, "execution", None), "kind", "") or "")
    return profile in _MANAGED_RUNTIME_HARNESS_PROFILES and kind == "command"


def _managed_runtime_int_option(
    options: dict[str, Any], names: tuple[str, ...], default: int, *, minimum: int = 1, maximum: int = 120
) -> int:
    for name in names:
        if options.get(name) is None:
            continue
        try:
            return max(minimum, min(int(options.get(name)), maximum))
        except (TypeError, ValueError):
            return default
    return default


def _managed_runtime_command(value: Any) -> list[str] | str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _managed_runtime_readiness_specs(options: dict[str, Any]) -> list[dict[str, Any]]:
    raw = (
        options.get("readiness")
        or options.get("readiness_probe")
        or options.get("readiness_probes")
    )
    if isinstance(raw, dict):
        specs = [raw]
    elif isinstance(raw, list):
        specs = [item for item in raw if isinstance(item, dict)]
    elif isinstance(raw, str) and raw.strip():
        specs = [{"type": "log_contains", "text": raw.strip()}]
    else:
        specs = []
    return specs or [{"type": "process_alive"}]


def _managed_runtime_contract_claims_behavior(proof: Any) -> bool:
    contract = getattr(proof, "generated_test_contract", {}) or {}
    if not isinstance(contract, dict):
        return False
    claims = contract.get("oracle_claims")
    if not isinstance(claims, list):
        return False
    behavioral_fields = {
        "input_sequence",
        "input_values",
        "expected_output",
        "expected_display",
        "expected_status",
        "expected_behavior",
    }
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        if claim.get("input_sequence"):
            return True
        if any(key in claim for key in behavioral_fields - {"expected_behavior"}):
            return True
    return False


def _managed_runtime_env_overrides(proof: Any, options: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    execution_env = getattr(getattr(proof, "execution", None), "env", None)
    for raw in (execution_env, options.get("env")):
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            name = str(key).strip()
            if name:
                env[name] = str(value)
    from umbrella.deep_agent_tools.domain_policy import public_workspace_llm_env_bridge

    bridged = public_workspace_llm_env_bridge({**os.environ, **env})
    bridged.setdefault("PYTHONIOENCODING", "utf-8")
    bridged.setdefault("PYTHONUTF8", "1")
    bridged.update(env)
    return bridged


def _managed_runtime_spec_ready(
    spec: dict[str, Any],
    *,
    status_payload: dict[str, Any],
    tail_text: str,
    elapsed: float,
) -> bool:
    kind = str(spec.get("type") or spec.get("kind") or "process_alive").strip()
    alive = str(status_payload.get("status") or "") == "running"
    if kind in {"process_alive", "alive"}:
        return alive
    if kind in {"wait", "wait_seconds"}:
        try:
            seconds = float(spec.get("seconds") or spec.get("value") or 1)
        except (TypeError, ValueError):
            seconds = 1.0
        return elapsed >= max(0.0, seconds) and alive
    if kind in {"log_contains", "stdout_contains"}:
        needle = str(spec.get("text") or spec.get("contains") or "").strip()
        return bool(needle and needle in tail_text)
    if kind in {"log_regex", "stdout_regex"}:
        pattern = str(spec.get("pattern") or spec.get("regex") or "").strip()
        if not pattern:
            return False
        try:
            return re.search(pattern, tail_text, re.MULTILINE) is not None
        except re.error:
            return False
    return False


def _run_managed_runtime_proof(
    ctx: ToolContext,
    *,
    workspace_id: str,
    workspace_root: pathlib.Path,
    proof: Any,
    command: list[str],
) -> str:
    from ouroboros.tools import background_jobs as _bg_jobs
    from umbrella.deep_agent_tools import workspace_commands as workspace_commands

    options = getattr(proof, "harness_options", {}) or {}
    if not isinstance(options, dict):
        options = {}
    startup_timeout = _managed_runtime_int_option(
        options,
        ("startup_timeout_sec", "startup_timeout_seconds", "timeout_sec"),
        max(1, min(int(getattr(getattr(proof, "execution", None), "timeout_sec", 10) or 10), 30)),
        maximum=120,
    )
    poll_interval = _managed_runtime_int_option(
        options,
        ("poll_interval_sec", "poll_seconds"),
        1,
        minimum=1,
        maximum=5,
    )
    subdir = str(getattr(getattr(proof, "execution", None), "subdir", "") or "").strip().strip("/\\")
    cwd = workspace_root / subdir if subdir else workspace_root
    repo_root = pathlib.Path(
        getattr(ctx, "host_repo_root", None)
        or getattr(ctx, "repo_dir", None)
        or workspace_root.parent.parent
    )
    prepared_command = workspace_commands._rewrite_python_command_for_workspace(
        list(command),
        repo_root=repo_root,
        workspace_root=workspace_root,
    )
    prepared_command = workspace_commands._wrap_compound_command_for_host(
        prepared_command
    )
    env_overrides = _managed_runtime_env_overrides(proof, options)
    job_id = ""
    job = None
    readiness_results: list[dict[str, Any]] = []
    latest_status: dict[str, Any] = {}
    latest_tail: dict[str, Any] = {}
    assert_payload: dict[str, Any] | None = None
    cleanup_payload: dict[str, Any] = {}
    result_payload: dict[str, Any] = {}
    started = time.time()
    try:
        job = _bg_jobs.start_background(
            pathlib.Path(getattr(ctx, "drive_root")),
            argv=prepared_command,
            cwd=cwd,
            label=f"proof-{workspace_id}",
            env_overrides=env_overrides,
        )
        job_id = job.job_id
        specs = _managed_runtime_readiness_specs(options)
        ready = False
        deadline = started + startup_timeout
        while time.time() <= deadline:
            latest_status = _bg_jobs.status(pathlib.Path(getattr(ctx, "drive_root")), job_id)
            latest_tail = _bg_jobs.tail(
                pathlib.Path(getattr(ctx, "drive_root")),
                job_id,
                lines=200,
            )
            tail_text = str(latest_tail.get("tail") or "")
            elapsed = time.time() - started
            readiness_results = [
                {
                    "type": str(spec.get("type") or spec.get("kind") or "process_alive"),
                    "ready": _managed_runtime_spec_ready(
                        spec,
                        status_payload=latest_status,
                        tail_text=tail_text,
                        elapsed=elapsed,
                    ),
                }
                for spec in specs
            ]
            if readiness_results and all(item["ready"] for item in readiness_results):
                ready = True
                break
            if str(latest_status.get("status") or "") == "exited":
                break
            time.sleep(float(poll_interval))
        if ready:
            assert_command: list[str] | str = []
            for key in ("assert_command", "interaction_command", "driver_command"):
                assert_command = _managed_runtime_command(options.get(key))
                if assert_command:
                    break
            if assert_command:
                raw_assert = workspace_commands.run_workspace_command(
                    ctx,
                    workspace_id=workspace_id,
                    command=assert_command,
                    subdir=str(options.get("assert_subdir") or options.get("driver_subdir") or subdir),
                    timeout_seconds=_managed_runtime_int_option(
                        options,
                        ("assert_timeout_sec", "interaction_timeout_sec", "driver_timeout_sec"),
                        30,
                        maximum=300,
                    ),
                    allow_dependency_install=False,
                )
                try:
                    assert_payload = json.loads(str(raw_assert or "{}"))
                except Exception:
                    assert_payload = {
                        "status": "error",
                        "exit_code": 1,
                        "output": str(raw_assert or "")[-1200:],
                    }
        latest_status = _bg_jobs.status(pathlib.Path(getattr(ctx, "drive_root")), job_id)
        latest_tail = _bg_jobs.tail(
            pathlib.Path(getattr(ctx, "drive_root")),
            job_id,
            lines=200,
        )
        required_props = {
            str(item)
            for item in getattr(getattr(proof, "oracle", None), "required_properties", ())
        }
        needs_driver = bool(
            required_props - _RUNTIME_STARTED_ONLY_PROPERTIES
        ) or _managed_runtime_contract_claims_behavior(proof)
        assert_ok = assert_payload is None or (
            int(assert_payload.get("exit_code", 1)) == 0
            and str(assert_payload.get("status") or "") != "blocked"
        )
        missing_driver = ready and needs_driver and assert_payload is None
        exit_code = 0 if ready and assert_ok and not missing_driver else 1
        status = "managed_runtime_passed" if exit_code == 0 else "managed_runtime_failed"
        result_payload = {
            "workspace_id": workspace_id,
            "exit_code": exit_code,
            "status": status,
            "backend": "managed_runtime",
            "command": prepared_command,
            "declared_command": command,
            "job_id": job_id,
            "pid": getattr(job, "pid", 0) if job is not None else 0,
            "cwd": str(cwd),
            "startup_timeout_sec": startup_timeout,
            "readiness": readiness_results,
            "assert_result": assert_payload,
            "output": str(latest_tail.get("tail") or "")[-4000:],
            "managed_runtime": {
                "ready": ready,
                "missing_driver": missing_driver,
                "status": latest_status,
                "log_path": latest_tail.get("log_path") or "",
            },
        }
    except Exception as exc:
        result_payload = {
            "workspace_id": workspace_id,
            "exit_code": 1,
            "status": "managed_runtime_error",
            "backend": "managed_runtime",
            "command": prepared_command,
            "declared_command": command,
            "job_id": job_id,
            "error": str(exc),
        }
    finally:
        if job_id:
            try:
                cleanup_payload = _bg_jobs.kill(
                    pathlib.Path(getattr(ctx, "drive_root")),
                    job_id,
                )
            except Exception:
                cleanup_payload = {"job_id": job_id, "status": "cleanup_failed"}
            if cleanup_payload:
                try:
                    record = pathlib.Path(getattr(ctx, "drive_root")) / "logs" / "managed_runtime_cleanup.jsonl"
                    record.parent.mkdir(parents=True, exist_ok=True)
                    with record.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(cleanup_payload, ensure_ascii=False) + "\n")
                except Exception:
                    log.debug("managed runtime cleanup audit failed", exc_info=True)
    if cleanup_payload:
        managed = result_payload.get("managed_runtime")
        if isinstance(managed, dict):
            managed["cleanup"] = cleanup_payload
        else:
            result_payload["managed_runtime"] = {"cleanup": cleanup_payload}
    return _json(result_payload)


def _request_watcher_review(
    ctx: ToolContext,
    *,
    reason: str,
    contract_issues: list[dict[str, Any]] | None = None,
) -> str:
    if stop := _stop_requested_message(ctx, "request_watcher_review"):
        return stop
    from umbrella.deep_agent_tools.phase_control_retry import (
        _phase_subtask_retry_watcher_review_payload,
    )

    review = _phase_subtask_retry_watcher_review_payload(
        ctx,
        reason=reason,
        contract_issues=contract_issues,
    )
    if review.get("status") not in {"review_recorded", "review_not_required"}:
        return json.dumps(review, ensure_ascii=False, indent=2)
    review = dict(review)
    gate = _loop_state_view(ctx).get("typed_action_gate")
    if (
        isinstance(gate, dict)
        and str(gate.get("reason") or "") == "no_test_tampering_oracle_freeze"
        and not bool(review.get("requires_plan_mutation"))
    ):
        _clear_typed_action_gate(ctx)
        review["cleared_typed_action_gate"] = "no_test_tampering_oracle_freeze"
    signal_id = _write_control_signal(ctx, "request_watcher_review", review)
    review["signal_id"] = signal_id
    if review.get("status") == "review_recorded":
        _mirror_watcher_review_to_palace(ctx, review=review)
    return json.dumps(review, ensure_ascii=False, indent=2)


def _mirror_watcher_review_to_palace(
    ctx: ToolContext,
    *,
    review: dict[str, Any],
) -> None:
    """Persist accepted retry-watcher reviews into subtask-scoped memory.

    The control-signal ledger remains canonical for orchestration. This mirror
    makes high-value watcher guidance available to later execute turns,
    subtask/final review, and operator memory inspection without replaying
    raw JSONL logs.
    """

    try:
        if str(review.get("status") or "") != "review_recorded":
            return
        subtask_id = str(review.get("subtask_id") or "").strip()
        if not subtask_id:
            return
        plan = _read_phase_plan(ctx) or {}
        workspace_id = (
            str(plan.get("workspace_id") or "").strip()
            or _workspace_id_from_drive(ctx)
            or str(_loop_state_view(ctx).get("active_workspace_id") or "").strip()
        )
        if not workspace_id:
            return
        phase_id = str(
            (getattr(ctx, "context_overlays", {}) or {})
            .get("phase_node", {})
            .get("id")
            or "execute"
        ).strip()
        run_id = str(plan.get("run_id") or _run_id(ctx) or "").strip()
        operator_reason = str(review.get("operator_reason") or "")
        memory_doc = {
            "artifact": "retry_watcher_review",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "phase_id": phase_id,
            "subtask_id": subtask_id,
            "failed_attempts": int(review.get("failed_attempts") or 0),
            "prior_watcher_reviews": int(review.get("prior_watcher_reviews") or 0),
            "operator_reason": operator_reason,
            "latest_failure": review.get("latest_failure") or {},
            "recommendation": str(review.get("recommendation") or ""),
            "signal_id": str(review.get("signal_id") or ""),
        }
        from umbrella.memory.palace.facade import MemPalace

        palace = MemPalace(pathlib.Path(getattr(ctx, "repo_dir", "")), workspace_id)
        palace.add(
            store="palace.subtask",
            content=json.dumps(memory_doc, ensure_ascii=False, indent=2),
            tier="hot",
            scope="subtask_scoped",
            tags=[
                "retry_watcher",
                "subtask_review",
                "execution_artifact",
                "execution_error",
            ],
            phase=phase_id or "execute",
            subtask_id=subtask_id,
            run_id=run_id,
            verified=True,
            source_path=(
                ".memory/drive/state/phase_control_signal.json#"
                + str(review.get("signal_id") or "")
            ),
            extra={
                "failed_attempts": int(review.get("failed_attempts") or 0),
                "review_kind": str(review.get("review_kind") or ""),
            },
        )
    except Exception:
        pass


def _harness_run(
    ctx: ToolContext,
    *,
    subtask_id: str,
    n_candidates: int = 2,
    strategy: str = "tests_pass",
    timeout_sec: int = 300,
) -> str:
    if stop := _stop_requested_message(ctx, "harness_run"):
        return stop
    signal_id = _write_control_signal(ctx, "harness_run", {
        "subtask_id": subtask_id,
        "n_candidates": n_candidates,
        "strategy": strategy,
        "timeout_sec": timeout_sec,
    })
    return f"Harness run requested for subtask '{subtask_id}' with {n_candidates} candidates, strategy={strategy} (signal: {signal_id})"


def _proof_scope_changed_files(subtask: dict[str, Any], proof: Any) -> list[str]:
    paths: list[str] = []
    for key in ("files_to_create", "files_to_change", "files_affected"):
        raw = subtask.get(key)
        if isinstance(raw, str) and raw.strip():
            paths.append(raw.strip().replace("\\", "/").lstrip("/"))
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                norm = str(item or "").strip().replace("\\", "/").lstrip("/")
                if norm:
                    paths.append(norm)
    scope = getattr(proof, "scope", None)
    if scope is not None:
        for key in ("files_under_test", "changed_files_expected"):
            values = getattr(scope, key, ()) or ()
            for item in values:
                norm = str(item or "").strip().replace("\\", "/").lstrip("/")
                if norm:
                    paths.append(norm)
    return list(dict.fromkeys(paths))


def _proof_payload_is_pytest_skip_only(payload: dict[str, Any]) -> bool:
    text = "\n".join(
        str(payload.get(key) or "")
        for key in ("output", "stdout", "stderr", "result", "result_preview")
    )
    if not text.strip():
        return False
    return bool(_PYTEST_SKIP_ONLY_RE.search(text)) and not (
        _PYTEST_PASS_RE.search(text) or _PYTEST_FAILURE_RE.search(text)
    )


def _resolve_execute_subtask(
    plan: dict[str, Any], subtask_id: str
) -> dict[str, Any] | None:
    execute = _phase_plan_execute_node(plan)
    if not isinstance(execute, dict):
        return None
    subtasks = execute.get("subtasks")
    if not isinstance(subtasks, list):
        return None
    wanted = str(subtask_id or "").strip()
    if wanted:
        for item in subtasks:
            if isinstance(item, dict) and str(item.get("id") or "") == wanted:
                return item
        return None
    for item in subtasks:
        if isinstance(item, dict) and str(item.get("status") or "pending") == "pending":
            return item
    return None


def _proof_execution_environment_id(proof: Any) -> str:
    execution = getattr(proof, "execution", None)
    env_id = str(getattr(execution, "execution_environment_id", "") or "").strip()
    if env_id:
        return env_id
    options = getattr(proof, "harness_options", {}) or {}
    if isinstance(options, dict):
        for key in ("execution_environment_id", "environment_id", "env_id"):
            value = str(options.get(key) or "").strip()
            if value:
                return value
    return DEFAULT_EXECUTION_ENVIRONMENT_ID


def _proof_command_python_executable(command: list[str], fallback: str) -> str:
    if not command:
        return fallback
    first = pathlib.Path(str(command[0])).name.lower()
    if first in {"python", "python3", "py", "python.exe", "python3.exe", "py.exe"}:
        return str(command[0])
    if "python" in first:
        return str(command[0])
    return fallback


def _proof_required_capability_issues(
    *,
    drive_root: pathlib.Path,
    proof: Any,
    subtask_id: str,
    env_id: str,
    env_hash: str,
    proof_ref: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    refs = []
    if isinstance(proof_ref, dict) and proof_ref.get("ref_id"):
        refs.append(f"{proof_ref.get('ref_type') or 'ledger_event'}:{proof_ref['ref_id']}")
    for cap in tuple(getattr(proof, "required_capabilities", ()) or ()):
        capability_id = str(cap or "").strip()
        if capability_id not in {"desktop_gui_runtime"}:
            continue
        binding = find_capability_binding(
            drive_root,
            capability_id=capability_id,
            env_id=env_id,
            env_hash=env_hash,
        )
        if binding is not None and binding.available:
            continue
        issues.append(
            {
                "code": "capability_probe_environment_mismatch",
                "severity": "blocking",
                "target_subtask_id": subtask_id,
                "target_kind": "proof",
                "contract_path": "proof.required_capabilities",
                "capability_id": capability_id,
                "env_id": env_id,
                "env_hash": env_hash,
                "failure_phase": "capability_binding",
                "production_code_entered": False,
                "message": (
                    f"Proof requires `{capability_id}`, but there is no "
                    "available probe binding for the same execution environment."
                ),
                "required_deltas": [
                    {
                        "op": "replace",
                        "path": "proof.execution.execution_environment_id",
                        "replacement": env_id,
                    }
                ],
                "evidence_refs": refs,
            }
        )
    return issues


def _tk_root_setup_failure_issue(
    *,
    output: str,
    proof: Any,
    subtask_id: str,
    drive_root: pathlib.Path,
    env_id: str,
    env_hash: str,
    python_executable: str,
    proof_ref: dict[str, Any],
) -> dict[str, Any] | None:
    status = classify_tcl_tk_status(output)
    if not status:
        return None
    lowered = str(output or "").lower()
    setup_failure = (
        "error at setup" in lowered
        or "fixture" in lowered
        or "root = tk.tk()" in lowered
        or "root=tk.tk()" in lowered
        or "_tkinter.tclerror" in lowered
    )
    if not setup_failure:
        return None
    refs = []
    if proof_ref.get("ref_id"):
        refs.append(f"{proof_ref.get('ref_type') or 'ledger_event'}:{proof_ref['ref_id']}")
    required_caps = {
        str(item).strip()
        for item in (getattr(proof, "required_capabilities", ()) or ())
        if str(item).strip()
    }
    harness_profile = str(getattr(proof, "harness_profile", "") or "")
    if "desktop_gui_runtime" in required_caps or harness_profile == "desktop_gui_runtime":
        code = "capability_probe_environment_mismatch"
        message = (
            "desktop_gui_runtime failed in the proof environment before "
            "production code entered; the accepted capability is not bound "
            "to this interpreter/env."
        )
    else:
        code = "proof_execution_infra"
        message = (
            "Headless/unit proof attempted to create a real native Tk root, "
            "and the proof environment cannot provide that runtime. This is "
            "a proof/capability contract issue, not an implementation repair."
        )
    try:
        persist_capability_binding(
            drive_root,
            CapabilityBinding(
                capability_id="desktop_gui_runtime",
                available=False,
                env_id=env_id,
                python_executable=python_executable,
                env_hash=env_hash,
                probe_command=tuple(getattr(getattr(proof, "execution", None), "command", ()) or ()),
                reason=str(status.get("reason") or "tk_runtime_setup_failed"),
            ),
        )
    except Exception:
        pass
    return {
        "code": code,
        "severity": "blocking",
        "target_subtask_id": subtask_id,
        "target_kind": "proof",
        "contract_path": "proof.execution",
        "capability_id": "desktop_gui_runtime",
        "env_id": env_id,
        "env_hash": env_hash,
        "python_executable": python_executable,
        "failure_phase": "fixture/setup/runtime_lifecycle",
        "production_code_entered": False,
        "tcl_tk_status": status,
        "message": message,
        "required_deltas": [
            {
                "op": "replace",
                "path": "proof.harness_profile",
                "replacement": "desktop_gui_headless",
            },
            {
                "op": "remove",
                "path": "proof.required_capabilities",
                "values": ["desktop_gui_runtime"],
            },
        ],
        "evidence_refs": refs,
    }


def _provision_workspace_environment(
    ctx: ToolContext,
    *,
    workspace_id: str = "",
    env_id: str = DEFAULT_EXECUTION_ENVIRONMENT_ID,
    operations: list[dict[str, Any]] | None = None,
    reason: str = "",
    expected_artifacts: list[str] | None = None,
) -> str:
    """Sanctioned environment setup/probe path, separate from proof shell."""

    if stop := _stop_requested_message(ctx, "provision_workspace_environment"):
        return stop
    if not _is_phase_run_context(ctx):
        return "ERROR: provision_workspace_environment is only available during Umbrella phase runs."
    workspace = str(workspace_id or _workspace_id_from_drive(ctx) or "").strip()
    if not workspace:
        return "ERROR: workspace_id is required."
    ops = operations or []
    if not isinstance(ops, list) or not ops:
        return "ERROR: operations must be a non-empty array."
    repo_root = _repo_root_from_phase_ctx(ctx)
    workspace_root = _workspace_root_from_phase_ctx(ctx, workspace)
    drive_root = pathlib.Path(getattr(ctx, "drive_root", "") or "")
    env_record = resolve_execution_environment(
        repo_root=repo_root,
        workspace_root=workspace_root,
        env_id=env_id or DEFAULT_EXECUTION_ENVIRONMENT_ID,
        cwd=workspace_root,
    )
    persist_environment_record(drive_root, env_record)
    from umbrella.contracts.runtime_probes import execute_probe_result
    from umbrella.enforcement import diff_snapshots, restore_snapshot_changes, snapshot_workspace

    results: list[dict[str, Any]] = []
    protected_mutation: dict[str, Any] | None = None
    expected = {
        str(item).replace("\\", "/").strip().lstrip("/")
        for item in (expected_artifacts or [])
        if str(item).strip()
    }
    for raw in ops:
        if not isinstance(raw, dict):
            return "ERROR: each provisioning operation must be an object."
        kind = str(raw.get("kind") or raw.get("operation") or "").strip()
        if kind not in {
            "python_dependency_install",
            "pip_install",
            "uv_sync",
            "capability_probe",
            "python_module_probe",
        }:
            return _json(
                {
                    "status": "blocked",
                    "reason": "unsupported_provisioning_operation",
                    "operation": raw,
                    "allowed_operations": [
                        "python_dependency_install",
                        "pip_install",
                        "uv_sync",
                        "capability_probe",
                        "python_module_probe",
                    ],
                }
            )
        if kind == "python_module_probe":
            module = str(raw.get("module") or "").strip()
            if not module:
                return "ERROR: python_module_probe requires module."
            command = [
                env_record.python_executable,
                "-c",
                f"import {module}",
            ]
        elif kind == "capability_probe":
            capability_id = str(raw.get("capability_id") or raw.get("capability") or "").strip()
            probe = raw.get("probe")
            if not capability_id or not isinstance(probe, dict):
                return "ERROR: capability_probe requires capability_id and probe."
            probe = dict(probe)
            probe.setdefault("execution_environment_id", env_record.env_id)
            probe_result = execute_probe_result(
                probe,
                workspace_root=workspace_root,
                capability_tag=capability_id,
                timeout_sec=float(probe.get("timeout_sec", raw.get("timeout_sec", 5.0))),
            )
            persist_capability_binding(
                drive_root,
                CapabilityBinding(
                    capability_id=capability_id,
                    available=bool(probe_result.get("available")),
                    env_id=str(probe_result.get("execution_environment_id") or env_record.env_id),
                    python_executable=str(probe_result.get("python_executable") or env_record.python_executable),
                    cwd=str(probe_result.get("cwd") or env_record.cwd),
                    env_hash=str(probe_result.get("env_hash") or env_record.env_hash),
                    probe_command=tuple(str(item) for item in (probe_result.get("probe_command") or []))
                    if isinstance(probe_result.get("probe_command"), list)
                    else (),
                    probe_exit_code=(
                        int(probe_result.get("probe_exit_code"))
                        if probe_result.get("probe_exit_code") is not None
                        else None
                    ),
                    reason=str(probe_result.get("reason") or ""),
                ),
            )
            results.append(
                {
                    "kind": kind,
                    "capability_id": capability_id,
                    "available": bool(probe_result.get("available")),
                    "reason": str(probe_result.get("reason") or ""),
                    "env_id": str(probe_result.get("execution_environment_id") or env_record.env_id),
                    "env_hash": str(probe_result.get("env_hash") or env_record.env_hash),
                }
            )
            continue
        else:
            if kind in {"python_dependency_install", "pip_install"}:
                packages = [
                    str(item).strip()
                    for item in (raw.get("packages") or raw.get("dependencies") or [])
                    if str(item).strip()
                ] if isinstance(raw.get("packages") or raw.get("dependencies"), list) else []
                command_raw = raw.get("command")
                if isinstance(command_raw, list):
                    command = [str(item) for item in command_raw]
                elif packages:
                    command = [
                        env_record.python_executable,
                        "-m",
                        "pip",
                        "install",
                        *packages,
                    ]
                else:
                    return "ERROR: pip_install requires packages or command."
                lowered = [str(item).strip().lower() for item in command]
                if "-e" in lowered or "--editable" in lowered:
                    return _json(
                        {
                            "status": "blocked",
                            "reason": "editable_install_not_allowed_in_provisioning",
                            "command": command,
                            "message": (
                                "Editable installs can write source-tree metadata "
                                "and are not accepted as proof provisioning."
                            ),
                        }
                    )
            else:
                command = ["uv", "sync"]
        before = snapshot_workspace(workspace_root, capture_content=True)
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        proc = subprocess.run(
            command,
            cwd=workspace_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=max(10, int(raw.get("timeout_sec") or 120)),
            check=False,
        )
        changes = diff_snapshots(before, snapshot_workspace(workspace_root))
        protected_changes = [
            change.path
            for change in changes
            if change.path not in expected
            and not change.path.startswith((".venv/", ".umbrella_scratch/", ".memory/"))
        ]
        if protected_changes:
            rollback = restore_snapshot_changes(before, changes)
            protected_mutation = {
                "status": "blocked",
                "reason": "provisioning_workspace_mutation_guard",
                "operation": kind,
                "command": command,
                "protected_changes": protected_changes,
                "rollback": rollback,
            }
            break
        results.append(
            {
                "kind": kind,
                "command": command,
                "exit_code": proc.returncode,
                "stdout_tail": (proc.stdout or "")[-1200:],
                "stderr_tail": (proc.stderr or "")[-1200:],
            }
        )
        if proc.returncode != 0:
            break
    payload: dict[str, Any] = {
        "status": "blocked" if protected_mutation else "ok",
        "workspace_id": workspace,
        "reason": str(reason or "").strip(),
        "environment_record": env_record.to_dict(),
        "operations": results,
    }
    if protected_mutation:
        payload.update(protected_mutation)
    return _json(payload)


def _run_subtask_proof(ctx: ToolContext, *, subtask_id: str = "") -> str:
    """Run the active subtask's typed proof and return ledger-backed completion refs."""

    if stop := _stop_requested_message(ctx, "run_subtask_proof"):
        return stop
    if not _is_phase_run_context(ctx):
        return "ERROR: run_subtask_proof is only available during Umbrella phase runs."

    workspace_id = _workspace_id_from_drive(ctx)
    repo_root = _repo_root_from_phase_ctx(ctx)
    workspace_root = _workspace_root_from_phase_ctx(ctx, workspace_id)
    phase = _phase_control_phase_id(ctx) or "execute"
    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: phase_plan.json is missing; cannot resolve subtask proof."

    subtask = _resolve_execute_subtask(plan, subtask_id)
    if subtask is None:
        return (
            "ERROR: subtask not found or no pending subtask remains "
            f"(requested={subtask_id!r})."
        )
    if retry_block := _phase_subtask_retry_escalation_block(
        ctx, tool_name="run_subtask_proof"
    ):
        return _json(retry_block)
    resolved_id = str(subtask.get("id") or "").strip()
    proof_raw = subtask.get("proof")
    if not isinstance(proof_raw, dict):
        return f"ERROR: subtask `{resolved_id}` has no typed proof contract."

    from umbrella.contracts import ProofSpec
    from umbrella.deep_agent_tools import workspace_commands as workspace_commands
    from umbrella.enforcement.ledger import (
        append_supervisor_ledger_event,
        latest_ledger_event_id,
        supervisor_ledger_ref,
    )

    proof = ProofSpec.from_mapping(proof_raw)
    command = list(proof.execution.command)
    if not command:
        return f"ERROR: subtask `{resolved_id}` proof has an empty command."
    proof_env_id = _proof_execution_environment_id(proof)
    proof_subdir = str(proof.execution.subdir or "").strip().strip("/\\")
    proof_cwd = workspace_root / proof_subdir if proof_subdir else workspace_root
    environment_record = resolve_execution_environment(
        repo_root=repo_root,
        workspace_root=workspace_root,
        env_id=proof_env_id,
        cwd=proof_cwd,
        env_overrides=proof.execution.env,
    )
    try:
        persist_environment_record(pathlib.Path(ctx.drive_root), environment_record)
    except Exception:
        log.debug("execution environment record persist failed", exc_info=True)

    if _proof_uses_managed_runtime(proof):
        raw = _run_managed_runtime_proof(
            ctx,
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            proof=proof,
            command=command,
        )
    else:
        raw = workspace_commands.run_workspace_command(
            ctx,
            workspace_id=workspace_id,
            command=command,
            subdir=proof.execution.subdir,
            timeout_seconds=max(10, int(proof.execution.timeout_sec or 120)),
            allow_dependency_install=False,
            extra_env=proof.execution.env,
        )
    try:
        import json as json_module

        payload = json_module.loads(str(raw or "{}"))
    except Exception:
        return (
            "ERROR: proof command did not return JSON; use shell only for exploration. "
            f"Output tail: {str(raw or '')[-800:]}"
        )
    if not isinstance(payload, dict):
        return "ERROR: proof command returned non-object JSON."

    exit_code = int(payload.get("exit_code", 1))
    skip_only = _proof_payload_is_pytest_skip_only(payload)
    if skip_only and proof.anti_gaming.requires_real_runtime:
        skip_only = True
    proof_command_passed = (
        exit_code == 0
        and str(payload.get("status") or "") != "blocked"
        and not skip_only
    )
    after_patch_event = latest_ledger_event_id(
        repo_root=repo_root,
        workspace_id=workspace_id,
        tool="apply_workspace_patch",
    )
    ws_hash = workspace_hash(workspace_root)
    changed_files = _proof_scope_changed_files(subtask, proof)
    diff_h = diff_hash(workspace_root, changed_files)
    materialization_issues: list[ContractIssue] = []
    if proof_command_passed:
        pending_ref = {
            "ref_type": "ledger_event",
            "ref_id": "pending_run_subtask_proof",
            "produced_by": "verifier",
            "phase": phase,
            "subtask_id": resolved_id,
        }
        pending_completion = CompletionContract.from_mapping(
            {
                "subtask_id": resolved_id,
                "status": "done",
                "changed_files": changed_files,
                "deleted_files": [],
                "completed_claims": [
                    {
                        "claim_id": f"{resolved_id}.proof",
                        "text": (
                            f"Subtask proof `{proof.execution.kind}` "
                            f"passed (exit {exit_code})."
                        ),
                        "proof_refs": [pending_ref],
                    }
                ],
                "evidence_refs": [pending_ref],
            }
        )
        materialization_issues = validate_completion_materialization(
            pending_completion,
            active_subtask=subtask,
            workspace_root=str(workspace_root),
            raw_completion=None,
            phase=phase,
        )
    blocking_materialization_issues = [
        issue
        for issue in materialization_issues
        if issue.severity in {"error", "blocking", "human_required"}
    ]
    materialization_passed = not blocking_materialization_issues
    passed = proof_command_passed and materialization_passed
    report_hash = hash_value(
        {
            "subtask_id": resolved_id,
            "passed": passed,
            "exit_code": exit_code,
            "proof_kind": proof.execution.kind,
            "execution_environment_id": environment_record.env_id,
            "env_hash": environment_record.env_hash,
            "python_executable": environment_record.python_executable,
            "workspace_hash": ws_hash,
            "diff_hash": diff_h,
            "skip_only": skip_only,
            "proof_command_passed": proof_command_passed,
            "materialization_issues": [
                json_ready(issue) for issue in blocking_materialization_issues
            ],
        }
    )
    ledger_result = {
        "report_hash": report_hash,
        "passed": passed,
        "workspace_hash": ws_hash,
        "diff_hash": diff_h,
    }
    try:
        proof_ledger = append_supervisor_ledger_event(
            repo_root=repo_root,
            workspace_id=workspace_id,
            actor="verifier",
            phase=phase,
            tool="run_subtask_proof",
            args={
                "subtask_id": resolved_id,
                "command": command,
                "proof_kind": proof.execution.kind,
                "harness_profile": proof.harness_profile,
                "execution_environment_id": environment_record.env_id,
                "env_hash": environment_record.env_hash,
            },
            result=ledger_result,
            touched_files=[],
        )
    except Exception:
        log.debug("supervisor ledger append failed for run_subtask_proof", exc_info=True)
        return "ERROR: failed to record verifier ledger event for subtask proof."

    verification_report = {
        "report_id": proof_ledger.event_id,
        "report_hash": report_hash,
        "workspace_hash": ws_hash,
        "diff_hash": diff_h,
        "produced_after_event_id": after_patch_event,
        "verifier_id": "run_subtask_proof",
        "passed": passed,
        "ledger_hash": proof_ledger.event_hash,
        "execution_environment_id": environment_record.env_id,
        "env_hash": environment_record.env_hash,
        "proof_env_hash": environment_record.env_hash,
        "python_executable": environment_record.python_executable,
    }
    proof_ref = {
        "ref_type": "ledger_event",
        "ref_id": proof_ledger.event_id,
        "hash": proof_ledger.event_hash,
        "produced_by": "verifier",
        "phase": phase,
        "subtask_id": resolved_id,
    }
    if after_patch_event:
        proof_ref["created_after_event"] = after_patch_event

    failure_output = "\n".join(
        str(payload.get(key) or "")
        for key in ("output", "stdout", "stderr")
        if str(payload.get(key) or "").strip()
    )
    shell_result = payload.get("shell_result")
    if isinstance(shell_result, dict):
        failure_output = "\n".join(
            item
            for item in (
                failure_output,
                str(shell_result.get("output") or ""),
                str(shell_result.get("stdout") or ""),
                str(shell_result.get("stderr") or ""),
            )
            if item.strip()
        )
    generated_contract = (
        proof.generated_test_contract
        or (
            subtask.get("generated_test_contract")
            if isinstance(subtask.get("generated_test_contract"), dict)
            else {}
        )
    )
    oracle_issue_payloads: list[dict[str, Any]] = []
    if not passed and generated_contract:
        oracle_issues = generated_oracle_contract_issues(
            generated_contract,
            subtask_id=resolved_id,
            failed_node_ids=extract_failed_pytest_node_ids(failure_output),
            evidence_refs=(f"ledger_event:{proof_ledger.event_id}",),
        )
        oracle_issue_payloads = contract_issues_payload(oracle_issues)
    infra_issue_payloads: list[dict[str, Any]] = []
    if not passed:
        infra_issue = _tk_root_setup_failure_issue(
            output=failure_output,
            proof=proof,
            subtask_id=resolved_id,
            drive_root=pathlib.Path(ctx.drive_root),
            env_id=environment_record.env_id,
            env_hash=environment_record.env_hash,
            python_executable=environment_record.python_executable,
            proof_ref=proof_ref,
        )
        if infra_issue:
            infra_issue_payloads.append(infra_issue)
        infra_issue_payloads.extend(
            _proof_required_capability_issues(
                drive_root=pathlib.Path(ctx.drive_root),
                proof=proof,
                subtask_id=resolved_id,
                env_id=environment_record.env_id,
                env_hash=environment_record.env_hash,
                proof_ref=proof_ref,
            )
        )
    contract_issue_payloads = [*oracle_issue_payloads, *infra_issue_payloads]

    if blocking_materialization_issues:
        issue_details = "; ".join(
            f"{issue.code}: {issue.message or issue.suggested_action or issue.code}"
            for issue in blocking_materialization_issues[:6]
        )
        return _json(
            {
                "passed": False,
                "proof_command_passed": proof_command_passed,
                "materialization_passed": False,
                "exit_code": exit_code,
                "skip_only": skip_only,
                "subtask_id": resolved_id,
                "command": command,
                "shell_result": payload,
                **supervisor_ledger_ref(proof_ledger),
                "verification_report": verification_report,
                "proof_ref": proof_ref,
                "materialization_issues": [
                    json_ready(issue) for issue in blocking_materialization_issues
                ],
                **(
                    {"contract_issues": contract_issue_payloads}
                    if contract_issue_payloads
                    else {}
                ),
                "next_step": (
                    "Do not call mark_subtask_complete yet. The proof command passed, "
                    "but the active subtask's declared filesystem materialization is "
                    f"missing: {issue_details}. Create or populate the missing declared "
                    "files inside the workspace, then rerun run_subtask_proof."
                ),
            }
        )

    completion_hint = {
        "subtask_id": resolved_id,
        "status": "done" if passed else "failed",
        "changed_files": changed_files,
        "deleted_files": [],
        "completed_claims": [
            {
                "claim_id": f"{resolved_id}.proof",
                "text": (
                    f"Subtask proof `{proof.execution.kind}` "
                    f"{'passed' if passed else 'failed'} (exit {exit_code})."
                ),
                "proof_refs": [proof_ref],
            }
        ],
        "evidence_refs": [proof_ref],
        "verification_report": verification_report,
        "notes": "Copy verification_report and proof_refs into mark_subtask_complete.",
    }
    if passed:
        import time as _time

        _set_completion_session(
            ctx,
            {
                "subtask_id": resolved_id,
                "frozen_at": _time.time(),
                "workspace_hash": ws_hash,
                "allowed_tools": _completion_tools_after_passed_proof(ctx),
            },
        )
    return _json(
        {
            "passed": passed,
            "exit_code": exit_code,
            "skip_only": skip_only,
            "subtask_id": resolved_id,
            "command": command,
            "shell_result": payload,
            **supervisor_ledger_ref(proof_ledger),
            "verification_report": verification_report,
            "proof_ref": proof_ref,
            **(
                {"contract_issues": contract_issue_payloads}
                if contract_issue_payloads
                else {}
            ),
            **(
                {"proof_failure_classification": infra_issue_payloads[0]}
                if infra_issue_payloads
                else {}
            ),
            "completion_contract_hint": completion_hint,
            "next_step": (
                "If passed, call mark_subtask_complete(completion_contract=completion_contract_hint). "
                "Do not rewrite completion_contract_hint.changed_files; it is the exact diff-hash input "
                "used by this verifier report."
            ),
        }
    )


def _trim_completion_text(value: Any, *, limit: int = 1600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ...[truncated]"


def _completion_evidence_items(evidence: Any) -> list[str]:
    if isinstance(evidence, str):
        values = [evidence]
    elif isinstance(evidence, (list, tuple, set, frozenset)):
        values = list(evidence)
    else:
        values = []
    return [str(item or "").strip() for item in values if str(item or "").strip()]


def _completion_signal_payload(
    *,
    subtask_id: str,
    notes: str = "",
    status: str = "done",
    summary: str = "",
    evidence: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_evidence = [
        _trim_completion_text(item, limit=1200)
        for item in _completion_evidence_items(evidence)
    ][:10]
    payload: dict[str, Any] = {
        "subtask_id": str(subtask_id or "").strip(),
        "status": str(status or "done").strip() or "done",
        "summary": _trim_completion_text(summary or notes),
        "notes": _trim_completion_text(notes),
        "evidence": normalized_evidence,
    }
    if extra:
        payload.update(extra)
    return payload


def _mark_subtask_complete(
    ctx: ToolContext,
    *,
    completion_contract: dict[str, Any] | None = None,
    subtask_id: str = "",
    notes: str = "",
    status: str = "done",
    summary: str = "",
    evidence: Any = None,
) -> str:
    """Complete either the internal Ouroboros subtask or a PhasePlan card.

    Umbrella phase manifests and the internal Ouroboros planner both use the
    public tool name ``mark_subtask_complete``. Keep that as one tool, but make
    the handler compatible with both contracts so phase-run orchestration does
    not shadow the planner completion gate.
    """
    if stop := _stop_requested_message(ctx, "mark_subtask_complete"):
        return stop
    evidence_items = _completion_evidence_items(evidence)
    if memory_claim_issue := _completion_llm_memory_claim_issue(
        subtask_id=subtask_id or "active_subtask",
        summary=summary,
        notes=notes,
        evidence=evidence_items,
    ):
        return memory_claim_issue
    typed_completion: CompletionContract | None = None
    if _is_phase_run_context(ctx):
        status_issue = _phase_completion_status_issue(
            subtask_id=subtask_id,
            status=status,
        )
        if status_issue:
            return status_issue
        if not isinstance(completion_contract, dict):
            plan = _read_phase_plan(ctx)
            current_phase = _current_phase_node(ctx, plan) if plan else None
            subtasks = _phase_subtasks(current_phase)
            if subtasks:
                requested = str(subtask_id or "").strip()
                phase_id = str((current_phase or {}).get("id") or "").strip()
                known_ids = {str(item.get("id") or "").strip() for item in subtasks}
                if requested and requested not in known_ids and requested != phase_id:
                    return f"ERROR: subtask '{requested}' not found in plan"
            if retry_block := _phase_subtask_retry_escalation_block(
                ctx, tool_name="mark_subtask_complete"
            ):
                return (
                    "ERROR: mark_subtask_complete blocked: "
                    f"{_json(retry_block)}"
                )
            if subtasks:
                return (
                    "ERROR: mark_subtask_complete contract rejected: "
                    "completion_contract is required for phase-run subtask "
                    "completion. Run `run_subtask_proof` and pass its "
                    "`completion_contract_hint` unchanged as "
                    "`completion_contract`; summary/evidence-only completion "
                    "drops verifier proof_refs and verification_report."
                )
            materialization_issue = _phase_subtask_materialization_issue(
                ctx,
                current_phase=current_phase,
                subtask_id=subtask_id,
            )
            if materialization_issue:
                return materialization_issue
            return _mark_phase_subtask_complete(
                ctx,
                subtask_id=subtask_id,
                notes=notes,
                status=status,
                summary=summary,
                evidence=evidence_items,
                completion_contract=None,
            )
        typed_completion = CompletionContract.from_mapping(completion_contract)
        if typed_completion.verification_report is None:
            return (
                "ERROR: mark_subtask_complete contract rejected: "
                "`completion_contract.verification_report` is required in "
                "phase-run context. Run `run_subtask_proof` and copy its "
                "verifier-backed report instead of closing from shell/debug "
                "evidence alone."
            )
        if typed_completion.verification_report.passed is not True:
            return (
                "ERROR: mark_subtask_complete contract rejected: "
                "verification_report.passed must be true. Rerun "
                "`run_subtask_proof` after fixing the implementation; "
                "skip-only or failed proof output cannot close the subtask."
            )
        workspace_id = _workspace_id_from_drive(ctx)
        context = build_workspace_context(
            repo_root=_repo_root_from_phase_ctx(ctx),
            workspace_root=_workspace_root_from_phase_ctx(ctx, workspace_id),
            workspace_id=workspace_id,
            changed_files=typed_completion.changed_files,
        )
        plan = _read_phase_plan(ctx)
        active_subtask = None
        if plan is not None:
            execute = _phase_plan_execute_node(plan)
            subtasks = execute.get("subtasks") if isinstance(execute, dict) else None
            if isinstance(subtasks, list):
                for item in subtasks:
                    if (
                        isinstance(item, dict)
                        and str(item.get("id") or "") == typed_completion.subtask_id
                    ):
                        active_subtask = item
                        break
        contract_issues = ContractValidator.validate(
            ContractBundle(
                run_id=_run_id(ctx),
                workspace_id=workspace_id,
                completions=(typed_completion,),
            ),
            context=context,
        )
        contract_issues.extend(
            validate_completion_materialization(
                typed_completion,
                active_subtask=active_subtask,
                workspace_root=str(context.workspace_root),
                raw_completion=completion_contract,
                phase=_phase_control_phase_id(ctx),
            )
        )
        if contract_issues:
            return _contract_issue_message(
                "mark_subtask_complete contract rejected", contract_issues
            )
        subtask_id = typed_completion.subtask_id
        status = typed_completion.status
        notes = typed_completion.notes
        summary = (
            typed_completion.completed_claims[0].text
            if typed_completion.completed_claims
            else typed_completion.notes
        )
    if _is_phase_run_context(ctx):
        if typed_completion is not None:
            typed_refs = list(typed_completion.evidence_refs)
            for claim in typed_completion.completed_claims:
                typed_refs.extend(claim.proof_refs)
            if typed_completion.verification_report is not None:
                typed_refs.append(
                    typed_completion.verification_report.evidence_ref(
                        phase=_phase_control_phase_id(ctx),
                        subtask_id=typed_completion.subtask_id,
                    )
                )
            evidence_items = [
                f"{ref.ref_type}:{ref.ref_id}"
                for ref in typed_refs
            ]
        return _mark_phase_subtask_complete(
            ctx,
            subtask_id=subtask_id,
            notes=notes,
            status=status,
            summary=summary,
            evidence=evidence_items,
            completion_contract=typed_completion,
        )
    try:
        from ouroboros.tools.control import _mark_subtask_complete as _internal_mark

        internal_result = _internal_mark(
            ctx,
            status=status,
            summary=summary or notes,
            evidence=evidence_items or ([notes] if notes else []),
        )
        if str(internal_result or "").lstrip().startswith("OK:"):
            signal_id = _write_control_signal(
                ctx,
                "mark_subtask_complete",
                _completion_signal_payload(
                    subtask_id=subtask_id,
                    notes=notes,
                    status=status,
                    summary=summary,
                    evidence=evidence_items,
                ),
            )
            return f"{internal_result} Phase signal: {signal_id}."
        if "active_plan_missing" not in str(internal_result):
            return internal_result
    except Exception:
        pass

    return _mark_phase_subtask_complete(
        ctx,
        subtask_id=subtask_id,
        notes=notes,
        status=status,
        summary=summary,
        evidence=evidence_items,
    )


def _completion_memory_quality_issue(
    *,
    subtask_id: str,
    status: str = "done",
    summary: str = "",
    notes: str = "",
    evidence: Any = None,
) -> str:
    if str(status or "done").strip().lower() not in {"done", "ok", "complete", "completed"}:
        return ""
    summary_text = str(summary or notes or "").strip()
    evidence_items = _completion_evidence_items(evidence)
    if summary_text and evidence_items:
        return ""
    missing: list[str] = []
    if not summary_text:
        missing.append("summary")
    if not evidence_items:
        missing.append("evidence")
    return (
        "ERROR: mark_subtask_complete rejected: completion for subtask "
        f"`{subtask_id}` must include non-empty {', '.join(missing)} so "
        "Umbrella can mirror useful, auditable subtask memory. Retry with "
        "the same unquoted subtask_id plus a concise summary and concrete "
        "verification evidence from the passing command."
    )


def _phase_completion_status_issue(*, subtask_id: str, status: str = "done") -> str:
    normalized = str(status or "done").strip().lower()
    if normalized in {"done", "ok", "complete", "completed"}:
        return ""
    return (
        "ERROR: mark_subtask_complete rejected: phase-run subtask "
        f"`{subtask_id or '<missing>'}` can only be closed with status='done'. "
        f"The call supplied status='{status}'. If the subtask is blocked or "
        "failing, keep remediating or call `request_watcher_review`; do not "
        "mark it complete with failed/skipped memory."
    )


def _watcher_force_verify_completion_issue(ctx: ToolContext) -> str:
    overlays = _context_overlays(ctx)
    phase_node = overlays.get("phase_node")
    overlay = phase_node.get("overlay") if isinstance(phase_node, dict) else None
    if not isinstance(overlay, dict) or not overlay.get("watcher_force_verify"):
        return ""
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    rows = _tool_log_rows_for_task(ctx, task_id)
    try:
        row_floor = max(0, int(overlay.get("watcher_force_verify_tool_row_floor") or 0))
    except (TypeError, ValueError):
        row_floor = 0
    try:
        force_after = float(overlay.get("watcher_force_verify_after") or 0.0)
    except (TypeError, ValueError):
        force_after = 0.0

    latest_write_idx = -1
    latest_passing_idx = -1
    latest_passing_time = 0.0
    for idx, row in enumerate(rows):
        if _tool_row_is_successful_repair_write(row):
            latest_write_idx = idx
            continue
        tool = str(row.get("tool") or "")
        if tool not in {"run_subtask_proof", "run_workspace_verify"}:
            continue
        payload = _tool_row_result_payload(row)
        if payload.get("passed") is not True:
            continue
        if tool == "run_workspace_verify":
            if int(payload.get("failed_step_count") or 0) > 0:
                continue
            if not str(payload.get("verify_run_id") or "").strip():
                continue
        row_time = _tool_row_time(row) or 0.0
        if force_after and row_time and row_time < force_after:
            continue
        latest_passing_idx = idx
        latest_passing_time = row_time

    if latest_passing_idx < row_floor:
        return (
            "ERROR: watcher_force_verify is active: run a fresh passing "
            "`run_subtask_proof` or `run_workspace_verify` in this retried "
            "phase before `mark_subtask_complete`."
        )
    if latest_write_idx > latest_passing_idx:
        return (
            "ERROR: watcher_force_verify is active: workspace changed after "
            "the latest passing proof. Rerun `run_subtask_proof` or "
            "`run_workspace_verify` before `mark_subtask_complete`."
        )
    if force_after and latest_passing_time and latest_passing_time < force_after:
        return (
            "ERROR: watcher_force_verify is active: proof evidence is older "
            "than the watcher signal. Rerun proof before completion."
        )
    return ""


def _mark_phase_subtask_complete(
    ctx: ToolContext,
    *,
    subtask_id: str = "",
    notes: str = "",
    status: str = "done",
    summary: str = "",
    evidence: Any = None,
    completion_contract: CompletionContract | None = None,
) -> str:
    """Apply Umbrella phase-plan completion gates and write phase signals."""

    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found"
    evidence_items = _completion_evidence_items(evidence)
    current_phase = _current_phase_node(ctx, plan)
    if not subtask_id:
        view = getattr(ctx, "loop_state_view", None)
        if isinstance(view, dict):
            subtask_id = str(view.get("current_subtask_id") or "")
    if _is_phase_run_context(ctx):
        status_issue = _phase_completion_status_issue(
            subtask_id=subtask_id,
            status=status,
        )
        if status_issue:
            return status_issue
    if _is_phase_run_context(ctx):
        if completion_contract is None and not (
            str(summary or "").strip() or evidence_items
        ):
            return (
                "ERROR: mark_subtask_complete rejected: "
                "completion_contract is required for phase-run completion."
            )
        subtasks = _phase_subtasks(current_phase)
        if subtasks:
            requested = str(subtask_id or "").strip()
            known_ids = {str(item.get("id") or "").strip() for item in subtasks}
            phase_id = str((current_phase or {}).get("id") or "").strip()
            if not requested:
                return "ERROR: subtask_id is required when the current phase has subtask cards"
            if requested not in known_ids and requested != phase_id:
                return f"ERROR: subtask '{requested}' not found in plan"
            first = _first_incomplete_subtask(subtasks)
            first_id = str((first or {}).get("id") or "").strip()
            if first is not None and requested != first_id:
                return (
                    "ERROR: mark_subtask_complete must follow the active phase plan "
                    f"order. Next pending subtask is `{first_id}`; cannot mark "
                    f"`{requested}` complete yet."
                )
        force_verify_issue = _watcher_force_verify_completion_issue(ctx)
        if force_verify_issue:
            return force_verify_issue
    memory_quality_issue = _completion_memory_quality_issue(
        subtask_id=subtask_id,
        status=status,
        summary=summary,
        notes=notes,
        evidence=evidence_items,
    )
    if memory_quality_issue:
        return memory_quality_issue
    if not _is_phase_run_context(ctx):
        memory_claim_issue = _completion_llm_memory_claim_issue(
            subtask_id=subtask_id,
            summary=summary,
            notes=notes,
            evidence=evidence_items,
        )
        if memory_claim_issue:
            return memory_claim_issue
    for node in plan.get("nodes", []):
        for subtask in node.get("subtasks") or []:
            if subtask.get("id") == subtask_id:
                subtask["status"] = "done"
                completion_payload = _completion_signal_payload(
                    subtask_id=subtask_id,
                    notes=notes,
                    status=status,
                    summary=summary,
                    evidence=evidence_items,
                    extra={
                        "completed_at": time.time(),
                        **(
                            {"completion_contract": json_ready(completion_contract)}
                            if completion_contract is not None
                            else {}
                        ),
                    },
                )
                subtask["completion"] = completion_payload
                plan["version"] = plan.get("version", 0) + 1
                _write_phase_plan(ctx, plan)
                _mirror_phase_subtask_completion_to_palace(
                    ctx,
                    plan=plan,
                    phase_node=node,
                    subtask=subtask,
                    completion_payload=completion_payload,
                )
                _write_control_signal(
                    ctx,
                    "mark_subtask_complete",
                    _completion_signal_payload(
                        subtask_id=subtask_id,
                        notes=notes,
                        status=status,
                        summary=summary,
                        evidence=evidence_items,
                        extra=(
                            {"completion_contract": json_ready(completion_contract)}
                            if completion_contract is not None
                            else None
                        ),
                    ),
                )
                return f"OK: Subtask '{subtask_id}' marked complete"
    phase_subtasks = _phase_subtasks(current_phase)
    phase_subtasks_complete = bool(phase_subtasks) and all(
        str(item.get("status") or "") == "done" for item in phase_subtasks
    )
    if (
        _is_phase_run_context(ctx)
        and current_phase is not None
        and (not phase_subtasks or phase_subtasks_complete)
    ):
        phase_id = str(current_phase.get("id") or "").strip()
        requested_subtask_id = str(subtask_id or "").strip()
        if phase_subtasks and requested_subtask_id and requested_subtask_id != phase_id:
            return f"ERROR: subtask '{subtask_id}' not found in plan"
        signal_id = _write_control_signal(
            ctx,
            "mark_subtask_complete",
            _completion_signal_payload(
                subtask_id=phase_id,
                notes=notes,
                status=status,
                summary=summary,
                evidence=evidence_items,
                extra={
                    "requested_subtask_id": requested_subtask_id,
                    "phase_id": phase_id,
                    "phase_level": True,
                    **(
                        {"completion_contract": json_ready(completion_contract)}
                        if completion_contract is not None
                        else {}
                    ),
                },
            ),
        )
        return (
            f"OK: Phase '{phase_id}' completion accepted"
            + (
                " after all internal subtasks were already done"
                if phase_subtasks
                else " without internal subtasks"
            )
            + f" (signal: {signal_id})"
        )
    if not subtask_id:
        return "ERROR: subtask_id is required when the current phase has subtask cards"
    return f"ERROR: subtask '{subtask_id}' not found in plan"


def _mirror_phase_subtask_completion_to_palace(
    ctx: ToolContext,
    *,
    plan: dict[str, Any],
    phase_node: dict[str, Any],
    subtask: dict[str, Any],
    completion_payload: dict[str, Any],
) -> None:
    """Mirror accepted phase-subtask completion into hierarchical memory.

    The phase plan remains canonical. This write makes completed work visible to
    later phase/final-review recall through palace.subtask instead of only
    append-only drive logs.
    """

    try:
        from umbrella.memory.palace.facade import MemPalace

        workspace_id = (
            str(plan.get("workspace_id") or "").strip()
            or _workspace_id_from_drive(ctx)
            or str(_loop_state_view(ctx).get("active_workspace_id") or "").strip()
        )
        if not workspace_id:
            return
        subtask_id = str(subtask.get("id") or completion_payload.get("subtask_id") or "").strip()
        if not subtask_id:
            return
        phase_id = str(phase_node.get("id") or phase_node.get("manifest_id") or "").strip()
        run_id = str(plan.get("run_id") or _run_id(ctx) or "").strip()
        memory_doc = {
            "artifact": "phase_subtask_completion",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "phase_id": phase_id,
            "subtask_id": subtask_id,
            "title": str(subtask.get("title") or ""),
            "proof": subtask.get("proof") if isinstance(subtask.get("proof"), dict) else {},
            "status": str(completion_payload.get("status") or ""),
            "summary": str(completion_payload.get("summary") or ""),
            "evidence": completion_payload.get("evidence") or [],
            "completed_at": completion_payload.get("completed_at"),
        }
        palace = MemPalace(pathlib.Path(getattr(ctx, "repo_dir", "")), workspace_id)
        palace.add(
            store="palace.subtask",
            content=json.dumps(memory_doc, ensure_ascii=False, indent=2),
            tier="hot",
            scope="subtask_scoped",
            tags=["subtask_complete", "execution_artifact", "subtask_card"],
            phase=phase_id or "execute",
            subtask_id=subtask_id,
            run_id=run_id,
            verified=True,
            source_path=".memory/drive/state/phase_plan.json",
            extra={"status": str(completion_payload.get("status") or "")},
        )
    except Exception:
        pass


_PHASE_PLAN_POLICY_AUDIT_KEYS = {
    "completion",
    "edits_log",
    "overlay",
    "plan_revision_patch",
    "required_deltas",
    "reason_code",
    "evidence_refs",
}


def _phase_plan_policy_payload(
    plan: dict[str, Any],
    *,
    touched_subtask_ids: set[str] | None = None,
    subtask_patch_fields_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: scrub(child)
                for key, child in value.items()
                if key not in _PHASE_PLAN_POLICY_AUDIT_KEYS
            }
        if isinstance(value, list):
            return [scrub(child) for child in value]
        return value

    cleaned = scrub(plan)
    if not isinstance(cleaned, dict):
        return {}
    touched = {str(item or "").strip() for item in (touched_subtask_ids or set())}
    if not touched:
        return cleaned
    patch_fields_by_id = subtask_patch_fields_by_id or {}

    def scoped_mutated_subtask(subtask: dict[str, Any]) -> dict[str, Any]:
        subtask_id = str(
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or subtask.get("name")
            or ""
        ).strip()
        patch_fields = patch_fields_by_id.get(subtask_id, {})
        scoped: dict[str, Any] = {
            "id": subtask_id,
            "title": subtask_id or "execute mutation",
            "goal": "Validate the current execute-time phase-plan mutation.",
        }
        for key, value in patch_fields.items():
            if (
                key == "id"
                or key in _PHASE_PLAN_MERGE_LIST_KEYS
                or key in _PHASE_PLAN_POLICY_AUDIT_KEYS
            ):
                continue
            scoped[key] = value
        for key in _PHASE_PLAN_MERGE_LIST_KEYS:
            if key in patch_fields:
                scoped[key] = _phase_plan_string_items(patch_fields.get(key))
        return scoped

    nodes = cleaned.get("nodes")
    if not isinstance(nodes, list):
        return cleaned
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("id") or node.get("manifest_id") or "").strip() != "execute":
            continue
        subtasks = node.get("subtasks")
        if not isinstance(subtasks, list):
            continue
        filtered: list[Any] = []
        for subtask in subtasks:
            if not isinstance(subtask, dict):
                filtered.append(subtask)
                continue
            subtask_id = str(
                subtask.get("id")
                or subtask.get("subtask_id")
                or subtask.get("title")
                or subtask.get("name")
                or ""
            ).strip()
            if subtask_id not in touched:
                continue
            filtered.append(scoped_mutated_subtask(subtask))
        node["subtasks"] = filtered
    return cleaned


__all__ = [
    '_apply_plan_revision_patch',
    '_mutate_phase_plan',
    '_add_phase',
    '_loop_back_to',
    '_submit_research_summary',
    '_submit_micro_review',
    '_submit_phase_plan',
    '_submit_final_review',
    '_submit_verification',
    '_submit_reflection',
    '_accept_bkb_proposal',
    '_submit_preflight_report',
    '_mentions_unresolved_pass_blocker',
    '_preflight_blockers_are_implementation_issues',
    '_edit_subtask_card',
    '_request_scope_change',
    '_request_watcher_review',
    '_provision_workspace_environment',
    '_mirror_watcher_review_to_palace',
    '_mirror_phase_plan_mutation_to_palace',
    '_harness_run',
    '_run_subtask_proof',
    '_mark_subtask_complete',
    '_mirror_phase_subtask_completion_to_palace',
    '_phase_subtask_completion_issue',
]
