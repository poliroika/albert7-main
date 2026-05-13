"""Terminal-Bench adapter for the umbrella / ouroboros stack.

The public entrypoint is :class:`terminal_bench_integration.agent.UmbrellaAgent`,
a subclass of `terminal_bench.agents.installed_agents.AbstractInstalledAgent`
that bundles the entire `umbrella` repository into the Terminal-Bench task
container, installs it, and then runs `umbrella.app_ouroboros` against the
`workspaces/terminal_bench` adapter workspace with the per-task instruction
written into `TASK_MAIN.md`.

For the host-side launcher see :mod:`terminal_bench_integration.cli`.
"""



__all__ = ["UmbrellaAgent"]


def __getattr__(name: str):
    if name == "UmbrellaAgent":
        from terminal_bench_integration.agent import UmbrellaAgent

        return UmbrellaAgent
    raise AttributeError(name)
