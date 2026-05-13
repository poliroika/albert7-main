"""Runtime verification for Umbrella workspaces.

Provides a small subprocess-driven verifier that runs a spec of steps
(shell commands, HTTP boot probes, import checks) against a workspace
directory and returns a structured report.  The report is consumed by:

- ``umbrella.control_plane.ouroboros_integration`` as a post-run gate.
- ``umbrella.app_ouroboros`` as the driver of the verify-then-retry loop.
- ``umbrella.meta_harness.evaluator`` / ``promotion`` as a runtime signal
  that blocks promotion of non-working candidates.
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
