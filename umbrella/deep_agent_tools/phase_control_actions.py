"""Phase-control action handlers exposed as tools."""

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools.phase_control_base import *
from umbrella.deep_agent_tools.phase_control_research import *
from umbrella.deep_agent_tools.phase_control_review import *
from umbrella.deep_agent_tools.phase_control_completion import *


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

_PHASE_PLAN_MERGE_LIST_KEYS = {
    "files_to_create",
    "files_to_change",
    "files_affected",
}

_PHASE_PLAN_POLICY_AUDIT_KEYS = {
    "completion",
    "edits_log",
    "overlay",
    *_CONTRACT_MIGRATION_REASON_KEYS,
    *_CONTRACT_MIGRATION_FILE_KEYS,
}


def _phase_plan_policy_payload(
    plan: dict[str, Any],
    *,
    touched_subtask_ids: set[str] | None = None,
    subtask_patch_fields_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the executable-content view used for mutate validation.

    A submitted plan is already accepted as a whole. During execute, a small
    subtask-card mutation should re-check the changed executable content, not
    re-grade stale runtime overlays, completed cards, or future cards against
    the workspace state that has evolved since plan submission.
    """

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
            for path in _string_list_values(subtask.get(key)):
                if path not in success_paths and _success_test_mentions_path(
                    success_text, path
                ):
                    success_paths.append(path)
        for key in _PHASE_PLAN_MERGE_LIST_KEYS:
            values: list[str] = []
            if key in patch_fields:
                values.extend(_string_list_values(patch_fields.get(key)))
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


def _string_list_values(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = str(item or "").replace("\\", "/").strip().strip("`'\"")
        if text:
            result.append(text)
    return result


def _merge_phase_plan_string_list(existing: Any, patch_value: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*_string_list_values(existing), *_string_list_values(patch_value)]:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


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
    """Guard the narrow escape hatch for changing a failed success-test file."""

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

    applied: list[str] = []
    for item, target, subtask_id in validated:
        for key, value in item.items():
            if key == "id":
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


def _mirror_phase_plan_mutation_to_palace(
    ctx: ToolContext,
    *,
    plan: dict[str, Any],
    payload: dict[str, Any],
    signal_id: str,
) -> None:
    """Mirror accepted plan mutations into hierarchical memory.

    The PhasePlan remains canonical. This mirror makes mid-execution contract
    migrations and subtask-card changes visible to later execute retries,
    final review, and operator inspection without replaying raw JSONL logs.
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
            reason = _contract_migration_reason_from_patch(item)
            memory_doc = {
                "artifact": "phase_plan_mutation",
                "run_id": run_id,
                "workspace_id": workspace_id,
                "phase_id": phase_id,
                "subtask_id": subtask_id,
                "applied": payload.get("applied") or [],
                "patch": item,
                "contract_migration_reason": reason,
                "phase_plan_version": payload.get("version"),
                "signal_id": signal_id,
            }
            tags = ["phase_plan_mutation", "subtask_card"]
            if reason:
                tags.append("contract_migration")
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
                    "contract_migration": bool(reason),
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
    policy_issue = _review_revision_policy_issue(ctx, reason=reason)
    if policy_issue:
        return policy_issue
    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found"
    found = False
    for node in plan.get("nodes", []):
        if node["id"] == phase:
            node["status"] = "pending"
            found = True
            break
    if not found:
        return f"ERROR: phase '{phase}' not found in plan"
    plan["version"] = plan.get("version", 0) + 1
    _write_phase_plan(ctx, plan)
    signal_id = _write_control_signal(ctx, "loop_back_to", {"phase": phase, "reason": reason})
    return f"Looping back to phase '{phase}' (signal {signal_id})"


def _submit_research_summary(
    ctx: ToolContext, *, architecture_id: str, findings_ids: list[str], notes: str = ""
) -> str:
    if stop := _stop_requested_message(ctx, "submit_research_summary"):
        return stop
    issue = _research_summary_validation_issue(
        ctx,
        architecture_id=architecture_id,
        findings_ids=findings_ids,
        notes=notes,
    )
    if issue:
        return issue
    canonical_findings = _normalise_research_finding_ids(ctx, findings_ids)
    _record_research_summary_artifact(
        ctx,
        architecture_id=architecture_id,
        findings_ids=canonical_findings,
        notes=notes,
    )
    signal_id = _write_control_signal(ctx, "submit_research_summary", {
        "architecture_id": architecture_id,
        "findings_ids": canonical_findings,
        "notes": notes,
    })
    return f"OK: Research summary submitted (architecture: {architecture_id}, findings: {len(canonical_findings)}, signal: {signal_id})"


def _submit_micro_review(
    ctx: ToolContext, *, verdict: str, revisions: list[str] | None = None, notes: str = ""
) -> str:
    if stop := _stop_requested_message(ctx, "submit_micro_review"):
        return stop
    if verdict not in ("ok", "revise", "abort"):
        return f"ERROR: verdict must be one of ok/revise/abort, got '{verdict}'"
    feedback_issue = _micro_review_feedback_issue(
        verdict=verdict, revisions=revisions or [], notes=notes
    )
    if feedback_issue:
        return feedback_issue
    policy_issue = _review_revision_policy_issue(
        ctx, verdict=verdict, revisions=revisions or [], notes=notes
    )
    if policy_issue:
        return policy_issue
    plan_review_issue = _plan_review_validation_issue(
        ctx, verdict=verdict, revisions=revisions or [], notes=notes
    )
    if plan_review_issue:
        return plan_review_issue
    plan_review_ok_issue = _plan_review_ok_artifact_issue(ctx, verdict=verdict)
    if plan_review_ok_issue:
        return plan_review_ok_issue
    plan_review_policy_issue = _plan_review_ok_policy_issue(ctx, verdict=verdict)
    if plan_review_policy_issue:
        return plan_review_policy_issue
    issue = _research_review_validation_issue(
        ctx, verdict=verdict, revisions=revisions or [], notes=notes
    )
    if issue:
        return issue
    signal_id = _write_control_signal(ctx, "submit_micro_review", {
        "verdict": verdict,
        "revisions": revisions or [],
        "notes": notes,
    })
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
        proposal_notes = str(payload.get("notes") or "")
        policy_notes = "\n".join(part for part in (proposal_notes, notes) if part)
        try:
            from umbrella.deep_agent_tools.phase_contract_tools import _phase_plan_policy_issues

            policy_issues = _phase_plan_policy_issues(
                plan_payload,
                ctx=ctx,
                notes=policy_notes,
            )
        except Exception:
            policy_issues = []
        if policy_issues:
            return (
                "ERROR: phase plan submission violates workspace policy: "
                + "; ".join(policy_issues)
                + ". Revise the proposal before submitting it."
            )
        rows = _tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or ""))
        plan_text = json.dumps(
            {
                "plan": plan_payload,
                "proposal_notes": proposal_notes,
                "submit_notes": notes,
            },
            ensure_ascii=False,
        )
        contradiction = _negative_claim_contradiction_issue(
            ctx,
            rows=rows,
            text=plan_text,
            label="phase plan submission",
        )
        if contradiction:
            return contradiction + (
                " Phase plans must not submit contradicted code blockers; "
                "revise the proposal with current file evidence first."
            )
    _record_submitted_phase_plan_artifact(
        ctx,
        payload=payload,
        plan_id=selected_plan_id,
        notes=notes,
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


def _submit_verification(ctx: ToolContext, *, status: str, details: str = "") -> str:
    if stop := _stop_requested_message(ctx, "submit_verification"):
        return stop
    if status not in ("pass", "fail"):
        return f"ERROR: status must be pass or fail, got '{status}'"
    if status == "pass":
        if _mentions_unresolved_pass_blocker(details):
            return (
                "ERROR: submit_verification(status='pass') cannot include "
                "unresolved blockers or limitations. Loop back to execute with "
                "the concrete failures, then verify again."
            )
        view = getattr(ctx, "loop_state_view", None)
        if isinstance(view, dict):
            if (
                not view.get("last_verify_run_id")
                or not view.get("last_verify_passed")
                or int(view.get("last_verify_failed_count") or 0) > 0
            ):
                return (
                    "ERROR: submit_verification(status='pass') requires a fresh "
                    "passing run_workspace_verify result in this phase. A skipped, "
                    "missing, stale, or failing verification cannot be promoted."
                )
    signal_id = _write_control_signal(ctx, "submit_verification", {
        "status": status,
        "details": details,
    })
    return f"OK: Verification submitted: {status} (signal: {signal_id})"


def _submit_reflection(
    ctx: ToolContext,
    *,
    text: str,
    applies_to_phase: str,
    applies_to_subtask: str = "",
    evidence_refs: list[str] | None = None,
) -> str:
    if stop := _stop_requested_message(ctx, "submit_reflection"):
        return stop
    if not evidence_refs:
        return "ERROR: evidence_refs must be non-empty — cite at least one event_id or artifact_id"
    signal_id = _write_control_signal(ctx, "submit_reflection", {
        "text": text,
        "applies_to_phase": applies_to_phase,
        "applies_to_subtask": applies_to_subtask,
        "evidence_refs": evidence_refs,
    })
    return f"OK: Reflection submitted for phase '{applies_to_phase}' with {len(evidence_refs)} citations (signal: {signal_id})"


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
    review = _phase_subtask_retry_watcher_review_payload(ctx, reason=reason)
    signal_id = _write_control_signal(ctx, "request_watcher_review", review)
    review = dict(review)
    review["signal_id"] = signal_id
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
        contract_migration = review.get("contract_migration")
        has_contract_migration = isinstance(contract_migration, dict)
        operator_reason = str(review.get("operator_reason") or "")
        if has_contract_migration:
            operator_reason = (
                "Raw watcher operator reason is recorded in "
                "phase_control_signals.jsonl; memory mirror uses the structured "
                "contract_migration verdict to avoid replaying rejected test-edit "
                "recipes as hot guidance."
            )
        memory_doc = {
            "artifact": "retry_watcher_review",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "phase_id": phase_id,
            "subtask_id": subtask_id,
            "success_test": str(review.get("success_test") or ""),
            "failed_attempts": int(review.get("failed_attempts") or 0),
            "prior_watcher_reviews": int(review.get("prior_watcher_reviews") or 0),
            "operator_reason": operator_reason,
            "latest_failure": review.get("latest_failure") or {},
            "recommendation": str(review.get("recommendation") or ""),
            "signal_id": str(review.get("signal_id") or ""),
        }
        if has_contract_migration:
            memory_doc["contract_migration"] = contract_migration
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
    if _is_phase_run_context(ctx):
        return _mark_phase_subtask_complete(
            ctx,
            subtask_id=subtask_id,
            notes=notes,
            status=status,
            summary=summary,
            evidence=evidence_items,
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
) -> str:
    """Apply Umbrella phase-plan completion gates and write phase signals."""

    plan = _read_phase_plan(ctx)
    if plan is None:
        return "ERROR: no phase_plan.json found"
    evidence_items = _completion_evidence_items(evidence)
    current_phase = _current_phase_node(ctx, plan)
    if retry_block := _phase_subtask_retry_escalation_block(
        ctx, tool_name="mark_subtask_complete"
    ):
        return (
            "ERROR: mark_subtask_complete blocked: "
            + json.dumps(retry_block, ensure_ascii=False)
        )
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
        completion_issue = _phase_subtask_completion_issue(
            ctx,
            current_phase=current_phase,
            subtask_id=subtask_id,
        )
        if completion_issue:
            return completion_issue
    memory_quality_issue = _completion_memory_quality_issue(
        subtask_id=subtask_id,
        status=status,
        summary=summary,
        notes=notes,
        evidence=evidence_items,
    )
    if memory_quality_issue:
        return memory_quality_issue
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
                    extra={"completed_at": time.time()},
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


def _success_test_text_for_memory(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or value.get("command") or "").strip()
    return str(value or "").strip()


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
            "success_test": _success_test_text_for_memory(subtask.get("success_test")),
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
    '_submit_preflight_report',
    '_mentions_unresolved_pass_blocker',
    '_preflight_blockers_are_implementation_issues',
    '_edit_subtask_card',
    '_request_watcher_review',
    '_mirror_watcher_review_to_palace',
    '_mirror_phase_plan_mutation_to_palace',
    '_harness_run',
    '_mark_subtask_complete',
    '_mirror_phase_subtask_completion_to_palace',
]
