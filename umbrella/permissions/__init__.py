from umbrella.permissions.envelope import PermissionEnvelope, EnvelopeResult
from umbrella.permissions.loader import build_envelope, load_global_rules

__all__ = [
    "PermissionEnvelope",
    "EnvelopeResult",
    "build_envelope",
    "load_global_rules",
]


def check_tool(
    tool_name: str,
    phase_rules: list,
    *,
    paths: list[str] | None = None,
    cmd: str | None = None,
    scope_arg: str | None = None,
    include_global: bool = True,
) -> EnvelopeResult:
    env = build_envelope(phase_rules, include_global=include_global)
    return env.check(tool_name, paths=paths, cmd=cmd, scope_arg=scope_arg)
