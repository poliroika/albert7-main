"""Canonical Umbrella phase id resolution for tools, runner, and completion gates."""

from typing import Any

KNOWN_PHASE_IDS = frozenset(
    {
        "preflight",
        "research",
        "research_review",
        "plan",
        "plan_review",
        "execute",
        "subtask_review",
        "final_review",
        "verify",
    }
)

_IGNORE_LOOP_PHASE_LABELS = frozenset({"linear", "phase"})


def phase_head(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    if text in KNOWN_PHASE_IDS:
        return text
    if ":" in text:
        head = text.split(":", 1)[0].strip()
        if head in KNOWN_PHASE_IDS:
            return head
    return ""


def phase_id_from_task_id(task_id: str) -> str:
    value = str(task_id or "").strip()
    if ":" not in value:
        return ""
    for part in value.split(":")[1:]:
        candidate = part.strip()
        if candidate in KNOWN_PHASE_IDS:
            return candidate
    return ""


def _phase_from_ctx_attr(ctx: Any, attr: str) -> str:
    return phase_head(str(getattr(ctx, attr, "") or ""))


def _phase_from_overlays(ctx: Any) -> str:
    overlays = getattr(ctx, "context_overlays", None)
    if not isinstance(overlays, dict):
        return ""
    phase_node = overlays.get("phase_node")
    if not isinstance(phase_node, dict):
        return ""
    for key in ("manifest_id", "id", "phase"):
        phase = phase_head(str(phase_node.get(key) or ""))
        if phase:
            return phase
    return ""


def _phase_from_loop_view(ctx: Any) -> str:
    view = getattr(ctx, "loop_state_view", None)
    if not isinstance(view, dict):
        return ""
    for key in ("phase_id", "phase", "phase_label"):
        phase = phase_head(str(view.get(key) or ""))
        if phase:
            return phase
    label = str(view.get("phase_label") or "").strip()
    if label.lower() in _IGNORE_LOOP_PHASE_LABELS:
        return ""
    return phase_head(label) or label


def resolve_phase_id(ctx: Any) -> str:
    phase = phase_id_from_task_id(str(getattr(ctx, "task_id", "") or ""))
    if phase:
        return phase
    for attr in ("umbrella_phase_id", "phase_id", "current_phase_id"):
        phase = _phase_from_ctx_attr(ctx, attr)
        if phase:
            return phase
    phase = _phase_from_overlays(ctx)
    if phase:
        return phase
    return _phase_from_loop_view(ctx)


def phase_control_row_matches(row: dict[str, Any], *, task_id: str) -> bool:
    row_task_id = str(row.get("task_id") or "")
    if not task_id:
        return True
    if row_task_id == task_id:
        return True
    expected_phase = phase_id_from_task_id(task_id)
    row_phase = str(row.get("phase") or "").strip()
    if expected_phase and row_phase and row_phase != expected_phase:
        return False
    if ":" not in task_id:
        return not row_task_id
    run_id = task_id.split(":", 1)[0]
    if row_task_id and not row_task_id.startswith(f"{run_id}:"):
        return False
    return bool(expected_phase or row_phase)
