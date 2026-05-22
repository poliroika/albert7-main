"""Small structured-plan helpers for phase-contract tools."""

from umbrella.deep_agent_tools.phase_contract_common import *


def _iter_plan_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            strings.extend(_iter_plan_strings(child))
    elif isinstance(value, list):
        for child in value:
            strings.extend(_iter_plan_strings(child))
    return strings


__all__ = ["_iter_plan_strings"]
