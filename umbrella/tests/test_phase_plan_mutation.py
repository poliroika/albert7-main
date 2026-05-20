import pytest
from umbrella.orchestrator.phase_plan import build_default_plan
from umbrella.phases.base import PlanEdit


def test_default_plan_nodes():
    plan = build_default_plan("ws1")
    assert len(plan.nodes) > 0
    assert plan.nodes[0].id == "preflight"
    assert plan.nodes[-1].id == "verify"


def test_mutate_increments_version():
    plan = build_default_plan("ws1")
    v0 = plan.version
    plan.mutate({"extra": "val"}, actor="test")
    assert plan.version == v0 + 1
    assert len(plan.edits_log) == 1


def test_get_node():
    plan = build_default_plan("ws1")
    node = plan.get_node("research")
    assert node is not None
    assert node.manifest_id == "research"


def test_next_pending():
    plan = build_default_plan("ws1")
    first = plan.next_pending()
    assert first is not None
    assert first.status == "pending"


def test_loop_back_target():
    plan = build_default_plan("ws1")
    # Mark preflight, research and plan as done
    for phase_id in ("preflight", "research", "research_review", "plan"):
        node = plan.get_node(phase_id)
        if node:
            node.status = "done"
    nxt = plan.next_pending()
    assert nxt is not None
    assert nxt.id not in ("preflight", "research", "plan")


def test_save_and_load(tmp_path):
    from umbrella.orchestrator.phase_plan import save_plan, load_plan
    plan = build_default_plan("ws2", run_id="r1")
    plan.mutate({"x": 1}, actor="test")
    save_plan(plan, tmp_path)
    loaded = load_plan(tmp_path)
    assert loaded is not None
    assert loaded.run_id == "r1"
    assert loaded.version == 1
    assert len(loaded.nodes) == len(plan.nodes)
