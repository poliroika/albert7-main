"""Ouroboros adapter for Umbrella-owned phase contract tools."""

from umbrella.deep_agent_tools import phase_contract_tools as _impl

globals().update(
    {
        name: value
        for name, value in vars(_impl).items()
        if not (name.startswith("__") and name.endswith("__"))
    }
)

__all__ = [
    name
    for name in globals()
    if not (name.startswith("__") and name.endswith("__"))
]
