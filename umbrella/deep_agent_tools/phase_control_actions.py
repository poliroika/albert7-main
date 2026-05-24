"""Phase-control action handlers exposed as tools."""

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
    compile_phase_plan,
    diff_hash,
    hash_value,
    json_ready,
    validate_completion_materialization,
    validate_review_contract,
    validate_verification_report_ref,
    workspace_hash,
)
from umbrella.deep_agent_tools.phase_control_retry import _phase_subtask_completion_issue


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


def _completion_llm_memory_claim_issue(**_kwargs: Any) -> str:
    return ""


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
        target = by_id.get(subtask_id)
        if target is None:
            return [], f"subtask '{subtask_id}' not found in execute phase"
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
        validated.append((item, target, subtask_id))

    list_patch_ops = _phase_plan_list_patch_key_ops()
    applied: list[str] = []
    for item, target, subtask_id in validated:
        for key, (base, op) in list_patch_ops.items():
            if key not in item:
                continue
            value = item[key]
            if op in {"replace", "set"}:
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
            else:
                target[key] = value
        applied.append(f"subtasks.{subtask_id}")
    return applied, None


def _mutate_phase_plan(ctx: ToolContext, *, patch: dict[str, Any]) -> str:
    if stop := _stop_requested_message(ctx, "mutate_phase_plan"):
        return stop
    if not isinstance(patch, dict):
        return "ERROR: mutate_phase_plan patch must be an object"
    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found — cannot mutate"
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


def _invalidate_plan_review_after_phase_plan_submit(
    ctx: ToolContext,
    *,
    selected_plan_id: str,
) -> None:
    """A newly submitted plan must be reviewed before execute can consume it."""

    plan = _read_phase_plan(ctx)
    if not isinstance(plan, dict):
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
) -> str:
    if stop := _stop_requested_message(ctx, "submit_micro_review"):
        return stop
    if revisions:
        return (
            "ERROR: submit_micro_review contract rejected: legacy `revisions` "
            "text is not accepted; use typed ReviewIssue objects in `issues`."
        )
    if issues is None:
        return "ERROR: submit_micro_review contract rejected: `issues` is required."
    contract = ReviewContract.from_mapping(
        {
            "verdict": verdict,
            "issues": issues,
            "loop_back_target": loop_back_target,
            "notes": notes,
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
        contract_issues = ContractValidator.validate(bundle, context=context)
        if contract_issues:
            return _contract_issue_message(
                "phase plan contract rejected", contract_issues
            )
    _record_submitted_phase_plan_artifact(
        ctx,
        payload=payload,
        plan_id=selected_plan_id,
        notes=notes,
    )
    _invalidate_plan_review_after_phase_plan_submit(
        ctx,
        selected_plan_id=selected_plan_id,
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


def _submit_preflight_report(ctx: ToolContext, *, status: str, blockers: list[str] | None = None) -> str:
    if stop := _stop_requested_message(ctx, "submit_preflight_report"):
        return stop
    if status not in ("ready", "blocked"):
        return f"ERROR: status must be ready or blocked, got '{status}'"
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


def _request_watcher_review(ctx: ToolContext, *, reason: str) -> str:
    if stop := _stop_requested_message(ctx, "request_watcher_review"):
        return stop
    from umbrella.deep_agent_tools.phase_control_retry import (
        _phase_subtask_retry_watcher_review_payload,
    )

    review = _phase_subtask_retry_watcher_review_payload(ctx, reason=reason)
    if review.get("status") not in {"review_recorded", "review_not_required"}:
        return json.dumps(review, ensure_ascii=False, indent=2)
    signal_id = _write_control_signal(ctx, "request_watcher_review", review)
    review = dict(review)
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

    raw = workspace_commands.run_workspace_command(
        ctx,
        workspace_id=workspace_id,
        command=command,
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
    passed = (
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
    report_hash = hash_value(
        {
            "subtask_id": resolved_id,
            "passed": passed,
            "exit_code": exit_code,
            "proof_kind": proof.execution.kind,
            "workspace_hash": ws_hash,
            "diff_hash": diff_h,
            "skip_only": skip_only,
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
    typed_completion: CompletionContract | None = None
    if _is_phase_run_context(ctx):
        if not isinstance(completion_contract, dict):
            plan = _read_phase_plan(ctx)
            legacy_issue = _phase_subtask_completion_issue(
                ctx,
                current_phase=_current_phase_node(ctx, plan) if plan else None,
                subtask_id=subtask_id,
            )
            if legacy_issue:
                return legacy_issue
            return (
                "ERROR: mark_subtask_complete contract rejected: "
                "`completion_contract` is required in phase-run context."
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
    evidence_items = _completion_evidence_items(evidence)
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
        if completion_contract is None:
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
    '_request_watcher_review',
    '_mirror_watcher_review_to_palace',
    '_mirror_phase_plan_mutation_to_palace',
    '_harness_run',
    '_run_subtask_proof',
    '_mark_subtask_complete',
    '_mirror_phase_subtask_completion_to_palace',
    '_phase_subtask_completion_issue',
]
