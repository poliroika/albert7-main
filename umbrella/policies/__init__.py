"""
Policy definitions for umbrella integration layer.

This module provides the policy system that governs:
- System boundaries (gmas, ouroboros, workspaces)
- Edit surfaces and mutation rules
- Self-improvement gating
- Human escalation requirements
"""

from umbrella.policies.models import (
    SystemBoundaryPolicy,
    EditSurfacePolicy,
    SelfImprovementPolicy,
    EscalationPolicy,
    WorkspaceMutationPolicy,
    FrameworkBoundaryPolicy,
    PolicyDecision,
    EditSurfaceCategory,
    SelfImprovementTrigger,
    EscalationReason,
)
from umbrella.policies.defaults import (
    load_default_policy,
    DEFAULT_POLICY_PATH,
)
from umbrella.policies.loader import (
    load_policy,
    load_policy_from_file,
)
from umbrella.policies.engine import (
    can_edit_path,
    should_prefer_workspace_patch,
    can_trigger_self_improvement,
    requires_human_escalation,
    classify_path,
    PolicyEngine,
)

__all__ = [
    # Models
    "SystemBoundaryPolicy",
    "EditSurfacePolicy",
    "SelfImprovementPolicy",
    "EscalationPolicy",
    "WorkspaceMutationPolicy",
    "FrameworkBoundaryPolicy",
    "PolicyDecision",
    "EditSurfaceCategory",
    "SelfImprovementTrigger",
    "EscalationReason",
    # Defaults
    "load_default_policy",
    "DEFAULT_POLICY_PATH",
    # Loader
    "load_policy",
    "load_policy_from_file",
    # Engine
    "can_edit_path",
    "should_prefer_workspace_patch",
    "can_trigger_self_improvement",
    "requires_human_escalation",
    "classify_path",
    "PolicyEngine",
]
