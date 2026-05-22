import json
import pathlib
import time
import uuid
from typing import Any

from umbrella.phases.base import (
    PhasePlan,
    PhaseNode,
    PlanEdit,
    SubtaskCard,
    _json_ready,
)
from umbrella.contracts import ProofSpec

_DEFAULT_PHASES = [
    "preflight",
    "research",
    "research_review",
    "plan",
    "plan_review",
    "execute",
    "final_review",
    "verify",
]


def build_default_plan(
    workspace_id: str,
    run_id: str | None = None,
    phases: list[str] | None = None,
) -> PhasePlan:
    run_id = run_id or str(uuid.uuid4())
    phase_list = phases or _DEFAULT_PHASES
    nodes = [
        PhaseNode(id=p, manifest_id=p, status="pending")
        for p in phase_list
    ]
    return PhasePlan(
        plan_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        run_id=run_id,
        nodes=nodes,
        version=0,
    )


def save_plan(plan: PhasePlan, drive_root: pathlib.Path) -> None:
    path = drive_root / "state" / "phase_plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    import dataclasses
    path.write_text(
        json.dumps(_json_ready(dataclasses.asdict(plan)), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_plan(drive_root: pathlib.Path) -> PhasePlan | None:
    import os
    path = pathlib.Path(os.environ.get(
        "OUROBOROS_PHASE_PLAN_PATH",
        str(drive_root / "state" / "phase_plan.json"),
    ))
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return _plan_from_dict(data)


def _plan_from_dict(data: dict[str, Any]) -> PhasePlan:
    nodes = [
        PhaseNode(
            id=n["id"],
            manifest_id=n.get("manifest_id", n["id"]),
            status=n.get("status", "pending"),
            subtasks=_subtasks_from_list(n.get("subtasks")),
            overlay=n.get("overlay"),
            started_at=n.get("started_at"),
            ended_at=n.get("ended_at"),
            parent_phase_id=n.get("parent_phase_id"),
        )
        for n in data.get("nodes", [])
    ]
    edits = [
        PlanEdit(timestamp=e["timestamp"], actor=e["actor"], patch=e["patch"])
        for e in data.get("edits_log", [])
    ]
    return PhasePlan(
        plan_id=data["plan_id"],
        workspace_id=data["workspace_id"],
        run_id=data["run_id"],
        nodes=nodes,
        version=data.get("version", 0),
        edits_log=edits,
    )


def _string_list_from_any(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, dict):
        for key in ("path", "file_path", "file", "target", "value", "name", "id"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return [value.strip()]
        return []
    if isinstance(raw, (list, tuple, set, frozenset)):
        values: list[str] = []
        for item in raw:
            values.extend(_string_list_from_any(item))
        return values
    text = str(raw).strip()
    return [text] if text else []


def _first_string_list(raw: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        values = _string_list_from_any(raw.get(key))
        if values:
            return values
    return []


def _subtask_from_dict(raw: dict[str, Any], idx: int) -> SubtaskCard:
    title = str(raw.get("title") or raw.get("name") or f"Subtask {idx + 1}").strip()
    subtask_id = str(raw.get("id") or raw.get("subtask_id") or f"subtask_{idx + 1:02d}").strip()
    proof = (
        ProofSpec.from_mapping(raw["proof"])
        if isinstance(raw.get("proof"), dict)
        else None
    )
    return SubtaskCard(
        id=subtask_id,
        title=title,
        goal=str(raw.get("goal") or raw.get("description") or title),
        allowed_tools=frozenset(str(t) for t in (raw.get("allowed_tools") or []) if str(t).strip()),
        allowed_skills=frozenset(str(s) for s in (raw.get("allowed_skills") or []) if str(s).strip()),
        proof=proof,
        codeptr_refs=[str(x) for x in (raw.get("codeptr_refs") or [])],
        mcp_refs=[str(x) for x in (raw.get("mcp_refs") or [])],
        files_to_create=_first_string_list(
            raw,
            "files_to_create",
            "file_to_create",
            "new_files",
            "new_file",
            "files_to_add",
        ),
        files_to_change=_first_string_list(
            raw,
            "files_to_change",
            "file_to_change",
            "files_to_modify",
            "files_to_update",
            "target_files",
            "target_file",
        ),
        files_affected=_first_string_list(raw, "files_affected", "files", "paths"),
        dependencies=_first_string_list(raw, "dependencies", "depends_on", "requires"),
        status=raw.get("status") if raw.get("status") in {"pending", "running", "done", "failed"} else "pending",
        review_verdict=raw.get("review_verdict")
        if raw.get("review_verdict") in {"ok", "revise", "abort"}
        else None,
        completion=raw.get("completion") if isinstance(raw.get("completion"), dict) else None,
    )


def _subtasks_from_list(raw: Any) -> list[SubtaskCard] | None:
    if not isinstance(raw, list):
        return None
    return [
        _subtask_from_dict(item, idx)
        for idx, item in enumerate(raw)
        if isinstance(item, dict)
    ]
