"""Umbrella-owned workspace tools exposed through deep-agent adapters."""

from umbrella.deep_agent_tools import context as _context
from umbrella.deep_agent_tools import memory as _memory
from umbrella.deep_agent_tools import skills as _skills
from umbrella.deep_agent_tools import workspace_commands as _commands
from umbrella.deep_agent_tools import workspace_gmas as _gmas
from umbrella.deep_agent_tools import workspace_ops as _ops
from umbrella.deep_agent_tools import workspace_read as _read
from umbrella.deep_agent_tools import workspace_services as _services

_MODULES = (
    _context,
    _memory,
    _skills,
    _gmas,
    _read,
    _ops,
    _commands,
    _services,
)

for _module in _MODULES:
    for _name in getattr(_module, "__all__", (name for name in vars(_module) if not (name.startswith("__") and name.endswith("__")))):
        if not (_name.startswith("__") and _name.endswith("__")):
            globals()[_name] = getattr(_module, _name)

_RUN_WORKSPACE_DEFAULT_TIMEOUT_S = _commands._RUN_WORKSPACE_DEFAULT_TIMEOUT_S
_RUN_WORKSPACE_MAX_TIMEOUT_S = _commands._RUN_WORKSPACE_MAX_TIMEOUT_S


def get_tools():
    from umbrella.deep_agent_tools.ouroboros_entries import get_ouroboros_tool_entries

    return get_ouroboros_tool_entries()


__all__ = [
    name
    for name in globals()
    if not (name.startswith("__") and name.endswith("__"))
]
