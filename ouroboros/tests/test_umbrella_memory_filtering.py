"""Tests for agent-facing Umbrella memory recall filtering."""

from types import SimpleNamespace

from ouroboros.tools.umbrella_tools import (
    _is_unverified_memory,
    _lesson_is_verified,
    _split_verified_first,
)


def test_memory_filter_detects_unverified_tags_and_rooms() -> None:
    assert _is_unverified_memory({"tags": "candidate,unverified"})
    assert _is_unverified_memory({"room": "ideas-hypothesis"})
    assert _is_unverified_memory(
        {"metadata": {"evidence_kind": "observation_from_log"}}
    )
    assert _is_unverified_memory({"metadata": {"verified": "False"}})
    assert not _is_unverified_memory({"tags": "verified", "room": "verify_runs"})


def test_split_verified_first_separates_candidates() -> None:
    trusted, unverified = _split_verified_first(
        [
            {"id": "verified", "room": "verify_runs"},
            {"id": "candidate", "tags": ["candidate"]},
        ]
    )

    assert [item["id"] for item in trusted] == ["verified"]
    assert [item["id"] for item in unverified] == ["candidate"]


def test_lesson_requires_verified_priority_and_tags() -> None:
    assert _lesson_is_verified(SimpleNamespace(priority=7, tags={"success"}))
    assert not _lesson_is_verified(SimpleNamespace(priority=3, tags={"success"}))
    assert not _lesson_is_verified(
        SimpleNamespace(priority=9, tags={"unverified_lesson"})
    )
