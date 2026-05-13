"""Tier 2.3 — PalaceBackend.search must filter out noisy rooms by default.

These tests mock Chroma so we can assert the post-filter behaviour
without spinning up the real vector store.
"""

from typing import Any

import pytest

from umbrella.memory.palace_backend import PalaceBackend


class _StubCollection:
    """In-memory stand-in for a Chroma collection.

    Returns ``(ids, documents, metadatas, distances)`` matching the
    shape Chroma uses. We let ``self.entries`` decide what comes back;
    every entry is a dict with ``id``, ``document``, ``metadata``,
    ``distance``.
    """

    def __init__(self, entries: list[dict[str, Any]]):
        self.entries = list(entries)
        self.last_query: dict[str, Any] = {}

    def query(self, *, query_texts, n_results, where, include):
        self.last_query = {
            "query_texts": list(query_texts or []),
            "n_results": n_results,
            "where": where,
            "include": list(include or []),
        }

        # Filter by ``where`` (only ``wing`` / ``room`` exact match here).
        def _matches(meta: dict[str, Any]) -> bool:
            if not where:
                return True
            if "$and" in where:
                return all(
                    all(meta.get(k) == v for k, v in clause.items())
                    for clause in where["$and"]
                )
            return all(meta.get(k) == v for k, v in where.items())

        picked = [e for e in self.entries if _matches(e["metadata"])][:n_results]
        return {
            "ids": [[e["id"] for e in picked]],
            "documents": [[e["document"] for e in picked]],
            "metadatas": [[e["metadata"] for e in picked]],
            "distances": [[e["distance"] for e in picked]],
        }

    def count(self) -> int:
        return len(self.entries)


@pytest.fixture
def palace_with_mixed_rooms(tmp_path, monkeypatch):
    """A backend whose underlying collection has a mix of room types so we
    can verify the default exclude_rooms set actually filters noise.
    """
    palace = PalaceBackend(tmp_path / "palace")
    entries = [
        {
            "id": "drawer-1",
            "document": "Verified: file_exists accepts list[str] in workspace.toml",
            "metadata": {
                "wing": "wing_ws_demo",
                "hall": "ouroboros",
                "room": "lessons",
            },
            "distance": 0.10,
        },
        {
            "id": "drawer-2",
            "document": "Hypothesis: maybe windows line endings break it",
            "metadata": {
                "wing": "wing_ws_demo",
                "hall": "ouroboros",
                "room": "ideas-hypothesis",
            },
            "distance": 0.12,
        },
        {
            "id": "drawer-3",
            "document": "scratchpad: trying things",
            "metadata": {
                "wing": "wing_ws_demo",
                "hall": "ouroboros",
                "room": "scratchpad",
            },
            "distance": 0.14,
        },
        {
            "id": "drawer-4",
            "document": "Verify run #42: passed",
            "metadata": {
                "wing": "wing_ws_demo",
                "hall": "ouroboros",
                "room": "verify_runs",
            },
            "distance": 0.20,
        },
        {
            "id": "drawer-5",
            "document": "Change: edited main.py",
            "metadata": {
                "wing": "wing_ws_demo",
                "hall": "ouroboros",
                "room": "changes",
            },
            "distance": 0.22,
        },
    ]
    stub = _StubCollection(entries)
    monkeypatch.setattr(palace, "_get_collection", lambda: stub)
    return palace, stub


def test_search_excludes_default_noisy_rooms(palace_with_mixed_rooms):
    palace, _ = palace_with_mixed_rooms

    hits = palace.search("anything", workspace_id="ws_demo", n_results=10)

    rooms = {h["room"] for h in hits}
    # Default exclude_rooms must remove these:
    assert "ideas-hypothesis" not in rooms
    assert "scratchpad" not in rooms
    assert "changes" not in rooms
    # Lessons / verify_runs must remain:
    assert "lessons" in rooms
    assert "verify_runs" in rooms


def test_search_empty_exclude_rooms_keeps_everything(palace_with_mixed_rooms):
    palace, _ = palace_with_mixed_rooms

    hits = palace.search(
        "anything", workspace_id="ws_demo", n_results=10, exclude_rooms=frozenset()
    )

    rooms = {h["room"] for h in hits}
    assert {
        "lessons",
        "ideas-hypothesis",
        "scratchpad",
        "verify_runs",
        "changes",
    } <= rooms


def test_search_custom_exclude_rooms_overrides_defaults(palace_with_mixed_rooms):
    palace, _ = palace_with_mixed_rooms

    hits = palace.search(
        "anything",
        workspace_id="ws_demo",
        n_results=10,
        exclude_rooms={"verify_runs"},
    )

    rooms = {h["room"] for h in hits}
    assert "verify_runs" not in rooms
    # Now scratchpad/ideas-hypothesis are NOT excluded (we replaced the set):
    assert "ideas-hypothesis" in rooms
    assert "scratchpad" in rooms


def test_search_over_fetches_when_filtering(palace_with_mixed_rooms):
    palace, stub = palace_with_mixed_rooms

    palace.search("anything", workspace_id="ws_demo", n_results=2)

    # With filtering active, we should over-fetch to compensate. Without
    # over-fetch a tight n_results=2 would risk returning 0 lessons if
    # both top hits happen to be noisy rooms.
    assert stub.last_query["n_results"] >= 6
