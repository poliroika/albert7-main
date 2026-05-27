"""Memory Scenario Harness — declarative memory audit lab for Umbrella."""

__all__ = [
    "MemoryScenarioRunner",
    "run_all_scenarios",
    "run_scenario_by_id",
]


def __getattr__(name: str):
    if name in __all__:
        from umbrella.evals.memory_scenarios import runner as _runner

        return getattr(_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
