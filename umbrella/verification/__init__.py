"""Runtime verification for Umbrella workspaces.

Provides a small subprocess-driven verifier that runs a spec of steps
(shell commands, HTTP boot probes, import checks) against a workspace
directory and returns a structured report. Consumed by the ``verify``
phase manifest inside :class:`umbrella.orchestrator.runner.PhaseRunner`.
"""

from umbrella.verification.models import (
    VerificationReport,
    VerificationStep,
    VerificationStepKind,
    VerificationStepResult,
    VerificationStatus,
)
from umbrella.verification.final_sweep import (
    SweepReport,
    run_workspace_sweep,
)
from umbrella.verification.runner import run_verification
from umbrella.verification.spec_loader import (
    VerificationSpecError,
    load_verification_spec,
)

__all__ = [
    "VerificationReport",
    "VerificationStatus",
    "VerificationStep",
    "VerificationStepKind",
    "VerificationStepResult",
    "run_verification",
    "load_verification_spec",
    "VerificationSpecError",
    "SweepReport",
    "run_workspace_sweep",
]
