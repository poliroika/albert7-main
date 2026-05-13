"""Per-workspace physical isolation for MemoryStore."""

from umbrella.memory.models import (
    MemoryQuery,
    WorkspaceLessonRecord,
    generate_lesson_id,
)
from umbrella.memory.paths import get_workspace_store


def test_workspace_lessons_isolated(tmp_path) -> None:
    repo = tmp_path / "repo"
    (repo / "workspaces" / "alpha").mkdir(parents=True)
    (repo / "workspaces" / "beta").mkdir(parents=True)

    a = get_workspace_store(repo, "alpha")
    b = get_workspace_store(repo, "beta")

    la = WorkspaceLessonRecord(
        id=generate_lesson_id(),
        task_id="t1",
        workspace_id="alpha",
        change_summary="only alpha",
        expected_effect="x",
        observed_effect="y",
        conclusion="z",
        evidence_summary="e",
        tags=set(),
    )
    a.add_lesson(la)

    mq = MemoryQuery(limit=20, include_stale=True)
    mq.workspace_id = "beta"
    assert b.query_lessons(mq) == []

    mq.workspace_id = "alpha"
    got = a.query_lessons(mq)
    assert len(got) == 1
    assert got[0].change_summary == "only alpha"
