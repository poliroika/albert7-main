import os
import pathlib
import tempfile
import pytest
from umbrella.memory.palace.facade import MemPalace, _normalize_store_for_write
from umbrella.memory.palace.tiers import Tier, Scope
from umbrella.memory.palace.graph import EdgeType


@pytest.fixture
def palace(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
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


def test_global_alias_write_routes_to_lesson_store(palace):
    node_id = palace.add(
        store="palace.global",
        content="Always verify before declaring success",
        kind="lesson",
        tags=["lesson"],
        tier=Tier.WARM,
        scope=Scope.CROSS_RUN_DURABLE,
    )
    node = palace.get(node_id)
    assert node is not None
    assert node["store"] == "palace.lesson"
    results = palace.search(
        "verify before declaring",
        stores=["palace.global"],
        n=5,
    )
    assert any(r["id"] == node_id for r in results)


def test_palace_durable_list_get_search(palace):
    node_id = palace.add(
        store="palace.durable",
        content="verification report body",
        tier=Tier.WARM,
        scope=Scope.CROSS_RUN_DURABLE,
        tags=["verification_report"],
        verified=True,
    )
    assert palace.get(node_id, stores=["palace.durable"]) is not None
    hits = palace.search("verification report", stores=["palace.durable"], n=5)
    assert any(h["id"] == node_id for h in hits)
    health = palace.health()
    assert "palace.durable" in str(health.get("stores_ok", [])) or health.get("volatile_stub")


def test_search_tags_any_and_tags_all(palace):
    palace.add(
        store="palace.idea",
        content="research finding about GMAS",
        tags=["research", "gmas"],
        tier=Tier.WARM,
        scope=Scope.CROSS_RUN_DURABLE,
    )
    palace.add(
        store="palace.idea",
        content="unrelated plan note",
        tags=["plan"],
        tier=Tier.WARM,
        scope=Scope.CROSS_RUN_DURABLE,
    )
    any_hits = palace.search("", stores=["palace.idea"], tags_any=["research"], n=10)
    assert len(any_hits) == 1
    assert "GMAS" in any_hits[0]["content"]
    all_hits = palace.search("", stores=["palace.idea"], tags_all=["lesson", "gmas"], n=10)
    assert len(all_hits) == 0
    palace.add(
        store="palace.idea",
        content="lesson with gmas tag",
        tags=["lesson", "gmas"],
        tier=Tier.WARM,
        scope=Scope.CROSS_RUN_DURABLE,
    )
    all_hits = palace.search("", stores=["palace.idea"], tags_all=["lesson", "gmas"], n=10)
    assert len(all_hits) == 1


def test_expire_scope_respects_run_key(palace):
    palace.add(
        store="palace.run",
        content="run a memory",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        run_id="run-a",
    )
    palace.add(
        store="palace.run",
        content="run b memory",
        tier=Tier.HOT,
        scope=Scope.RUN_SCOPED,
        run_id="run-b",
    )
    deleted = palace.expire_scope("run", "run-a")
    assert deleted >= 1
    remaining = palace.list_all(stores=["palace.run"], n=20)
    contents = [n["content"] for n in remaining]
    assert "run b memory" in contents
    assert "run a memory" not in contents


def test_null_chroma_stub_shared_across_instances(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    a = MemPalace(tmp_path, "stub_ws")
    try:
        node_id = a.add(
            store="palace.idea",
            content="shared stub memory",
            tags=["stub"],
            kind="observation",
        )
    finally:
        a.close()
    b = MemPalace(tmp_path, "stub_ws")
    try:
        node = b.get(node_id, stores=["palace.idea"])
    finally:
        b.close()
    assert node is not None
    assert "shared stub memory" in str(node.get("content") or "")


def test_normalize_store_for_write_global_alias():
    assert _normalize_store_for_write("palace.global", kind="lesson") == "palace.lesson"
    assert _normalize_store_for_write("palace.global", tags=["failure_pattern"]) == "palace.lesson"
    assert _normalize_store_for_write("palace.idea") == "palace.idea"

