import pathlib
from typing import Any, Iterator

from umbrella.utils.result_envelope import ResultEnvelope


def run_self_improvement(
    task_input: str,
    *,
    repo_root: pathlib.Path,
    workspace_id: str,
    run_id: str | None = None,
    launcher: Any = None,
) -> Iterator[ResultEnvelope]:
    """
    Separate runner for system self-improvement (edits to umbrella/ouroboros/gmas).
    Uses relaxed PermissionEnvelope from umbrella/permissions/self_improvement.yaml.
    NOT called from ordinary workspace runs.
    """
    import os
    from umbrella.permissions.loader import build_envelope
    import yaml
    envelope_path = pathlib.Path(
        os.environ.get(
            "UMBRELLA_SELF_IMPROVEMENT_ENVELOPE",
            str(repo_root / "umbrella" / "permissions" / "self_improvement.yaml"),
        )
    )
    if not envelope_path.exists():
        yield ResultEnvelope.failure(
            "SELF_IMPROVEMENT_ENVELOPE_MISSING",
            f"Relaxed envelope not found: {envelope_path}",
        )
        return
    data = yaml.safe_load(envelope_path.read_text()) or {}
    phase_rules = data.get("rules", [])
    envelope = build_envelope(phase_rules, include_global=False)
    yield ResultEnvelope.success(
        data={"ready": True, "envelope_rules": len(phase_rules)},
        run_id=run_id or "self_improvement",
    )
