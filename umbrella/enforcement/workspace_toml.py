"""workspace.toml verification policy edits during agent execute phases."""

import tomllib
from pathlib import Path
from typing import Any

from umbrella.enforcement.kernel import normalise_workspace_path

_STRONG_VERIFICATION_KINDS = frozenset({"shell", "pytest", "smoke_run"})

def verification_steps_from_toml(text: str) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(text or "")
    except Exception:
        return []
    verification = data.get("verification") if isinstance(data, dict) else None
    if not isinstance(verification, dict):
        return []
    steps = verification.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _verification_step_name(step: dict[str, Any], index: int) -> str:
    value = (
        step.get("name")
        or step.get("id")
        or step.get("command")
        or step.get("path")
        or index
    )
    return str(value).strip()


def _verification_step_kind(step: dict[str, Any]) -> str:
    return str(step.get("kind") or step.get("type") or "").strip().lower()


def workspace_toml_verification_weakening_block(
    seed_path: Path,
    rel_path: str,
    new_content: str,
) -> dict[str, Any] | None:
    norm = normalise_workspace_path(rel_path)
    if norm != "workspace.toml":
        return None
    old_path = seed_path / "workspace.toml"
    if not old_path.is_file():
        return None
    try:
        old_content = old_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    old_steps = verification_steps_from_toml(old_content)
    new_steps = verification_steps_from_toml(new_content)
    if not old_steps:
        return None
    dropped_count = len(new_steps) < len(old_steps)
    old_by_name = {
        _verification_step_name(step, idx): _verification_step_kind(step)
        for idx, step in enumerate(old_steps)
    }
    new_by_name = {
        _verification_step_name(step, idx): _verification_step_kind(step)
        for idx, step in enumerate(new_steps)
    }
    missing_names = [name for name in old_by_name if name and name not in new_by_name]
    downgraded = [
        name
        for name, old_kind in old_by_name.items()
        if old_kind in _STRONG_VERIFICATION_KINDS
        and new_by_name.get(name) == "file_exists"
    ]
    replacement_strong_count = sum(
        1 for kind in new_by_name.values() if kind in _STRONG_VERIFICATION_KINDS
    )
    old_strong_count = sum(
        1 for kind in old_by_name.values() if kind in _STRONG_VERIFICATION_KINDS
    )
    dropped_strong = bool(missing_names) and replacement_strong_count < old_strong_count
    if dropped_count or downgraded or dropped_strong:
        return {
            "status": "blocked",
            "reason": "verification_self_weakening_blocked",
            "file_path": norm,
            "old_step_count": len(old_steps),
            "new_step_count": len(new_steps),
            "missing_steps": missing_names[:10],
            "downgraded_steps": downgraded[:10],
            "message": (
                "workspace.toml verification cannot be weakened during a run. "
                "Add stronger checks or fix existing checks instead of deleting/downgrading them."
            ),
            "next_step": (
                "Keep prior shell/pytest/smoke verification coverage and let "
                "umbrella.verification.spec_loader augment safety-critical local tests."
            ),
        }
    return None

