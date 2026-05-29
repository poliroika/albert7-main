"""Runtime-owned execute WorkItems and repair-subtask materialization."""


import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Mapping

from umbrella.contracts.hashing import hash_value
from umbrella.contracts.models import json_ready

WorkItemKind = Literal[
    "implementation_repair",
    "proof_contract_repair",
    "oracle_repair",
    "packaging_import_repair",
    "env_capability_repair",
    "runtime_smoke_repair",
    "test_tampering_review",
]

WORK_ITEM_QUEUE_FILENAME = "work_items.json"
ACTIVE_WORK_ITEM_FILENAME = "active_work_item.json"

WRITE_TOOLS = frozenset(
    {
        "apply_workspace_patch",
        "replace_workspace_file",
        "delete_workspace_file",
        "provision_workspace_environment",
    }
)
COMPLETION_TOOLS = frozenset({"mark_subtask_complete", "run_subtask_proof"})
PLAN_REPAIR_TOOLS = frozenset({"apply_plan_revision_patch"})
BLOCKED_CONTROL_TOOLS = frozenset(
    {
        "apply_workspace_patch",
        "replace_workspace_file",
        "delete_workspace_file",
        "provision_workspace_environment",
        "run_subtask_proof",
        "mark_subtask_complete",
        "request_watcher_review",
        "apply_plan_revision_patch",
    }
)
PACKAGING_IMPORT_FILES = (
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "workspace.toml",
)


@dataclass(frozen=True)
class WorkItem:
    id: str
    kind: WorkItemKind
    source_phase: str
    target_phase: str
    active_subtask_id: str = ""
    required_changes: tuple[dict[str, Any], ...] = ()
    allowed_files: tuple[str, ...] = ()
    forbidden_files: tuple[str, ...] = ()
    proof_contract: dict[str, Any] = field(default_factory=dict)
    execution_environment: dict[str, Any] = field(default_factory=dict)
    memory_scope: dict[str, Any] = field(default_factory=dict)
    tool_envelope: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[dict[str, Any], ...] = ()
    created_from_decision_id: str = ""
    attempt_id: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkItem":
        return cls(
            id=str(value.get("id") or ""),
            kind=_work_item_kind(value.get("kind")),
            source_phase=str(value.get("source_phase") or ""),
            target_phase=str(value.get("target_phase") or ""),
            active_subtask_id=str(value.get("active_subtask_id") or ""),
            required_changes=tuple(
                dict(item)
                for item in value.get("required_changes") or ()
                if isinstance(item, dict)
            ),
            allowed_files=tuple(_unique_paths(value.get("allowed_files"))),
            forbidden_files=tuple(_unique_paths(value.get("forbidden_files"))),
            proof_contract=dict(value.get("proof_contract") or {})
            if isinstance(value.get("proof_contract"), dict)
            else {},
            execution_environment=dict(value.get("execution_environment") or {})
            if isinstance(value.get("execution_environment"), dict)
            else {},
            memory_scope=dict(value.get("memory_scope") or {})
            if isinstance(value.get("memory_scope"), dict)
            else {},
            tool_envelope=dict(value.get("tool_envelope") or {})
            if isinstance(value.get("tool_envelope"), dict)
            else {},
            evidence_refs=tuple(
                dict(item)
                for item in value.get("evidence_refs") or ()
                if isinstance(item, dict)
            ),
            created_from_decision_id=str(value.get("created_from_decision_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source_phase": self.source_phase,
            "target_phase": self.target_phase,
            "active_subtask_id": self.active_subtask_id,
            "required_changes": [json_ready(item) for item in self.required_changes],
            "allowed_files": list(self.allowed_files),
            "forbidden_files": list(self.forbidden_files),
            "proof_contract": json_ready(self.proof_contract),
            "execution_environment": json_ready(self.execution_environment),
            "memory_scope": json_ready(self.memory_scope),
            "tool_envelope": json_ready(self.tool_envelope),
            "evidence_refs": [json_ready(item) for item in self.evidence_refs],
            "created_from_decision_id": self.created_from_decision_id,
            "attempt_id": self.attempt_id,
        }


def _work_item_kind(value: Any) -> WorkItemKind:
    text = str(value or "").strip()
    allowed = {
        "implementation_repair",
        "proof_contract_repair",
        "oracle_repair",
        "packaging_import_repair",
        "env_capability_repair",
        "runtime_smoke_repair",
        "test_tampering_review",
    }
    return text if text in allowed else "implementation_repair"  # type: ignore[return-value]


def _state_path(drive_root: Path, filename: str) -> Path:
    path = Path(drive_root) / "state" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_work_item_queue(drive_root: Path | None) -> list[WorkItem]:
    if drive_root is None:
        return []
    path = _state_path(Path(drive_root), WORK_ITEM_QUEUE_FILENAME)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = payload.get("work_items") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    return [WorkItem.from_mapping(item) for item in raw if isinstance(item, dict)]


def save_work_item_queue(drive_root: Path, work_items: list[WorkItem]) -> Path:
    path = _state_path(drive_root, WORK_ITEM_QUEUE_FILENAME)
    path.write_text(
        json.dumps(
            {"work_items": [item.to_dict() for item in work_items]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_active_work_item(drive_root: Path | None) -> WorkItem | None:
    if drive_root is None:
        return None
    path = _state_path(Path(drive_root), ACTIVE_WORK_ITEM_FILENAME)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return WorkItem.from_mapping(payload) if isinstance(payload, dict) else None


def save_active_work_item(drive_root: Path, work_item: WorkItem) -> Path:
    path = _state_path(drive_root, ACTIVE_WORK_ITEM_FILENAME)
    path.write_text(
        json.dumps(work_item.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    queue = load_work_item_queue(drive_root)
    by_id = {item.id: item for item in queue}
    by_id[work_item.id] = work_item
    save_work_item_queue(drive_root, list(by_id.values()))
    return path


def clear_active_work_item(drive_root: Path) -> None:
    path = _state_path(drive_root, ACTIVE_WORK_ITEM_FILENAME)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def complete_active_work_item(drive_root: Path, work_item_id: str) -> None:
    queue = [item for item in load_work_item_queue(drive_root) if item.id != work_item_id]
    save_work_item_queue(drive_root, queue)
    current = load_active_work_item(drive_root)
    if current is not None and current.id == work_item_id:
        clear_active_work_item(drive_root)


def _norm_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").lstrip("/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _string_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        out: list[str] = []
        for key in (
            "path",
            "file_path",
            "file",
            "target",
            "target_file",
            "target_path",
            "value",
            "name",
        ):
            if isinstance(value.get(key), str):
                out.append(str(value.get(key) or ""))
        for key in (
            "files",
            "allowed_files",
            "files_to_change",
            "files_to_create",
            "files_affected",
            "changed_files",
        ):
            out.extend(_string_items(value.get(key)))
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        out: list[str] = []
        for item in value:
            out.extend(_string_items(item))
        return out
    return [str(value)]


def _unique_paths(value: Any) -> list[str]:
    return list(dict.fromkeys(path for path in (_norm_path(item) for item in _string_items(value)) if path))


def _subtask_allowed_files(subtask: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(subtask, Mapping):
        return []
    paths: list[str] = []
    for key in ("files_to_create", "files_to_change", "files_affected"):
        paths.extend(_unique_paths(subtask.get(key)))
    proof = subtask.get("proof")
    scope = proof.get("scope") if isinstance(proof, Mapping) else None
    if isinstance(scope, Mapping):
        paths.extend(_unique_paths(scope.get("changed_files_expected")))
        paths.extend(_unique_paths(scope.get("files_under_test")))
    return list(dict.fromkeys(paths))


def _target_subtask_id(value: Mapping[str, Any]) -> str:
    for key in (
        "active_subtask_id",
        "target_subtask_id",
        "subtask_id",
        "target_subtask",
        "target_id",
    ):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return ""


def _payload_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts.append(value)
        else:
            try:
                parts.append(json.dumps(json_ready(value), ensure_ascii=False))
            except Exception:
                parts.append(str(value or ""))
    return "\n".join(part for part in parts if part)


def classify_work_item_kind(*payloads: Any) -> WorkItemKind:
    text = _payload_text(*payloads).lower()
    if re.search(r"\b(no module named|bare import|package import|pythonpath|editable install|setup\.py|setup\.cfg|pyproject|workspace\.toml)\b", text):
        return "packaging_import_repair"
    if re.search(r"\b(tcl|tk|tkinter|display|desktop_gui_runtime|capability_probe_environment_mismatch|env_hash|environment)\b", text):
        return "env_capability_repair"
    if re.search(r"\b(oracle|contradict|generated test|bad_generated_oracle)\b", text):
        return "oracle_repair"
    if re.search(r"\b(proof_contract|proof target|harness profile|pytest target|weak proof|manual proof|headless proof)\b", text):
        return "proof_contract_repair"
    if re.search(r"\b(runtime smoke|process_alive|weak_runtime_proof)\b", text):
        return "runtime_smoke_repair"
    if re.search(r"\b(test tamper|test_tampering|skip|xfail|weaken test)\b", text):
        return "test_tampering_review"
    return "implementation_repair"


def _kind_from_control_payload(payload: Mapping[str, Any]) -> WorkItemKind:
    text = _payload_text(payload).lower()
    code = str(
        payload.get("reason_code")
        or payload.get("trigger_code")
        or payload.get("code")
        or ""
    ).strip()
    if code in {"package_import_env_mismatch", "setup_harness_mismatch"}:
        return "packaging_import_repair"
    if code == "proof_execution_env_mismatch":
        return "proof_contract_repair"
    return classify_work_item_kind(payload, text)


def _repair_allowed_files(kind: WorkItemKind, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if kind == "packaging_import_repair":
        return PACKAGING_IMPORT_FILES
    if kind == "proof_contract_repair":
        return ()
    return fallback


def _repair_forbidden_files(kind: WorkItemKind, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if kind == "packaging_import_repair":
        forbidden = set(fallback)
        forbidden.update(path for path in fallback if path.startswith(("src/", "tests/")))
        forbidden.update(("src/", "tests/"))
        return tuple(sorted(forbidden))
    return fallback


def reclassify_active_work_item(
    drive_root: Path,
    payload: Mapping[str, Any],
    *,
    decision_id: str = "",
) -> WorkItem | None:
    """Supersede the active WorkItem when proof evidence belongs to another lane."""

    current = load_active_work_item(drive_root)
    if current is None:
        return None
    kind = _kind_from_control_payload(payload)
    if kind == current.kind:
        return current
    text = _payload_text(payload)
    allowed_files = _repair_allowed_files(kind, current.allowed_files)
    forbidden_files = _repair_forbidden_files(kind, current.forbidden_files)
    required_change = {
        "id": str(payload.get("reason_code") or payload.get("code") or kind),
        "source": "WorkItemReclassifier",
        "reason_code": str(payload.get("reason_code") or payload.get("code") or ""),
        "message": text[:1200],
        "target_subtask_id": current.active_subtask_id,
        "evidence_refs": list(payload.get("evidence_refs") or []),
        "supersedes_work_item_id": current.id,
    }
    item_id = "work:" + hash_value(
        {
            "supersedes": current.id,
            "kind": kind,
            "payload": json_ready(payload),
            "decision_id": decision_id,
            "attempt_id": current.attempt_id,
        }
    )[:16]
    tool_envelope = _tool_envelope_for_kind(
        kind,
        allowed_files=list(allowed_files),
        forbidden_files=list(forbidden_files),
    )
    next_item = replace(
        current,
        id=item_id,
        kind=kind,
        required_changes=(required_change,),
        allowed_files=tuple(allowed_files),
        forbidden_files=tuple(forbidden_files),
        tool_envelope=tool_envelope,
        created_from_decision_id=decision_id or current.created_from_decision_id,
    )
    queue = [item for item in load_work_item_queue(drive_root) if item.id != current.id]
    queue.append(next_item)
    save_work_item_queue(drive_root, queue)
    save_active_work_item(drive_root, next_item)
    return next_item


def _module_name_from_text(text: str) -> str:
    for pattern in (
        r"No module named ['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]",
        r"\bimport\s+([A-Za-z_][A-Za-z0-9_.]*)\b",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1).split(".")[0]
    return ""


def _default_proof_contract(
    *,
    kind: WorkItemKind,
    text: str,
    fallback: Mapping[str, Any] | None,
    allowed_files: list[str],
) -> dict[str, Any]:
    if isinstance(fallback, Mapping) and fallback:
        return dict(fallback)
    if kind == "packaging_import_repair":
        module = _module_name_from_text(text) or "calculator"
        return {
            "execution": {
                "kind": "command",
                "command": ["python", "-c", f"import {module}"],
                "timeout_sec": 60,
            },
            "oracle": {"required_properties": ["module_imports"]},
            "scope": {"changed_files_expected": allowed_files},
            "harness_profile": "packaging_import",
        }
    if kind == "env_capability_repair":
        return {
            "execution": {
                "kind": "command",
                "command": [
                    "python",
                    "-c",
                    "import tkinter as tk; root=tk.Tk(); root.update(); root.destroy()",
                ],
                "timeout_sec": 60,
            },
            "oracle": {"required_properties": ["runtime_started"]},
            "scope": {"changed_files_expected": allowed_files},
            "required_capabilities": ["desktop_gui_runtime"],
            "harness_profile": "desktop_gui_runtime",
        }
    return {
        "execution": {
            "kind": "command",
            "command": ["python", "-m", "pytest"],
            "timeout_sec": 120,
        },
        "oracle": {"required_properties": ["tests_pass"]},
        "scope": {"changed_files_expected": allowed_files},
        "harness_profile": "repair_smoke",
    }


def _tool_envelope_for_kind(
    kind: WorkItemKind,
    *,
    allowed_files: list[str],
    forbidden_files: list[str],
    extra_allowed_tools: list[str] | None = None,
) -> dict[str, Any]:
    allowed = {
        "read_file",
        "list_files",
        "repo_read",
        "repo_list",
        "apply_workspace_patch",
        "replace_workspace_file",
        "delete_workspace_file",
        "run_subtask_proof",
        "mark_subtask_complete",
        "request_watcher_review",
    }
    if kind in {"proof_contract_repair"}:
        allowed.add("apply_plan_revision_patch")
    allowed.update(str(tool).strip() for tool in (extra_allowed_tools or []) if str(tool).strip())
    return {
        "allowed_tools": sorted(allowed),
        "denied_tools": ["wipe_workspace", "reset_palace"],
        "policy": {
            "allowed_files": allowed_files,
            "forbidden_files": forbidden_files,
            "state_owner": "umbrella_runtime",
        },
    }


def build_work_item_from_subtask(
    subtask: Mapping[str, Any],
    *,
    source_phase: str = "execute",
    target_phase: str = "execute",
    attempt_id: str = "",
    created_from_decision_id: str = "",
) -> WorkItem:
    subtask_id = str(subtask.get("id") or subtask.get("subtask_id") or "").strip()
    allowed_files = _subtask_allowed_files(subtask)
    proof = dict(subtask.get("proof") or {}) if isinstance(subtask.get("proof"), Mapping) else {}
    memory_scope = (
        dict(subtask.get("memory_scope") or {})
        if isinstance(subtask.get("memory_scope"), Mapping)
        else {}
    )
    extra_allowed_tools = _string_items(subtask.get("allowed_tools"))
    for asset in memory_scope.get("assets") or []:
        if isinstance(asset, Mapping) and str(asset.get("kind") or "") == "gmas_context":
            extra_allowed_tools.extend(["get_gmas_context", "search_gmas_knowledge"])
    item_id = f"execute:{subtask_id}" if subtask_id else f"execute:{hash_value(json_ready(subtask))[:12]}"
    tool_envelope = _tool_envelope_for_kind(
        "implementation_repair",
        allowed_files=allowed_files,
        forbidden_files=_unique_paths(subtask.get("forbidden_files")),
        extra_allowed_tools=extra_allowed_tools,
    )
    return WorkItem(
        id=item_id,
        kind="implementation_repair",
        source_phase=source_phase,
        target_phase=target_phase,
        active_subtask_id=subtask_id,
        allowed_files=tuple(allowed_files),
        forbidden_files=tuple(_unique_paths(subtask.get("forbidden_files"))),
        proof_contract=proof,
        memory_scope=memory_scope,
        tool_envelope=tool_envelope,
        created_from_decision_id=created_from_decision_id,
        attempt_id=attempt_id,
    )


def ensure_active_work_item_for_subtask(
    drive_root: Path,
    subtask: Mapping[str, Any],
    *,
    attempt_id: str = "",
) -> WorkItem:
    subtask_id = str(subtask.get("id") or subtask.get("subtask_id") or "").strip()
    current = load_active_work_item(drive_root)
    if current is not None and current.active_subtask_id == subtask_id:
        return current
    for item in load_work_item_queue(drive_root):
        if item.active_subtask_id == subtask_id:
            save_active_work_item(drive_root, item)
            return item
    item = build_work_item_from_subtask(subtask, attempt_id=attempt_id)
    save_active_work_item(drive_root, item)
    return item


def _evidence_refs_from_payload(*values: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        for key in ("evidence_refs", "proof_refs"):
            raw = value.get(key)
            if isinstance(raw, list):
                refs.extend(dict(item) for item in raw if isinstance(item, dict))
        ref = value.get("verification_report_ref")
        if isinstance(ref, Mapping):
            refs.append(
                {
                    "ref_type": "verification_report",
                    "ref_id": str(ref.get("report_id") or ""),
                    "hash": str(ref.get("ledger_hash") or ref.get("hash") or ""),
                    "produced_by": "verifier",
                }
            )
    return refs


def materialize_work_items_from_phase_exit(
    decision: Mapping[str, Any],
    *,
    execute_subtasks: list[Mapping[str, Any]] | None = None,
    attempt_id: str = "",
) -> list[WorkItem]:
    if str(decision.get("outcome") or "") != "loop_back":
        return []
    target_phase = str(decision.get("target_phase") or "execute").strip() or "execute"
    required_changes = [
        dict(item)
        for item in decision.get("required_changes") or ()
        if isinstance(item, dict)
    ]
    issues = [
        dict(item)
        for item in decision.get("issues") or ()
        if isinstance(item, dict)
    ]
    if not required_changes and issues:
        required_changes = [
            {
                "id": str(issue.get("code") or f"issue-{idx}"),
                "source": "ReviewIssue",
                "reason_code": str(issue.get("code") or ""),
                "message": str(issue.get("message") or ""),
                "target_subtask_id": _target_subtask_id(issue),
                "evidence_refs": issue.get("evidence_refs") or [],
            }
            for idx, issue in enumerate(issues)
        ]
    if not required_changes:
        text = _payload_text(decision.get("verification_summary"), decision)
        if text.strip():
            required_changes = [
                {
                    "id": "verification-loopback",
                    "source": "PhaseExitDecision",
                    "message": text[:1200],
                    "evidence_refs": decision.get("evidence_refs") or [],
                }
            ]
    if not required_changes:
        return []

    subtasks_by_id = {
        str(item.get("id") or item.get("subtask_id") or ""): item
        for item in (execute_subtasks or [])
        if isinstance(item, Mapping)
    }
    decision_id = str(decision.get("source_tool_call_id") or "") or hash_value(json_ready(decision))[:12]
    work_items: list[WorkItem] = []
    for idx, change in enumerate(required_changes):
        target_subtask_id = _target_subtask_id(change)
        active_subtask = subtasks_by_id.get(target_subtask_id) if target_subtask_id else None
        text = _payload_text(change, issues, decision.get("verification_summary"))
        kind = classify_work_item_kind(change, issues, decision.get("verification_summary"))
        allowed_files = _unique_paths(change.get("allowed_files"))
        if not allowed_files:
            allowed_files = _unique_paths(change)
        if not allowed_files:
            allowed_files = _subtask_allowed_files(active_subtask)
        if kind == "packaging_import_repair" and not allowed_files:
            allowed_files = [
                path
                for path in PACKAGING_IMPORT_FILES
                if path in text
            ] or list(PACKAGING_IMPORT_FILES)
        forbidden_files = _unique_paths(change.get("forbidden_files"))
        fallback_proof = (
            change.get("proof_contract")
            if isinstance(change.get("proof_contract"), Mapping)
            else (
                active_subtask.get("proof")
                if isinstance(active_subtask, Mapping)
                and isinstance(active_subtask.get("proof"), Mapping)
                else {}
            )
        )
        proof_contract = _default_proof_contract(
            kind=kind,
            text=text,
            fallback=fallback_proof if isinstance(fallback_proof, Mapping) else {},
            allowed_files=allowed_files,
        )
        active_subtask_id = target_subtask_id or f"repair-{kind.replace('_', '-')}-{idx + 1}"
        if active_subtask_id in subtasks_by_id:
            repair_subtask_id = f"repair-{kind.replace('_', '-')}-{active_subtask_id}"
        else:
            repair_subtask_id = active_subtask_id
        item_seed = {
            "decision_id": decision_id,
            "idx": idx,
            "kind": kind,
            "active_subtask_id": repair_subtask_id,
            "change": change,
        }
        item_id = f"work:{hash_value(item_seed)[:16]}"
        evidence_refs = _evidence_refs_from_payload(decision, change, *issues)
        tool_envelope = _tool_envelope_for_kind(
            kind,
            allowed_files=allowed_files,
            forbidden_files=forbidden_files,
        )
        work_items.append(
            WorkItem(
                id=item_id,
                kind=kind,
                source_phase=str(decision.get("phase_id") or ""),
                target_phase=target_phase,
                active_subtask_id=repair_subtask_id,
                required_changes=(change,),
                allowed_files=tuple(allowed_files),
                forbidden_files=tuple(forbidden_files),
                proof_contract=proof_contract,
                execution_environment=dict(change.get("execution_environment") or {})
                if isinstance(change.get("execution_environment"), Mapping)
                else {},
                memory_scope=dict(change.get("memory_scope") or {})
                if isinstance(change.get("memory_scope"), Mapping)
                else {},
                tool_envelope=tool_envelope,
                evidence_refs=tuple(evidence_refs),
                created_from_decision_id=decision_id,
                attempt_id=attempt_id or str(time.time()),
            )
        )
    return work_items


def work_item_to_repair_subtask(work_item: WorkItem) -> dict[str, Any]:
    title = work_item.kind.replace("_", " ").title()
    change_text = _payload_text(*work_item.required_changes)[:1000]
    create_files: list[str] = []
    for change in work_item.required_changes:
        action = str(
            change.get("change_type")
            or change.get("action")
            or change.get("op")
            or ""
        ).lower()
        if action in {"create", "add"}:
            create_files.extend(_unique_paths(change))
    create_files = list(dict.fromkeys(create_files))
    return {
        "id": work_item.active_subtask_id,
        "title": title,
        "goal": change_text or title,
        "allowed_tools": list(work_item.tool_envelope.get("allowed_tools") or []),
        "allowed_skills": [],
        "proof": work_item.proof_contract,
        "memory_scope": work_item.memory_scope,
        "files_to_create": create_files,
        "files_to_change": list(work_item.allowed_files),
        "files_affected": list(work_item.allowed_files),
        "status": "pending",
    }


def work_item_tool_filter(
    tool_filter: Mapping[str, Any],
    *,
    work_item: WorkItem | None,
    control_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "allow": list(tool_filter.get("allow") or []),
        "deny": list(tool_filter.get("deny") or []),
        "required": list(tool_filter.get("required") or []),
        "completion_prerequisites": dict(tool_filter.get("completion_prerequisites") or {}),
    }
    gated = WRITE_TOOLS | COMPLETION_TOOLS | PLAN_REPAIR_TOOLS
    if work_item is None:
        payload["allow"] = [tool for tool in payload["allow"] if tool not in gated]
        payload["deny"] = sorted(set(payload["deny"]) | gated)
        return payload
    if (
        isinstance(control_decision, Mapping)
        and str(control_decision.get("kind") or "") == "blocked_no_valid_next_action"
    ):
        allowed_next = set(_string_items(control_decision.get("allowed_next_tools")))
        if not allowed_next:
            allowed_next = {
                tool
                for tool in _string_items(control_decision.get("required_next_tools"))
                if tool
            }
        payload["allow"] = sorted(set(payload["allow"]) & allowed_next)
        payload["deny"] = sorted(set(payload["deny"]) | (BLOCKED_CONTROL_TOOLS - allowed_next))
        payload["required"] = [tool for tool in payload["required"] if tool in allowed_next]
        return payload
    envelope = work_item.tool_envelope or {}
    allowed = set(payload["allow"])
    scoped_allowed = set(envelope.get("allowed_tools") or [])
    if scoped_allowed:
        allowed &= scoped_allowed
        allowed |= COMPLETION_TOOLS & scoped_allowed
    if work_item.kind != "proof_contract_repair":
        allowed -= PLAN_REPAIR_TOOLS
    payload["allow"] = sorted(allowed)
    payload["deny"] = sorted(set(payload["deny"]) | set(envelope.get("denied_tools") or []))
    return payload
