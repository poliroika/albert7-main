"""Ouroboros adapter for Umbrella-owned workspace tools."""

from umbrella.deep_agent_tools import workspace_tools as _impl

globals().update(
    {
        name: value
        for name, value in vars(_impl).items()
        if not (name.startswith("__") and name.endswith("__"))
    }
)


def get_tools():
    from umbrella.deep_agent_tools.ouroboros_entries import get_ouroboros_tool_entries

    return get_ouroboros_tool_entries()


__all__ = [
    name
    for name in globals()
    if not (name.startswith("__") and name.endswith("__"))
]
