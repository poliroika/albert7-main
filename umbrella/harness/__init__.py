"""Umbrella harness package: parallel multi-candidate Ouroboros runs.

The harness lets the user spend more compute on one task by spinning up
several Ouroboros candidate runs in parallel, scoring them, and applying
the winner.  See :mod:`umbrella.harness.orchestrator` for the entry point.
"""

from umbrella.harness.orchestrator import (
    HarnessCandidateResult,
    HarnessEvent,
    HarnessOrchestrator,
    HarnessResult,
    HarnessStagePlan,
    HarnessStageResult,
)

__all__ = [
    "HarnessCandidateResult",
    "HarnessEvent",
    "HarnessOrchestrator",
    "HarnessResult",
    "HarnessStagePlan",
    "HarnessStageResult",
]
