"""
Seed workspace protection policies and guardrails.

This module defines the default protection policies for seed workspaces
to prevent uncontrolled drift from task-instance modifications.
"""

import logging
from pathlib import Path

from umbrella.evals.models import (
    SeedGuardrail,
    SeedProtectionPolicy,
    StabilityRating,
)

log = logging.getLogger(__name__)


# Default guardrails for seed workspaces
DEFAULT_SEED_GUARDRAILS = [
    SeedGuardrail(
        id="no_destructive_changes",
        name="No Destructive Changes",
        description="Block promotions that delete or fundamentally break seed structure",
        blocked_patterns=[
            "delete",
            "remove core",
            "break graph",
            "remove agent",
            "destroy",
        ],
        required_approvals=[],
    ),
    SeedGuardrail(
        id="no_task_specific_leakage",
        name="No Task-Specific Leakage",
        description="Block promotions that leak specific task details into seed",
        blocked_patterns=[
            "task_main",
            "specific task",
            "one-time",
            "temporary hack",
            "quick fix for this",
        ],
        required_approvals=[],
    ),
    SeedGuardrail(
        id="prompt_changes_require_review",
        name="Prompt Changes Require Review",
        description="Any changes to system prompts require explicit approval",
        blocked_patterns=["system.md", "bible.md", "constitution"],
        required_approvals=["prompt_review"],
    ),
    SeedGuardrail(
        id="gmas_changes_blocked",
        name="GMAS Changes Blocked",
        description="Changes to GMAS core are never auto-promoted",
        blocked_patterns=["gmas/", "gmas\\", "/gmas", "\\gmas"],
        required_approvals=["architecture_review"],
    ),
]


def create_default_policy() -> SeedProtectionPolicy:
    """Create the default seed protection policy."""
    return SeedProtectionPolicy(
        enabled=True,
        require_human_approval_for_promotion=False,
        min_improvement_threshold=0.1,
        min_runs_for_promotion=2,
        require_stability=StabilityRating.MOSTLY_STABLE,
        guardrails=list(DEFAULT_SEED_GUARDRAILS),
    )


def load_policy_from_file(policy_path: Path) -> SeedProtectionPolicy:
    """Load a seed protection policy from a YAML or JSON file.

    Args:
        policy_path: Path to the policy file

    Returns:
        SeedProtectionPolicy loaded from file, or default if file doesn't exist
    """
    if not policy_path.exists():
        log.info(f"Policy file not found: {policy_path}, using default policy")
        return create_default_policy()

    import json

    suffix = policy_path.suffix.lower()

    try:
        if suffix == ".json":
            data = json.loads(policy_path.read_text(encoding="utf-8"))
        elif suffix in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        else:
            log.warning(f"Unknown policy file format: {suffix}, using default")
            return create_default_policy()

        # Reconstruct guardrails
        guardrails = []
        for g_data in data.get("guardrails", []):
            guardrails.append(
                SeedGuardrail(
                    id=g_data.get("id", ""),
                    name=g_data.get("name", ""),
                    description=g_data.get("description", ""),
                    blocked_patterns=g_data.get("blocked_patterns", []),
                    required_approvals=g_data.get("required_approvals", []),
                )
            )

        return SeedProtectionPolicy(
            enabled=data.get("enabled", True),
            require_human_approval_for_promotion=data.get(
                "require_human_approval_for_promotion", False
            ),
            min_improvement_threshold=data.get("min_improvement_threshold", 0.1),
            min_runs_for_promotion=data.get("min_runs_for_promotion", 2),
            require_stability=StabilityRating(
                data.get("require_stability", "mostly_stable")
            ),
            guardrails=guardrails,
        )

    except Exception as e:
        log.error(f"Failed to load policy from {policy_path}: {e}, using default")
        return create_default_policy()


def save_policy_to_file(policy: SeedProtectionPolicy, policy_path: Path) -> None:
    """Save a seed protection policy to a YAML file.

    Args:
        policy: The policy to save
        policy_path: Path to save the policy file
    """
    import yaml

    policy_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "enabled": policy.enabled,
        "require_human_approval_for_promotion": policy.require_human_approval_for_promotion,
        "min_improvement_threshold": policy.min_improvement_threshold,
        "min_runs_for_promotion": policy.min_runs_for_promotion,
        "require_stability": policy.require_stability.value,
        "guardrails": [
            {
                "id": g.id,
                "name": g.name,
                "description": g.description,
                "blocked_patterns": g.blocked_patterns,
                "required_approvals": g.required_approvals,
            }
            for g in policy.guardrails
        ],
    }

    policy_path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    log.info(f"Saved policy to {policy_path}")


def check_promotion_eligibility(
    candidate_id: str,
    patch_description: str,
    policy: SeedProtectionPolicy,
) -> tuple[bool, list[str]]:
    """Check if a promotion candidate is eligible based on policy.

    Args:
        candidate_id: ID of the promotion candidate
        patch_description: Description of the patch
        policy: The seed protection policy to check against

    Returns:
        Tuple of (is_eligible, list_of_blocking_reasons)
    """
    if not policy.enabled:
        return True, []

    blocking_reasons = []

    # Check guardrails
    for guardrail in policy.guardrails:
        desc_lower = patch_description.lower()
        for pattern in guardrail.blocked_patterns:
            if pattern.lower() in desc_lower:
                blocking_reasons.append(
                    f"Guardrail '{guardrail.name}' triggered by pattern '{pattern}'"
                )
                # Track that guardrail was triggered
                guardrail.times_triggered += 1
                import time

                guardrail.last_triggered_at = time.time()

    # Check human approval requirement
    if policy.require_human_approval_for_promotion:
        # In a full implementation, this would check for actual approval
        # For now, we note that human approval is required
        blocking_reasons.append("Human approval required but not yet obtained")

    is_eligible = len(blocking_reasons) == 0

    return is_eligible, blocking_reasons


def create_guardrail(
    guardrail_id: str,
    name: str,
    description: str,
    blocked_patterns: list[str] | None = None,
    required_approvals: list[str] | None = None,
) -> SeedGuardrail:
    """Create a new seed guardrail.

    Args:
        guardrail_id: Unique ID for the guardrail
        name: Human-readable name
        description: What this guardrail protects against
        blocked_patterns: Patterns that trigger this guardrail
        required_approvals: Types of approval needed to bypass

    Returns:
        New SeedGuardrail instance
    """
    return SeedGuardrail(
        id=guardrail_id,
        name=name,
        description=description,
        blocked_patterns=blocked_patterns or [],
        required_approvals=required_approvals or [],
    )


def add_guardrail_to_policy(
    policy: SeedProtectionPolicy,
    guardrail: SeedGuardrail,
) -> SeedProtectionPolicy:
    """Add a guardrail to an existing policy.

    Args:
        policy: The policy to modify
        guardrail: The guardrail to add

    Returns:
        Modified policy (note: creates a new policy object)
    """
    # Create a new policy with the guardrail added
    updated_policy = SeedProtectionPolicy(
        enabled=policy.enabled,
        require_human_approval_for_promotion=policy.require_human_approval_for_promotion,
        min_improvement_threshold=policy.min_improvement_threshold,
        min_runs_for_promotion=policy.min_runs_for_promotion,
        require_stability=policy.require_stability,
        guardrails=list(policy.guardrails) + [guardrail],
    )

    return updated_policy
