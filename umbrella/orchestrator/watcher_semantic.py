"""Semantic failure streaks, env thresholds, and inject_lesson text for Watcher."""

import json
import os
import pathlib
from typing import Any

from umbrella.deep_agent_tools.research_provenance import next_finding_source_hint


def _json_obj_from_preview(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _semantic_error_code(row: dict[str, Any]) -> str:
    tool = str(row.get("tool") or "")
    raw = row.get("result_preview") or row.get("result") or row.get("output") or ""
    payload = _json_obj_from_preview(raw)
    raw_text = str(raw or "").strip()
    blob = json.dumps({"row": row, "payload": payload}, ensure_ascii=False).lower()
    if tool == "run_subtask_proof" and payload.get("passed") is False:
        shell_result = payload.get("shell_result")
        shell_blob = (
            json.dumps(shell_result, ensure_ascii=False).lower()
            if isinstance(shell_result, dict)
            else ""
        )
        if "attributeerror" in shell_blob:
            return "proof_runtime_attribute_error"
        if "modulenotfounderror" in shell_blob or "importerror" in shell_blob:
            return "proof_runtime_import_error"
        if "typeerror" in shell_blob:
            return "proof_runtime_type_error"
        return "proof_not_passing"
    if "proof_stale_rerun_required" in blob:
        return "proof_stale_rerun_required"
    if "patch_protocol_loop" in blob:
        return "patch_protocol_loop"
    if payload.get("reason") == "scope_change_required" or "scope_change_required" in blob:
        return "scope_conflict_loop"
    if "workspace_hash_mismatch" in blob or "diff_hash_mismatch" in blob:
        return "completion_hash_mismatch"
    if "completion_contract" in blob and (
        "required" in blob or "missing" in blob or "invalid" in blob
    ):
        return "completion_contract_invalid"
    if "fake_evidence_ref" in blob:
        return "fake_evidence_ref"
    if "subtask materialization missing" in blob or "subtask_materialization_missing" in blob:
        return "materialization_missing"
    if "verification_report.passed must be true" in blob:
        return "completion_with_failed_proof"
    if "gmas_context_before_first_write" in blob:
        return "context_gate_gmas_not_followed"
    if "subtask_context_read_required" in blob:
        return "context_gate_read_not_followed"
    if "patch_hunk_mismatch" in blob:
        recent = payload.get("recent_mismatches")
        if isinstance(recent, int) and recent >= 3:
            return "patch_protocol_loop"
        return "patch_hunk_mismatch"
    if raw_text.startswith("ERROR:"):
        if tool in {"palace_add", "submit_research_summary"} and (
            "research_finding" in blob
            or "source_id" in blob
            or "evidence metadata" in blob
            or "finding" in blob
        ):
            return "research_memory_provenance_error"
        return "tool_result_error"
    return ""


def _tool_row_is_successful_progress(row: dict[str, Any]) -> bool:
    if row.get("error"):
        return False
    if row.get("exit_code", 0) not in (0, None):
        return False
    raw = str(row.get("result_preview") or row.get("result") or row.get("output") or "")
    if raw.strip().lower().startswith("error:"):
        return False
    return bool(str(row.get("tool") or "").strip())


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def semantic_thresholds() -> tuple[int, int, int]:
    """Return (inject_m, restart_m, abort_m) from env with ordering enforced."""
    legacy_abort = os.environ.get("OUROBOROS_WATCHER_REPEAT_M", "").strip()
    abort_default = 30
    if legacy_abort and not os.environ.get("OUROBOROS_WATCHER_SEMANTIC_ABORT_M", "").strip():
        try:
            abort_default = max(1, int(legacy_abort))
        except ValueError:
            pass
    inject_m = _env_int("OUROBOROS_WATCHER_SEMANTIC_INJECT_M", 3)
    restart_m = _env_int("OUROBOROS_WATCHER_SEMANTIC_RESTART_M", 15)
    abort_m = _env_int("OUROBOROS_WATCHER_SEMANTIC_ABORT_M", abort_default)
    restart_m = max(inject_m, restart_m)
    abort_m = max(restart_m, abort_m)
    return inject_m, restart_m, abort_m


def semantic_signal_kind_for_streak(category: str, streak: int) -> str:
    inject_m, restart_m, abort_m = semantic_thresholds()
    if streak < inject_m:
        return ""
    if streak >= abort_m:
        return "abort_phase"
    if streak >= restart_m:
        return "restart_phase"
    return "inject_lesson"


def semantic_escalation_key(*, phase: str, category: str, streak: int) -> str:
    inject_m, restart_m, abort_m = semantic_thresholds()
    if streak >= abort_m:
        level = "abort"
    elif streak >= restart_m:
        level = "restart"
    elif streak >= inject_m:
        level = "inject"
    else:
        level = "none"
    return f"{phase}:{category}:{level}:{streak // inject_m}"


def current_semantic_error_streak(
    tools_path: pathlib.Path,
    *,
    task_id: str = "",
) -> tuple[int, str, str]:
    """Return (streak_length, signature, category) for the active tail streak."""
    task_id = str(task_id or "").strip()
    try:
        lines = tools_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, "", ""
    streak = 0
    signature = ""
    category = ""
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if task_id and str(row.get("task_id") or "") != task_id:
            continue
        code = _semantic_error_code(row)
        if not code:
            if _tool_row_is_successful_progress(row):
                break
            continue
        tool = str(row.get("tool") or "")
        payload = _json_obj_from_preview(
            row.get("result_preview") or row.get("result") or {}
        )
        subtask_id = ""
        args = row.get("args")
        if isinstance(args, dict):
            subtask_id = str(args.get("subtask_id") or "").strip()
        if not subtask_id and isinstance(payload, dict):
            subtask_id = str(payload.get("subtask_id") or "").strip()
        sig = f"{code}:{tool}:{subtask_id}"
        if streak == 0:
            signature = sig
            category = code
            streak = 1
        elif sig == signature:
            streak += 1
        else:
            break
    return streak, signature, category


def rounds_without_progress(
    tools_path: pathlib.Path,
    *,
    task_id: str = "",
    window: int = 20,
) -> int:
    task_id = str(task_id or "").strip()
    try:
        lines = tools_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    rounds = 0
    for line in reversed(lines):
        if rounds >= window:
            break
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if task_id and str(row.get("task_id") or "") != task_id:
            continue
        rounds += 1
        if _tool_row_is_successful_progress(row):
            tool = str(row.get("tool") or "")
            if tool in {
                "apply_workspace_patch",
                "replace_workspace_file",
                "run_subtask_proof",
                "run_workspace_verify",
                "mark_subtask_complete",
            }:
                return 0
    return rounds


def _truncate(text: str, limit: int | None = None) -> str:
    if limit is None:
        from ouroboros.limits import WATCHER_TOOL_ARGS_SNIPPET_CHARS

        limit = WATCHER_TOOL_ARGS_SNIPPET_CHARS
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def summarize_recent_tool_rows(
    tools_path: pathlib.Path,
    *,
    n: int = 12,
) -> list[dict[str, str]]:
    try:
        lines = tools_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, str]] = []
    for line in reversed(lines):
        if len(rows) >= n:
            break
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        tool = str(row.get("tool") or "").strip()
        if not tool:
            continue
        args = row.get("args") if isinstance(row.get("args"), dict) else {}
        args_summary = _truncate(
            json.dumps(
                {
                    k: args[k]
                    for k in ("kind", "source_id", "title", "query", "slug", "subtask_id")
                    if k in args and args[k]
                },
                ensure_ascii=False,
            )
            or str(args)[:120]
        )
        raw = str(row.get("result_preview") or row.get("result") or "")
        from ouroboros.limits import WATCHER_TOOL_SNIPPET_CHARS

        cap = WATCHER_TOOL_SNIPPET_CHARS
        if raw.strip().lower().startswith("error:"):
            snippet = _truncate(raw, cap)
        elif '"saved": true' in raw:
            snippet = "saved"
        else:
            snippet = _truncate(raw, max(160, cap // 2))
        rows.append(
            {
                "tool": tool,
                "args_summary": args_summary,
                "result_snippet": snippet,
            }
        )
    return list(reversed(rows))


def accepted_research_finding_ids(tools_path: pathlib.Path) -> list[str]:
    ids: list[str] = []
    try:
        lines = tools_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ids
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("tool") or "") != "palace_add":
            continue
        raw = str(row.get("result_preview") or "")
        if '"saved": true' not in raw:
            continue
        args = row.get("args") if isinstance(row.get("args"), dict) else {}
        payload = _json_obj_from_preview(raw)
        kind = str(args.get("kind") or payload.get("kind") or "").strip().lower()
        tags = args.get("tags") or payload.get("tags") or []
        if isinstance(tags, str):
            tag_values = {part.strip().lower() for part in tags.replace(",", " ").split()}
        elif isinstance(tags, list):
            tag_values = {str(part).strip().lower() for part in tags}
        else:
            tag_values = set()
        if kind != "research_finding" and "research_finding" not in tag_values:
            continue
        finding_id = str(payload.get("id") or "").strip()
        if finding_id:
            ids.append(finding_id)
    return ids[-8:]


def load_tool_log_rows(tools_path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = tools_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def build_semantic_lesson(
    *,
    phase: str,
    category: str,
    streak: int,
    signature: str,
    tools_path: pathlib.Path,
    recent_tools: list[dict[str, str]] | None = None,
) -> str:
    rows = load_tool_log_rows(tools_path)
    inject_m, restart_m, abort_m = semantic_thresholds()
    lines = [
        f"Watcher detected {streak} consecutive semantic tool failure(s) "
        f"({category or 'unknown'}) during phase `{phase}`.",
        f"Signature: {signature or 'n/a'}.",
        f"Escalation thresholds: inject>={inject_m}, restart>={restart_m}, abort>={abort_m}.",
    ]
    if category == "research_memory_provenance_error":
        lines.extend(
            [
                "For palace_add research findings:",
                "- Use kind=observation for synthesis, progress notes, or ungrounded summaries.",
                "- For kind=research_finding, copy a concrete URL, repo handle, or snippet "
                "from the cited tool result into the finding body.",
                "- Use source_id exactly as logged (e.g. deep_search:<query> from args, "
                "github:owner/repo only after github_project_search, or get_gmas_context:<query> "
                "when GMAS retrieval is non-fallback).",
                "- After two identical ERROR responses, change strategy; do not repeat the "
                "same source_id and text.",
                "- If evidence is thin, submit_research_summary with coverage_status=source_scarce.",
            ]
        )
        hint = next_finding_source_hint(rows)
        if hint.strip():
            lines.append(hint.strip())
        accepted = accepted_research_finding_ids(tools_path)
        if accepted:
            lines.append(
                "Accepted research_finding ids in this task: "
                + ", ".join(f"`{item}`" for item in accepted)
            )
    elif category.startswith("proof_") or category == "proof_not_passing":
        lines.append(
            "Read the latest run_subtask_proof shell output, fix the root cause, "
            "and re-run proof before attempting completion again."
        )
    elif category in {
        "completion_contract_invalid",
        "materialization_missing",
        "completion_with_failed_proof",
        "completion_hash_mismatch",
    }:
        lines.append(
            "Read the contract rejection text, satisfy every required field/evidence ref, "
            "then retry the completion tool once the underlying proof/verify passes."
        )
    else:
        lines.append(
            "Read the latest tool ERROR lines in this phase and change approach; "
            "do not repeat the same failing call with identical arguments."
        )

    recent = recent_tools if recent_tools is not None else summarize_recent_tool_rows(tools_path)
    if recent:
        lines.append("Recent tool activity:")
        for item in recent[-8:]:
            lines.append(
                f"- {item.get('tool')}: args={item.get('args_summary')} "
                f"-> {item.get('result_snippet')}"
            )
    return "\n".join(lines)


def watcher_use_llm_on_semantic() -> bool:
    return str(os.environ.get("OUROBOROS_WATCHER_USE_LLM_ON_SEMANTIC", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
