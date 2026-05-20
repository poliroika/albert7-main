import pathlib
import tempfile
import pytest
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.tiers import Tier, Scope
from umbrella.memory.palace.graph import EdgeType


@pytest.fixture
def palace(tmp_path):
    return MemPalace(repo_root=tmp_path, workspace_id="test_ws")


def test_add_and_health(palace):
    h = palace.health()
    assert "ok" in h


def test_add_transient(palace):
    node_id = palace.add(
        store="palace.transient",
        content="terminal error: connection refused",
        tier=Tier.TRANSIENT,
        scope=Scope.TRANSIENT,
        tags=["error"],
        phase="execute",
    )
    assert isinstance(node_id, str)


def test_add_and_search(palace):
    palace.add(
        store="palace.idea",
        content="Use FastAPI for the web layer with async handlers",
        tier=Tier.WARM,
        scope=Scope.CROSS_RUN_DURABLE,
        tags=["architecture"],
        verified=False,
    )
    results = palace.search("FastAPI web", stores=["palace.idea"])
    assert isinstance(results, list)


def test_link_and_walk(palace):
    a = palace.add(store="palace.run", content="finding A", tier=Tier.HOT, scope=Scope.RUN_SCOPED)
    b = palace.add(store="palace.idea", content="idea B", tier=Tier.WARM, scope=Scope.CROSS_RUN_DURABLE)
    palace.link(a, b, EdgeType.CITES)
    edges = palace.walk(a, edge_types=[EdgeType.CITES], hops=1)
    assert any(e.dst_id == b for e in edges)


def test_recall_bundle(palace):
    bundle = palace.recall("research", n=10, query_seed="FastAPI architecture")
    assert hasattr(bundle, "always_on")
    assert hasattr(bundle, "hot")
    assert hasattr(bundle, "warm")
    assert hasattr(bundle, "graph_neighbours")


def test_recall_filters_hot_memory_to_current_run_and_manifest_tags(palace):
    palace.add(
        store="palace.run",
        content="old run phase plan",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["phase_plan", "umbrella_plan"],
        run_id="old-run",
    )
    palace.add(
        store="palace.run",
        content="current run research summary",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["research_summary"],
        run_id="new-run",
    )
    palace.add(
        store="palace.run",
        content="current run phase plan",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["phase_plan", "umbrella_plan"],
        run_id="new-run",
    )

    bundle = palace.recall(
        "plan_review",
        run_id="new-run",
        hot_rules=[{"store": "palace.run", "tags": ["phase_plan", "subtask_card"]}],
    )

    assert [node["content"] for node in bundle.hot] == ["current run phase plan"]


def test_recall_filters_superseded_plan_drafts_after_loopback(palace):
    palace.add(
        store="palace.run",
        content="research finding stays available",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["research_finding", "research"],
        phase="research",
        run_id="run-loop",
        extra={"created_at": 10},
    )
    palace.add(
        store="palace.run",
        content="old proposal draft must not reach execute",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["phase_plan_proposal", "umbrella_plan_candidate"],
        phase="plan",
        run_id="run-loop",
        extra={"created_at": 20},
    )
    palace.add(
        store="palace.run",
        content="old accepted plan memory must not reach execute",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["phase_plan", "plan"],
        phase="plan",
        run_id="run-loop",
        extra={"created_at": 30},
    )
    palace.add(
        store="palace.run",
        content="latest accepted plan memory reaches execute",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["phase_plan_submitted", "umbrella_plan_selected", "phase_plan"],
        phase="plan",
        run_id="run-loop",
        extra={"created_at": 40},
    )

    bundle = palace.recall(
        "execute",
        run_id="run-loop",
        hot_rules=[{"store": "palace.run", "tags": []}],
        n=10,
    )

    contents = [node["content"] for node in bundle.hot]
    assert contents == [
        "latest accepted plan memory reaches execute",
        "research finding stays available",
    ]


def test_recall_drops_unsubmitted_plan_memory_after_plan_phase(palace):
    palace.add(
        store="palace.run",
        content="research finding stays available",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["research_finding", "research"],
        phase="research",
        run_id="run-draft",
        extra={"created_at": 10},
    )
    palace.add(
        store="palace.run",
        content="direct palace_add phase_plan draft must not reach execute",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        tags=["phase_plan", "plan"],
        phase="plan",
        run_id="run-draft",
        extra={"created_at": 20},
    )

    bundle = palace.recall(
        "execute",
        run_id="run-draft",
        hot_rules=[{"store": "palace.run", "tags": []}],
        n=10,
    )

    contents = [node["content"] for node in bundle.hot]
    assert contents == ["research finding stays available"]


def test_recall_uses_manifest_warm_search_rules(palace):
    palace.add(
        store="palace.idea",
        content="Reusable plan lesson: keep frontend build proof as npm run build.",
        tier=Tier.WARM,
        scope=Scope.CROSS_RUN_DURABLE,
        tags=["plan"],
        extra={"type": "plan"},
    )
    palace.add(
        store="palace.idea",
        content="Research-only note that should not match plan warm filter.",
        tier=Tier.WARM,
        scope=Scope.CROSS_RUN_DURABLE,
        tags=["research"],
        extra={"type": "research"},
    )

    bundle = palace.recall(
        "plan",
        query_seed="frontend build proof",
        warm_search_rules=[
            {"store": "palace.global", "n": 6, "filter": {"type": "plan"}}
        ],
    )

    contents = [node["content"] for node in bundle.warm]
    assert contents == [
        "Reusable plan lesson: keep frontend build proof as npm run build."
    ]


def test_expire_scope(palace):
    palace.add(
        store="palace.phase",
        content="phase scratchpad note",
        tier=Tier.HOT,
        scope=Scope.PHASE_SCOPED,
        phase="research",
    )
    count = palace.expire_scope("phase", "research")
    assert isinstance(count, int)


def test_migrators(palace, tmp_path):
    import json
    from umbrella.memory.palace.migrators import run_full_migration

    mem_root = tmp_path / "memory"
    mem_root.mkdir()
    (mem_root / "lessons.jsonl").write_text(
        json.dumps({"content": "Always write tests", "tags": ["testing"]}) + "\n"
    )
    (mem_root / "ideas.jsonl").write_text(
        json.dumps({"content": "Consider using Redis", "evidence_kind": "hypothesis", "tags": []}) + "\n"
        + json.dumps({"content": "FastAPI works well", "evidence_kind": "verified_outcome", "tags": ["api"]}) + "\n"
    )
    results = run_full_migration(palace, mem_root)
    assert results["lessons"] == 1
    assert results["ideas"] == 2
    assert not (mem_root / "lessons.jsonl").exists()
