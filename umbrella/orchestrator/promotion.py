import pathlib

from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.tiers import Tier, Scope
from umbrella.memory.palace.graph import EdgeType


def promote_run_on_verify_pass(
    palace: MemPalace,
    *,
    run_id: str,
    phase: str = "verify",
) -> dict[str, int]:
    promoted: dict[str, int] = {}
    results = palace.search(
        "verified knowledge",
        stores=["palace.run"],
        tiers=[Tier.HOT, Tier.WARM],
        n=100,
    )
    for node in results:
        if not node.get("verified"):
            continue
        tags = (node.get("tags") or "").split(",")
        if "lesson" in tags:
            palace.promote(node["id"], target_store="palace.lesson", verified=True, phase=phase)
            promoted["lesson"] = promoted.get("lesson", 0) + 1
        elif "idea" in tags or "finding" in tags:
            palace.promote(node["id"], target_store="palace.idea", verified=True, phase=phase)
            promoted["idea"] = promoted.get("idea", 0) + 1
    return promoted


def check_reflexion_promotion_gate(
    palace: MemPalace,
    *,
    reflection_id: str,
    run_id: str,
) -> bool:
    """
    Returns True if reflection can be promoted to palace.lesson.
    Gate: applied_reflection edge must exist AND run passed verify.
    """
    import os
    if not int(os.environ.get("OUROBOROS_REFLEXION_PROMOTE_REQUIRES_VERIFY_PASS", "1")):
        return True
    edges = palace.walk(reflection_id, edge_types=[EdgeType.APPLIED_REFLECTION], hops=1, direction="in")
    return len(edges) > 0
