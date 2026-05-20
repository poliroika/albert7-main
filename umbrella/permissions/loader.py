import os
import pathlib
from typing import Any

import yaml

from umbrella.permissions.envelope import PermissionEnvelope

_DEFAULT_GLOBAL = pathlib.Path(__file__).parent / "global.yaml"


def load_global_rules() -> list[dict[str, Any]]:
    path = pathlib.Path(os.environ.get("UMBRELLA_PERMISSIONS_GLOBAL", str(_DEFAULT_GLOBAL)))
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("rules", [])


def build_envelope(
    phase_rules: list[dict[str, Any]],
    context_vars: dict[str, str] | None = None,
    include_global: bool = True,
) -> PermissionEnvelope:
    global_deny = load_global_rules() if include_global else []
    return PermissionEnvelope(
        phase_rules=phase_rules,
        global_deny_rules=global_deny,
        context_vars=context_vars,
    )
