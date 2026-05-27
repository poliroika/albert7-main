"""Read research_depth from preflight control signals."""

import json
from pathlib import Path
from typing import Any

_VALID_DEPTHS = frozenset({"none", "light", "full"})


def _signal_created_at(row: dict[str, Any]) -> float:
    try:
        return float(row.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def read_preflight_research_depth(
    drive_root: Path | None,
    *,
    run_id: str = "",
) -> str:
    if drive_root is None:
        return ""
    path = Path(drive_root) / "state" / "phase_control_signals.jsonl"
    if not path.exists():
        return ""
    latest = ""
    latest_at = 0.0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("kind") or "") != "submit_preflight_report":
                continue
            row_run = str(row.get("run_id") or "").strip()
            if run_id and row_run and row_run != run_id:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            depth = str(payload.get("research_depth") or "").strip().lower()
            if depth not in _VALID_DEPTHS:
                continue
            created = _signal_created_at(row)
            if created >= latest_at:
                latest_at = created
                latest = depth
    except OSError:
        return ""
    return latest
