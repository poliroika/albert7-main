"""
Umbrella harness — DEPRECATED package, kept for import compatibility only.

The harness model is now phase-level and lives inside :class:`umbrella.orchestrator.runner.PhaseRunner`.
Pass ``candidates_per_phase=N`` to PhaseRunner (or ``harness_candidates`` in the
``POST /api/runs`` payload) to enable parallel candidates for each phase. The
watcher / heuristic picks the winner at the end of each phase.

This module no longer exports any symbols.
"""

__all__: list[str] = []
