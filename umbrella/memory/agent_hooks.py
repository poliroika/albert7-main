"""Umbrella-owned memory hooks used by the Ouroboros agent loop."""

from typing import Protocol, runtime_checkable

from ouroboros.memory_hooks import *  # noqa: F401,F403


@runtime_checkable
class MemoryHooks(Protocol):
    """Protocol for injecting Umbrella memory behavior into an agent loop."""

    def init_loop_memory(self, *args, **kwargs): ...

    def maybe_inject_periodic_recall(self, *args, **kwargs): ...

    def observe_tool_calls(self, *args, **kwargs): ...

    def mirror_subtask_to_memory(self, *args, **kwargs): ...


class UmbrellaMemoryHooks:
    """Default implementation backed by this module's functions."""

    def init_loop_memory(self, *args, **kwargs):
        return init_loop_memory(*args, **kwargs)

    def maybe_inject_periodic_recall(self, *args, **kwargs):
        return maybe_inject_periodic_recall(*args, **kwargs)

    def observe_tool_calls(self, *args, **kwargs):
        return observe_tool_calls(*args, **kwargs)

    def mirror_subtask_to_memory(self, *args, **kwargs):
        return mirror_subtask_to_memory(*args, **kwargs)


DEFAULT_MEMORY_HOOKS = UmbrellaMemoryHooks()
