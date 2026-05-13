"""
End-to-end integration layer for the Umbrella manager system.

This module provides the bootstrap and service orchestration that wires together
all subsystems (policy, registry, runtime, retrieval, memory, control-plane, evals, telemetry)
into a coherent manager workflow.
"""

from umbrella.integration.services import UmbrellaServices
from umbrella.integration.runner import run_manager_task, ManagerRunResult
from umbrella.integration.demo import create_demo_runner, DemoScenario

__all__ = [
    "UmbrellaServices",
    "run_manager_task",
    "ManagerRunResult",
    "create_demo_runner",
    "DemoScenario",
]
