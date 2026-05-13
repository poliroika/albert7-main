"""Tests for contrastive memory retrieval."""

import pytest

from umbrella.memory.contrastive import (
    _classify_outcome,
    render_contrastive_memory_section,
    retrieve_contrastive_lessons,
)
from umbrella.memory.models import (
    MemoryConfig,
    WorkspaceLessonRecord,
    generate_lesson_id,
)
from umbrella.memory.store import MemoryStore


@pytest.fixture
def store_with_lessons(tmp_path):
    config = MemoryConfig(
        memory_root=tmp_path / "memory",
        lessons_path=tmp_path / "memory" / "lessons.jsonl",
        gaps_path=tmp_path / "memory" / "gaps.jsonl",
        signals_path=tmp_path / "memory" / "signals.jsonl",
    )
    store = MemoryStore(config)

    # Add success lessons
    for i in range(3):
        store.add_lesson(
            WorkspaceLessonRecord(
                id=generate_lesson_id(),
                task_id=f"task_{i}",
                workspace_id="ws1",
                change_summary=f"Success change {i}",
                expected_effect="Improvement",
                observed_effect="Worked as expected",
                conclusion="Successful improvement",
                evidence_summary="Tests pass",
                tags={"success", "promoted"},
                repeat_tags=["use_caching"],
            )
        )

    # Add failure lessons
    for i in range(3):
        store.add_lesson(
            WorkspaceLessonRecord(
                id=generate_lesson_id(),
                task_id=f"fail_{i}",
                workspace_id="ws1",
                change_summary=f"Failed change {i}",
                expected_effect="Fix bug",
                observed_effect="Still broken",
                conclusion="Did not fix the issue",
                evidence_summary="Tests still fail",
                tags={"failure", "eval_failure"},
                avoid_tags=["blind_retry"],
            )
        )

    return store


class TestClassifyOutcome:
    def test_success_tags(self):
        lesson = WorkspaceLessonRecord(
            id="test",
            task_id="t",
            workspace_id="ws",
            change_summary="x",
            expected_effect="x",
            observed_effect="x",
            conclusion="x",
            evidence_summary="x",
            tags={"success", "promoted"},
        )
        assert _classify_outcome(lesson) == "success"

    def test_failure_tags(self):
        lesson = WorkspaceLessonRecord(
            id="test",
            task_id="t",
            workspace_id="ws",
            change_summary="x",
            expected_effect="x",
            observed_effect="x",
            conclusion="x",
            evidence_summary="x",
            tags={"failure"},
        )
        assert _classify_outcome(lesson) == "failure"

    def test_neutral(self):
        lesson = WorkspaceLessonRecord(
            id="test",
            task_id="t",
            workspace_id="ws",
            change_summary="x",
            expected_effect="x",
            observed_effect="x",
            conclusion="x",
            evidence_summary="x",
            tags={"observation"},
        )
        assert _classify_outcome(lesson) == "neutral"

    def test_observed_effect_keywords(self):
        lesson = WorkspaceLessonRecord(
            id="test",
            task_id="t",
            workspace_id="ws",
            change_summary="x",
            expected_effect="x",
            observed_effect="Tests failed with timeout",
            conclusion="x",
            evidence_summary="x",
        )
        assert _classify_outcome(lesson) == "failure"


class TestRetrieveContrastive:
    def test_returns_successes_and_failures(self, store_with_lessons):
        result = retrieve_contrastive_lessons(
            store_with_lessons,
            workspace_id="ws1",
        )
        assert len(result["successes"]) > 0
        assert len(result["failures"]) > 0

    def test_respects_limits(self, store_with_lessons):
        result = retrieve_contrastive_lessons(
            store_with_lessons,
            workspace_id="ws1",
            limit_successes=1,
            limit_failures=1,
        )
        assert len(result["successes"]) <= 1
        assert len(result["failures"]) <= 1

    def test_collects_tags(self, store_with_lessons):
        result = retrieve_contrastive_lessons(
            store_with_lessons,
            workspace_id="ws1",
        )
        assert "use_caching" in result["repeat_tags"]
        assert "blind_retry" in result["avoid_tags"]

    def test_empty_store(self, tmp_path):
        config = MemoryConfig(
            memory_root=tmp_path / "memory",
            lessons_path=tmp_path / "memory" / "lessons.jsonl",
            gaps_path=tmp_path / "memory" / "gaps.jsonl",
            signals_path=tmp_path / "memory" / "signals.jsonl",
        )
        store = MemoryStore(config)
        result = retrieve_contrastive_lessons(store)
        assert result["successes"] == []
        assert result["failures"] == []


class TestDisputedClusters:
    """Tier 2.3 — when multiple unverified lessons cluster around the same
    topic with no verified anchor, they are flagged DISPUTED so recall
    surfaces uncertainty instead of presenting them as facts.
    """

    def _make_store(self, tmp_path):
        config = MemoryConfig(
            memory_root=tmp_path / "memory",
            lessons_path=tmp_path / "memory" / "lessons.jsonl",
            gaps_path=tmp_path / "memory" / "gaps.jsonl",
            signals_path=tmp_path / "memory" / "signals.jsonl",
        )
        return MemoryStore(config)

    def test_multiple_unverified_lessons_on_same_topic_flagged_disputed(self, tmp_path):
        store = self._make_store(tmp_path)
        for i, conclusion in enumerate(
            [
                "core_files_exist failed because of path resolution",
                "core_files_exist failed because of windows line endings",
                "core_files_exist failed because of missing prefix",
            ]
        ):
            store.add_lesson(
                WorkspaceLessonRecord(
                    id=generate_lesson_id(),
                    task_id=f"t{i}",
                    workspace_id="ws_news",
                    change_summary=f"core_files_exist verification fix attempt {i}",
                    expected_effect="verification passes",
                    observed_effect="still failing",
                    conclusion=conclusion,
                    evidence_summary="failed retry",
                    tags={"unverified_lesson", "avoid"},
                    priority=1,
                )
            )

        bundle = retrieve_contrastive_lessons(store, workspace_id="ws_news")
        clusters = bundle.get("disputed_clusters") or []
        assert clusters, "expected at least one disputed cluster"
        assert clusters[0]["lesson_count"] >= 2
        assert clusters[0]["label"].startswith("[DISPUTED")

        rendered = render_contrastive_memory_section(bundle)
        assert "[DISPUTED" in rendered

    def test_verified_anchor_resolves_dispute(self, tmp_path):
        """A cluster with at least one verified (priority 5, no unverified
        tag) lesson is NOT marked disputed — the verified one settles it.
        """
        store = self._make_store(tmp_path)
        store.add_lesson(
            WorkspaceLessonRecord(
                id=generate_lesson_id(),
                task_id="t_v",
                workspace_id="ws_news",
                change_summary="core_files_exist verification fix",
                expected_effect="passes",
                observed_effect="passes after path list expansion",
                conclusion="path list expansion fixes it",
                evidence_summary="verify_run_id=round-42",
                tags={"verified"},
                priority=5,
            )
        )
        store.add_lesson(
            WorkspaceLessonRecord(
                id=generate_lesson_id(),
                task_id="t_unv",
                workspace_id="ws_news",
                change_summary="core_files_exist verification fix",
                expected_effect="passes",
                observed_effect="still broken (old theory)",
                conclusion="windows line endings",
                evidence_summary="no verify run",
                tags={"unverified_lesson", "avoid"},
                priority=1,
            )
        )
        bundle = retrieve_contrastive_lessons(store, workspace_id="ws_news")
        assert bundle.get("disputed_clusters") == []


class TestRenderContrastive:
    def test_renders_sections(self, store_with_lessons):
        bundle = retrieve_contrastive_lessons(
            store_with_lessons,
            workspace_id="ws1",
        )
        rendered = render_contrastive_memory_section(bundle)
        assert "What Worked" in rendered
        assert "What Failed" in rendered

    def test_empty_bundle(self):
        rendered = render_contrastive_memory_section({})
        assert rendered == ""
