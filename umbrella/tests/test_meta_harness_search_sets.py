"""Tests for Meta-Harness search set builders."""

import json

import pytest

from umbrella.meta_harness.search_sets import (
    build_search_set_from_memory,
    build_search_set_from_workspaces,
    load_search_set,
    merge_search_sets,
    write_search_set,
)
from umbrella.meta_harness.models import SearchSet, SearchTask


@pytest.fixture
def repo_with_lessons(tmp_path):
    lessons_dir = tmp_path / ".umbrella" / "memory"
    lessons_dir.mkdir(parents=True)
    lessons = [
        json.dumps(
            {
                "id": "lesson_1",
                "task_id": "t1",
                "workspace_id": "ws1",
                "tags": ["eval_failure", "partial"],
                "change_summary": "Added retry logic",
                "conclusion": "Still failing",
            }
        ),
        json.dumps(
            {
                "id": "lesson_2",
                "task_id": "t2",
                "workspace_id": "ws2",
                "tags": ["success"],
                "change_summary": "Fixed parsing",
                "conclusion": "Working now",
            }
        ),
        json.dumps(
            {
                "id": "lesson_3",
                "task_id": "t3",
                "workspace_id": "ws3",
                "tags": ["HIGH_COST_NO_GAIN"],
                "change_summary": "Expensive rewrite",
                "conclusion": "No improvement",
            }
        ),
    ]
    (lessons_dir / "lessons.jsonl").write_text("\n".join(lessons), encoding="utf-8")
    return tmp_path


@pytest.fixture
def repo_with_workspaces(tmp_path):
    for ws_name in ("ws_alpha", "ws_beta"):
        ws_dir = tmp_path / "workspaces" / ws_name
        ws_dir.mkdir(parents=True)
        (ws_dir / "TASK_MAIN.md").write_text(f"# Task for {ws_name}", encoding="utf-8")
    return tmp_path


class TestBuildFromMemory:
    def test_finds_failures(self, repo_with_lessons):
        ss = build_search_set_from_memory(repo_with_lessons)
        assert ss.size >= 2
        task_ids = {t.task_id for t in ss.tasks}
        assert "t1" in task_ids
        assert "t3" in task_ids
        assert "t2" not in task_ids  # success, not failure

    def test_empty_repo(self, tmp_path):
        ss = build_search_set_from_memory(tmp_path)
        assert ss.size == 0

    def test_respects_limit(self, repo_with_lessons):
        ss = build_search_set_from_memory(repo_with_lessons, limit=1)
        assert ss.size == 1


class TestBuildFromWorkspaces:
    def test_finds_workspaces(self, repo_with_workspaces):
        ss = build_search_set_from_workspaces(repo_with_workspaces)
        assert ss.size == 2
        ws_ids = {t.workspace_id for t in ss.tasks}
        assert "ws_alpha" in ws_ids
        assert "ws_beta" in ws_ids

    def test_empty_repo(self, tmp_path):
        ss = build_search_set_from_workspaces(tmp_path)
        assert ss.size == 0


class TestMerge:
    def test_deduplicates(self):
        ss1 = SearchSet(
            tasks=[SearchTask(task_id="t1", workspace_id="ws1", task_text="a")]
        )
        ss2 = SearchSet(
            tasks=[
                SearchTask(task_id="t1", workspace_id="ws1", task_text="a"),
                SearchTask(task_id="t2", workspace_id="ws2", task_text="b"),
            ]
        )
        merged = merge_search_sets(ss1, ss2)
        assert merged.size == 2


class TestPersistence:
    def test_write_and_load(self, tmp_path):
        ss = SearchSet(
            name="test",
            tasks=[SearchTask(task_id="t1", workspace_id="ws1", task_text="test")],
        )
        path = tmp_path / "search_set.json"
        write_search_set(path, ss)
        loaded = load_search_set(path)
        assert loaded.name == "test"
        assert loaded.size == 1
        assert loaded.tasks[0].task_id == "t1"
