"""Phase-control action handlers exposed as tools."""

import copy

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools.phase_control_base import *
from umbrella.deep_agent_tools.phase_control_legacy import _subtask_success_test_text
from umbrella.deep_agent_tools.phase_control_research import *
from umbrella.deep_agent_tools.phase_control_retry import (
    _final_review_e2e_gate,
    _phase_control_signal_rows_for_task,
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
    ReviewContract,
    ResearchSummaryContract,
    VerificationReportRef,
    build_workspace_context,
    canonicalize_phase_plan,
    compile_phase_plan,
    diff_hash,
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
from umbrella.deep_agent_tools.phase_control_retry import (
    _completion_llm_memory_claim_issue,
    _phase_subtask_completion_issue,
)


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
        target.get("proof"),
        patch_item.get("proof"),
        replace_required_properties=bool(
            _contract_migration_reason_from_patch(patch_item)
            and _contract_migration_files_from_patch(patch_item)
        ),
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
    if (
        getattr(previous.execution, "kind", "") != "pytest"
        or getattr(updated.execution, "kind", "") != "pytest"
        or "no_test_tampering" not in updated.oracle.required_properties
    ):
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


def _merge_phase_plan_proof_patch(
    existing: Any,
    patch: Any,
    *,
    replace_required_properties: bool = False,
) -> Any:
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
            merged[key] = _merge_phase_plan_proof_patch(
                merged.get(key),
                value,
                replace_required_properties=replace_required_properties,
            )
        elif key == "required_properties":
            if replace_required_properties:
                merged[key] = _phase_plan_string_items(value)
            else:
                merged[key] = _merge_phase_plan_string_list(merged.get(key), value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


_PHASE_PLAN_SEMANTIC_CONTRACT_KEYS = frozenset(
    {
        "success_test",
        "proof",
        "proof_contract",
        "generated_test_contract",
        "files_under_test",
        "test_oracle",
        "acceptance_criteria",
    }
)


def _contract_migration_declared(item: dict[str, Any]) -> bool:
    return bool(
        _contract_migration_reason_from_patch(item)
        or _contract_migration_files_from_patch(item)
        or str(item.get("contract_migration_id") or "").strip()
        or str(item.get("contract_migration_token") or "").strip()
    )


def _contract_migration_has_semantic_patch(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if any(key in item for key in _PHASE_PLAN_SEMANTIC_CONTRACT_KEYS):
        return True
    list_patch_ops = _phase_plan_list_patch_key_ops()
    for key, (base, _op) in list_patch_ops.items():
        if key in item and base in _PHASE_PLAN_SEMANTIC_CONTRACT_KEYS:
            return True
    proof = item.get("proof")
    if isinstance(proof, dict) and proof:
        return True
    proof_contract = item.get("proof_contract")
    return isinstance(proof_contract, dict) and bool(proof_contract)


def _legacy_phase_subtask_materialization_issue(
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
    validated: list[tuple[dict[str, Any], dict[str, Any], str]] = []
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
        if (
            _contract_migration_declared(item)
            and not _contract_migration_has_semantic_patch(item)
        ):
            return [], (
                "contract migration must change proof/test/oracle contract; "
                "metadata-only contract_migration_reason/files patches are "
                "not accepted."
            )
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
        migration_issue = _active_success_test_contract_migration_issue(
            ctx,
            plan=plan,
            subtask=target,
            subtask_id=subtask_id,
            item=item,
        )
        if migration_issue:
            return [], migration_issue
        proof_issue = _mutated_subtask_proof_issue(
            target, item, subtask_id=subtask_id
        )
        if proof_issue:
            return [], proof_issue
        validated.append((item, target, subtask_id))

    list_patch_ops = _phase_plan_list_patch_key_ops()
    applied: list[str] = []
    for item, target, subtask_id in validated:
        for key, (base, op) in list_patch_ops.items():
            if key not in item:
                continue
            value = item[key]
            if op == "merge":
                target[base] = _merge_phase_plan_string_list(target.get(base), value)
            elif op in {"replace", "set"}:
                target[base] = _phase_plan_string_items(value)
            elif op == "remove":
                remove_set = set(_phase_plan_string_items(value))
                target[base] = [
                    path
                    for path in _phase_plan_string_items(target.get(base))
                    if path not in remove_set
                ]
        for key, value in item.items():
            if key == "id" or key in list_patch_ops:
                continue
            if key == "success_test":
                target[key] = _patched_success_test(target.get(key), value)
            elif key in _CONTRACT_MIGRATION_REASON_KEYS:
                reason = _contract_migration_reason_from_patch(item)
                if reason:
                    alias_issue = _supported_llm_alias_memory_claim_issue(reason)
                    if alias_issue:
                        return (
                            [],
                            "contract_migration_reason for subtask "
                            f"'{subtask_id}' {alias_issue}",
                        )
                    target["contract_migration_reason"] = reason
            elif key in _CONTRACT_MIGRATION_FILE_KEYS:
                target["contract_migration_files"] = _contract_migration_files_from_patch(
                    item
                )
            elif key in _PHASE_PLAN_MERGE_LIST_KEYS:
                target[key] = _merge_phase_plan_string_list(target.get(key), value)
            elif key == "proof":
                target[key] = _merge_phase_plan_proof_patch(
                    target.get(key),
                    value,
                    replace_required_properties=bool(
                        _contract_migration_reason_from_patch(item)
                        and _contract_migration_files_from_patch(item)
                    ),
                )
            else:
                target[key] = value
        applied.append(f"subtasks.{subtask_id}")
    return applied, None


def _mutate_phase_plan(
    ctx: ToolContext,
    *,
    patch: dict[str, Any],
    target_subtask_id: str = "",
    subtask_id: str = "",
) -> str:
    if stop := _stop_requested_message(ctx, "mutate_phase_plan"):
        return stop
    if not isinstance(patch, dict):
        return "ERROR: mutate_phase_plan patch must be an object"
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
    contract_migration_keys = {
        *_CONTRACT_MIGRATION_REASON_KEYS,
        *_CONTRACT_MIGRATION_FILE_KEYS,
        "contract_migration_id",
        "contract_migration_token",
    }
    top_level_contract_migration = {
        key: patch.get(key)
        for key in contract_migration_keys
        if key in patch
    }
    if top_level_contract_migration:
        patch = {
            key: value
            for key, value in patch.items()
            if key not in contract_migration_keys
        }
        existing_subtasks = patch.get("subtasks")
        if isinstance(existing_subtasks, list) and existing_subtasks:
            first = existing_subtasks[0]
            if not isinstance(first, dict):
                return (
                    "ERROR: top-level contract migration patch cannot merge into "
                    "a non-object first subtask patch."
                )
            patch["subtasks"] = [
                {**top_level_contract_migration, **first},
                *existing_subtasks[1:],
            ]
        else:
            execute = _phase_plan_execute_node(plan)
            active = _first_incomplete_subtask(_phase_subtasks(execute))
            if not isinstance(active, dict):
                return (
                    "ERROR: top-level contract migration patch could not resolve "
                    "the active execute subtask; use patch.subtasks[{id,...}]."
                )
            subtask_id = str(active.get("id") or "").strip()
            if not subtask_id:
                return (
                    "ERROR: top-level contract migration patch resolved an "
                    "active subtask without id; use patch.subtasks[{id,...}]."
                )
            patch = {
                "subtasks": [
                    {"id": subtask_id, **top_level_contract_migration, **patch}
                ]
            }
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
            "ERROR: unsupported mutate_phase_plan patch keys: "
            + ", ".join(unsupported)
        )
    if not applied:
        return "ERROR: mutate_phase_plan patch did not apply any changes"
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
    signal_id = _write_control_signal(ctx, "mutate_phase_plan", signal_payload)
    _mirror_phase_plan_mutation_to_palace(
        ctx,
        plan=plan,
        payload=signal_payload,
        signal_id=signal_id,
    )
    return f"PhasePlan mutated (version {plan['version']}): {applied} (signal: {signal_id})"


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
            "mutate_phase_plan only when changing proof/oracle/contract shape, "
            "especially for test files."
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


def _submit_micro_review(
    ctx: ToolContext,
    *,
    verdict: str,
    issues: list[dict[str, Any]] | None = None,
    revisions: list[str] | None = None,
    loop_back_target: str = "",
    notes: str = "",
    coverage: dict[str, Any] | None = None,
    required_plan_changes: list[str] | None = None,
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
    if feedback_issue := _micro_review_feedback_issue(
        verdict=verdict,
        revisions=revisions,
        notes=notes,
    ):
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


def _submit_plan_revision_contract_issues(ctx: ToolContext, plan: dict[str, Any]) -> list[str]:
    overlays = getattr(ctx, "context_overlays", {}) or {}
    phase_node = overlays.get("phase_node") if isinstance(overlays, dict) else None
    overlay = phase_node.get("overlay") if isinstance(phase_node, dict) else None
    if not isinstance(overlay, dict):
        return []
    reason = str(overlay.get("retry_reason") or "").strip().lower()
    if not reason.startswith("micro review requested revisions"):
        return []
    contract = overlay.get("revision_contract")
    if not isinstance(contract, dict):
        return []
    raw_revisions = contract.get("required_plan_changes") or contract.get("revisions") or []
    revisions = [str(item).strip() for item in raw_revisions if str(item).strip()]
    if not revisions:
        return []
    plan_text = json.dumps(plan, ensure_ascii=False).lower()
    stopwords = {
        "with",
        "from",
        "into",
        "that",
        "this",
        "phase",
        "project",
        "subtask",
        "subtasks",
        "add",
        "these",
        "fields",
        "provide",
        "specify",
        "exactly",
        "validates",
        "pytest-cov",
        "platform-appropriate",
    }
    issues: list[str] = []
    for revision in revisions:
        revision_l = revision.lower()
        if re.match(r"\s*(?:consider|optional|maybe|could|nice to have)\b", revision_l):
            continue
        if "replace" in revision_l and " with " in revision_l:
            positive = revision_l.split(" with ", 1)[1]
        elif "revision requires" in revision_l:
            positive = revision_l.split("revision requires", 1)[1]
        else:
            positive = revision_l
        semantic_numbers = re.findall(
            r"\b(\d+(?:\.\d+)?)\s*(?:times?|retries?|attempts?|%)\b",
            positive,
        )
        missing_numbers = [
            number for number in semantic_numbers if number not in plan_text
        ]
        if missing_numbers:
            issues.append(
                "review revision numeric requirement appears unaddressed: "
                f"`{revision}`; missing number(s): "
                + ", ".join(missing_numbers[:8])
            )
            continue
        alternatives = [
            item.strip()
            for item in re.split(r"\bor\b", positive)
            if item.strip()
        ] or [positive]
        missing_by_alternative: list[list[str]] = []
        revision_satisfied = False
        for alternative in alternatives:
            keywords = [
                item
                for raw in re.findall(r"[a-z0-9_.-]{4,}", alternative)
                for item in (raw.strip("._-"),)
                if item and item not in stopwords
            ]
            if not keywords:
                revision_satisfied = True
                break
            covered = [item for item in keywords if item in plan_text]
            floor = 1 if len(alternatives) > 1 else 2
            required = min(len(keywords), max(floor, (len(keywords) + 1) // 2))
            if len(covered) >= required:
                revision_satisfied = True
                break
            missing_by_alternative.append([item for item in keywords if item not in covered])
        if revision_satisfied:
            continue
        missing = min(missing_by_alternative, key=len) if missing_by_alternative else []
        issues.append(
            "review revision appears unaddressed: "
            f"`{revision}`; missing keyword(s): " + ", ".join(missing[:8])
        )
    return issues


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
        revision_issues = _submit_plan_revision_contract_issues(ctx, plan_payload)
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


def _submit_final_review(ctx: ToolContext, *, outcome: str, notes: str = "") -> str:
    if stop := _stop_requested_message(ctx, "submit_final_review"):
        return stop
    if outcome not in ("ok", "loop_back"):
        return f"ERROR: outcome must be ok or loop_back, got '{outcome}'"
    if outcome == "ok":
        gate = _final_review_e2e_gate(ctx)
        if gate:
            return gate
    signal_id = _write_control_signal(ctx, "submit_final_review", {
        "outcome": outcome,
        "notes": notes,
    })
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
        needs_driver = bool(required_props - _RUNTIME_STARTED_ONLY_PROPERTIES)
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


def _request_watcher_review(ctx: ToolContext, *, reason: str) -> str:
    if stop := _stop_requested_message(ctx, "request_watcher_review"):
        return stop
    from umbrella.deep_agent_tools.phase_control_retry import (
        _phase_subtask_retry_watcher_review_payload,
    )

    review = _phase_subtask_retry_watcher_review_payload(ctx, reason=reason)
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
            source_path=".memory/drive/state/phase_control_signals.jsonl",
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
            allow_dependency_install=proof.execution.kind in {"build", "command"},
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
            legacy_issue = _phase_subtask_completion_issue(
                ctx,
                current_phase=current_phase,
                subtask_id=subtask_id,
            )
            if legacy_issue:
                return legacy_issue
            if subtasks:
                return (
                    "ERROR: mark_subtask_complete contract rejected: "
                    "completion_contract is required for phase-run subtask "
                    "completion. Run `run_subtask_proof` and pass its "
                    "`completion_contract_hint` unchanged as "
                    "`completion_contract`; summary/evidence-only completion "
                    "drops verifier proof_refs and verification_report."
                )
            materialization_issue = _legacy_phase_subtask_materialization_issue(
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


def _patched_success_test(existing: Any, replacement: Any) -> Any:
    if isinstance(replacement, dict):
        return dict(replacement)
    value = str(replacement or "")
    if isinstance(existing, dict):
        updated = dict(existing)
        updated.setdefault("kind", "cmd")
        updated["value"] = value
        return updated
    return value


_CONTRACT_MIGRATION_REASON_KEYS = (
    "contract_migration_reason",
    "test_contract_migration_reason",
    "success_test_contract_migration_reason",
    "contract_migration",
    "test_contract_migration",
    "success_test_contract_migration",
)

_CONTRACT_MIGRATION_FILE_KEYS = (
    "contract_migration_files",
    "test_contract_migration_files",
    "success_test_contract_migration_files",
)

_PHASE_PLAN_POLICY_AUDIT_KEYS = {
    "completion",
    "edits_log",
    "overlay",
    *_CONTRACT_MIGRATION_REASON_KEYS,
    *_CONTRACT_MIGRATION_FILE_KEYS,
}


def _contract_migration_files_from_patch(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    raw_files: Any = None
    for key in _CONTRACT_MIGRATION_FILE_KEYS:
        if item.get(key) is not None:
            raw_files = item.get(key)
            break
    if raw_files is None:
        raw_files = item.get("files") or item.get("file_paths")
    if isinstance(raw_files, str):
        values = [raw_files]
    elif isinstance(raw_files, (list, tuple, set, frozenset)):
        values = list(raw_files)
    else:
        values = []
    return [
        str(file_path or "").replace("\\", "/").strip().lstrip("/")
        for file_path in values
        if str(file_path or "").strip()
    ]


_ACTIVE_TEST_MIGRATION_BAD_REASON_FRAGMENTS = (
    "clean architecture",
    "clean architectural",
    "differs from a clean",
    "differs from the implementation",
    "differs from implementation",
    "different public api",
    "generated test expectations",
    "match generated test",
    "match the generated test",
    "rewriting the implementation",
    "rewrite the implementation",
    "line ending",
    "crlf",
    "truncation",
    "truncated",
    "import failure",
    "import failures",
)

_ACTIVE_TEST_MIGRATION_EVIDENCE_FRAGMENTS = (
    "contradiction",
    "contradicts",
    "contradictory",
    "self-contradictory",
    "self-inconsistent",
    "self-consistent failure",
    "self-match",
    "self matches",
    "self-matches",
    "matches itself",
    "test scans itself",
    "violates its own",
    "sample violates",
    "warning context",
    "warning contexts",
    "correctly warns",
    "warns not to use",
    "not to use that alias",
    "negative warning",
    "forbidden pattern",
    "forbidden_patterns",
    "internally inconsistent",
    "structurally impossible",
    "cannot be satisfied",
    "cannot satisfy",
    "no valid",
    "flat key",
    "nested dictionaries",
    "impossible assertion",
    "wrong assertion",
    "assertion must be",
    "expected",
    "even though",
    "setup computes",
    "miscomputed",
    "miscalculated",
    "typo",
    "misspelled",
    "accepted plan",
    "declared plan",
)


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
        if "success_test" in patch_fields:
            scoped["success_test"] = _patched_success_test(
                subtask.get("success_test"), patch_fields.get("success_test")
            )
        elif "success_test" in subtask:
            scoped["success_test"] = subtask.get("success_test")
        for key, value in patch_fields.items():
            if (
                key == "id"
                or key == "success_test"
                or key in _PHASE_PLAN_MERGE_LIST_KEYS
                or key in _PHASE_PLAN_POLICY_AUDIT_KEYS
            ):
                continue
            scoped[key] = value
        for key in _PHASE_PLAN_MERGE_LIST_KEYS:
            scoped.pop(key, None)
        success_text = _subtask_success_test_text(scoped)
        success_paths: list[str] = []
        for key in _PHASE_PLAN_MERGE_LIST_KEYS:
            for path in _phase_plan_string_items(subtask.get(key)):
                if path not in success_paths and _success_test_mentions_path(
                    success_text, path
                ):
                    success_paths.append(path)
        for key in _PHASE_PLAN_MERGE_LIST_KEYS:
            values: list[str] = []
            if key in patch_fields:
                values.extend(_phase_plan_string_items(patch_fields.get(key)))
            values.extend(path for path in success_paths if path not in values)
            if values:
                scoped[key] = values
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


def _success_test_mentions_path(success_text: str, rel_path: str) -> bool:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not norm:
        return False
    return norm in str(success_text or "").replace("\\", "/")


def _active_test_migration_has_evidence(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        fragment in lowered for fragment in _ACTIVE_TEST_MIGRATION_EVIDENCE_FRAGMENTS
    )


def _valid_contract_migration_watcher_payload(
    payload: dict[str, Any], *, subtask_id: str, success_test: str
) -> dict[str, Any] | None:
    if not payload:
        return None
    if str(payload.get("status") or "") != "review_recorded":
        return None
    if str(payload.get("reviewer") or "") != "umbrella":
        return None
    if str(payload.get("review_kind") or "") != "retry_watcher":
        return None
    if str(payload.get("subtask_id") or "").strip() != str(subtask_id or "").strip():
        return None
    if str(payload.get("success_test") or "").strip() != str(success_test or "").strip():
        return None
    try:
        failed_attempts = int(payload.get("failed_attempts") or 0)
    except (TypeError, ValueError):
        return None
    if failed_attempts < 1:
        return None
    return payload


def _contract_migration_watcher_payloads(
    ctx: ToolContext, *, state: dict[str, Any], subtask_id: str, success_test: str
) -> list[dict[str, Any]]:
    task_id = str(state.get("task_id") or getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
        return []
    payloads: list[dict[str, Any]] = []
    for row in _tool_log_rows_for_task(ctx, task_id):
        if str(row.get("tool") or "") != "request_watcher_review":
            continue
        payload = _valid_contract_migration_watcher_payload(
            _tool_row_result_payload(row),
            subtask_id=subtask_id,
            success_test=success_test,
        )
        if payload:
            payloads.append(payload)
    for row in _phase_control_signal_rows_for_task(
        ctx, task_id, kind="request_watcher_review"
    ):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        payload = _valid_contract_migration_watcher_payload(
            payload,
            subtask_id=subtask_id,
            success_test=success_test,
        )
        if payload:
            payloads.append(payload)
    return payloads


def _contract_migration_watcher_evidence_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "operator_reason",
        "reason",
        "message",
        "recommendation",
        "patch_guidance",
        "next_step",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    latest_failure = payload.get("latest_failure")
    if isinstance(latest_failure, dict):
        for key in ("reason", "output", "output_excerpt", "stderr", "stdout"):
            value = latest_failure.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    contract_migration = payload.get("contract_migration")
    if isinstance(contract_migration, dict):
        for key in ("verdict", "evidence", "reason"):
            value = contract_migration.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    if not parts:
        try:
            parts.append(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
    return "\n".join(parts)


def _contract_migration_watcher_supports_files(
    payload: dict[str, Any], targeted_files: list[str]
) -> bool:
    contract_migration = payload.get("contract_migration")
    if not isinstance(contract_migration, dict):
        return False
    verdict = str(contract_migration.get("verdict") or "").strip()
    if verdict not in {
        "bad_generated_success_test_contract",
        "bad_success_test_contract",
    }:
        return False
    raw_files = (
        contract_migration.get("target_files")
        or contract_migration.get("files")
        or contract_migration.get("contract_migration_files")
    )
    if isinstance(raw_files, str):
        values = [raw_files]
    elif isinstance(raw_files, (list, tuple, set, frozenset)):
        values = list(raw_files)
    else:
        values = []
    supported = {
        str(file_path or "").replace("\\", "/").strip().lstrip("/").lower()
        for file_path in values
        if str(file_path or "").strip()
    }
    if not supported:
        return False
    for file_path in targeted_files:
        norm = str(file_path or "").replace("\\", "/").strip().lstrip("/").lower()
        if norm in supported:
            return True
    return False


def _active_success_test_contract_migration_issue(
    ctx: ToolContext,
    *,
    plan: dict[str, Any],
    subtask: dict[str, Any],
    subtask_id: str,
    item: dict[str, Any],
) -> str | None:
    try:
        current_phase = _current_phase_node(ctx, plan)
        if not isinstance(current_phase, dict):
            return None
        if str(current_phase.get("id") or "").strip() != "execute":
            return None
        first = _first_incomplete_subtask(_phase_subtasks(current_phase))
        if not isinstance(first, dict):
            return None
        if str(first.get("id") or "").strip() != subtask_id:
            return None
        state = _phase_subtask_retry_state(ctx)
        if not state or int(state.get("failures") or 0) < 1:
            return None
        success_text = str(
            state.get("success_test") or _subtask_success_test_text(subtask)
        )
        files = _contract_migration_files_from_patch(item)
        targeted_files = [
            file_path
            for file_path in files
            if _success_test_mentions_path(success_text, file_path)
        ]
        if not targeted_files:
            return None
        reason = _contract_migration_reason_from_patch(item)
        lowered = reason.lower()
        bad_fragment = next(
            (
                fragment
                for fragment in _ACTIVE_TEST_MIGRATION_BAD_REASON_FRAGMENTS
                if fragment in lowered
            ),
            "",
        )
        watcher_payloads = _contract_migration_watcher_payloads(
            ctx,
            state=state,
            subtask_id=subtask_id,
            success_test=success_text,
        )
        has_evidence = (
            _active_test_migration_has_evidence(reason)
            or any(
                _contract_migration_watcher_supports_files(payload, targeted_files)
                for payload in watcher_payloads
            )
            or any(
                _active_test_migration_has_evidence(
                    _contract_migration_watcher_evidence_text(payload)
                )
                for payload in watcher_payloads
            )
        )
        if not reason or bad_fragment or not has_evidence:
            return (
                "declared success-test contract migration for subtask "
                f"'{subtask_id}' targets {targeted_files} after failing "
                f"`{success_text}` without proving the generated test is "
                "internally contradictory, typoed, impossible, or contrary to "
                "the accepted plan. Repair the implementation against the "
                "declared success test; do not use contract migration for API "
                "preference, clean-architecture preference, patch/line-ending "
                "problems, or import failures."
            )
    except Exception:
        return None
    return None


def _contract_migration_reason_from_patch(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in _CONTRACT_MIGRATION_REASON_KEYS:
        value = item.get(key)
        if isinstance(value, dict):
            text = str(value.get("reason") or value.get("summary") or "").strip()
        else:
            text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = [
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
    '_mirror_watcher_review_to_palace',
    '_mirror_phase_plan_mutation_to_palace',
    '_harness_run',
    '_run_subtask_proof',
    '_mark_subtask_complete',
    '_mirror_phase_subtask_completion_to_palace',
    '_phase_subtask_completion_issue',
]
