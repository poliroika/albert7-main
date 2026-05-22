"""Tests for the memory_hooks module.

Strategy: we don't want to spin up a real ChromaDB for unit tests, so
we monkeypatch ``_safe_palace`` and ``_safe_store`` to return fakes
that record what the hooks did. That keeps the tests pinned to the
*contract* of the hook (what it asks of the palace, what it composes
into the system message) without binding them to MemPalace internals.
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ouroboros import memory_hooks


class _FakePalace:
    """Minimal palace stand-in. Records every call so tests can assert."""

    def __init__(self, *, recent_payload=None, search_payload=None):
        self.recent_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.add_calls: list[dict] = []
        self._recent_payload = list(recent_payload or [])
        self._search_payload = list(search_payload or [])

    def recent(self, *, workspace_id="", limit=20):
        self.recent_calls.append({"workspace_id": workspace_id, "limit": limit})
        return list(self._recent_payload)

    def search(self, query, *, workspace_id="", room="", n_results=10):
        self.search_calls.append(
            {
                "query": query,
                "workspace_id": workspace_id,
                "room": room,
                "n_results": n_results,
            }
        )
        return list(self._search_payload)

    def add(self, **kwargs):
        self.add_calls.append(kwargs)
        return {"id": "drawer_fake", "wing": f"wing_{kwargs.get('workspace_id', 'x')}"}


class _FakeLessonStore:
    def __init__(self, lessons=None, gaps=None):
        self._lessons = list(lessons or [])
        self._gaps = list(gaps or [])

    def query_lessons(self, _query):
        return list(self._lessons)

    def get_active_gaps(self):
        return list(self._gaps)


class TestRecallForTaskStart(unittest.TestCase):
    def test_returns_empty_when_palace_unavailable(self):
        with patch.object(memory_hooks, "_safe_palace", return_value=None):
            out = memory_hooks.recall_for_task_start(
                workspace_id="JKX",
                task_input="build a thing",
                repo_root=Path("/tmp"),
            )
        self.assertEqual(out, "")

    def test_returns_empty_when_palace_has_nothing(self):
        palace = _FakePalace(recent_payload=[], search_payload=[])
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            out = memory_hooks.recall_for_task_start(
                workspace_id="JKX",
                task_input="build a thing",
                repo_root=Path("/tmp"),
            )
        self.assertEqual(out, "")
        # Workspace + manager palace both call recent/search when available.
        self.assertEqual(len(palace.recent_calls), 2)
        self.assertEqual(len(palace.search_calls), 2)

    def test_renders_recent_and_semantic_blocks(self):
        palace = _FakePalace(
            recent_payload=[
                {
                    "id": "d1",
                    "content": "fixed pipeline bug",
                    "room": "changes",
                    "hall": "hall_facts",
                },
            ],
            search_payload=[
                {
                    "id": "d2",
                    "content": "categories mismatch RU vs EN",
                    "room": "lessons",
                    "hall": "hall_discoveries",
                    "distance": 0.21,
                },
            ],
        )
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            out = memory_hooks.recall_for_task_start(
                workspace_id="JKX",
                task_input="generate dataset",
                repo_root=Path("/tmp"),
            )
        self.assertIn("[MEMORY_RECALL]", out)
        self.assertIn("workspace=JKX", out)
        self.assertIn("Recent (1)", out)
        self.assertIn("fixed pipeline bug", out)
        self.assertIn("Semantic matches (1)", out)
        self.assertIn("categories mismatch", out)
        self.assertIn("d=0.21", out)

    def test_phase_reranks_relevant_memory(self):
        palace = _FakePalace(
            recent_payload=[
                {
                    "id": "h",
                    "content": "cleanup pycache leftovers",
                    "room": "hygiene",
                    "hall": "ops",
                },
                {
                    "id": "g",
                    "content": "GMAS graph implementation pattern",
                    "room": "lessons",
                    "hall": "code",
                },
            ],
            search_payload=[],
        )
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            out = memory_hooks.recall_for_task_start(
                workspace_id="JKX",
                task_input="build gmas graph",
                repo_root=Path("/tmp"),
                phase="subtask_1",
            )
        assert out.index("GMAS graph implementation") < out.index("cleanup pycache")

    def test_passes_workspace_id_through_to_palace(self):
        palace = _FakePalace(recent_payload=[{"content": "x", "room": "r"}])
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            memory_hooks.recall_for_task_start(
                workspace_id="JKX",
                task_input="something",
                repo_root=Path("/tmp"),
            )
        self.assertEqual(palace.recent_calls[0]["workspace_id"], "JKX")
        self.assertEqual(palace.search_calls[0]["workspace_id"], "JKX")


class TestRecallPeriodic(unittest.TestCase):
    def test_minimal_bundle_below_rich_threshold(self):
        palace = _FakePalace(
            recent_payload=[{"content": "ok", "room": "events"}],
            search_payload=[],
        )
        store = _FakeLessonStore(
            lessons=[
                SimpleNamespace(
                    change_summary="x",
                    expected_effect="y",
                    observed_effect="z",
                )
            ]
        )
        with (
            patch.object(memory_hooks, "_safe_palace", return_value=palace),
            patch.object(memory_hooks, "_safe_store", return_value=store),
        ):
            out = memory_hooks.recall_periodic(
                workspace_id="JKX",
                round_idx=20,  # below default RICH_RECALL_THRESHOLD=50
                recent_actions_summary="updated pipeline.py",
                repo_root=Path("/tmp"),
            )
        # Lessons and verify_runs only show up in the rich bundle.
        self.assertIn("[MEMORY_RECALL]", out)
        self.assertIn("Periodic recall", out)
        self.assertNotIn("(rich)", out)
        self.assertNotIn("Structured lessons", out)
        self.assertNotIn("Recent verify runs", out)

    def test_rich_bundle_above_threshold_includes_lessons(self):
        palace = _FakePalace(
            recent_payload=[{"content": "ok", "room": "events"}],
            search_payload=[],
        )
        lessons = [
            SimpleNamespace(
                change_summary="ran tests",
                expected_effect="green",
                observed_effect="2 failed",
            )
        ]
        store = _FakeLessonStore(lessons=lessons)
        with (
            patch.object(memory_hooks, "_safe_palace", return_value=palace),
            patch.object(memory_hooks, "_safe_store", return_value=store),
        ):
            out = memory_hooks.recall_periodic(
                workspace_id="JKX",
                round_idx=80,  # above RICH_RECALL_THRESHOLD
                recent_actions_summary="adding GMAS graph",
                repo_root=Path("/tmp"),
            )
        self.assertIn("(rich)", out)
        self.assertIn("Structured lessons", out)
        self.assertIn("ran tests", out)

    def test_rich_bundle_queries_verify_runs_room(self):
        palace = _FakePalace(
            recent_payload=[{"content": "ok", "room": "events"}],
            search_payload=[],
        )
        store = _FakeLessonStore(lessons=[])
        with (
            patch.object(memory_hooks, "_safe_palace", return_value=palace),
            patch.object(memory_hooks, "_safe_store", return_value=store),
        ):
            memory_hooks.recall_periodic(
                workspace_id="JKX",
                round_idx=80,
                recent_actions_summary="something",
                repo_root=Path("/tmp"),
            )
        # We expect at least two .search() calls: the activity-based one
        # plus the verify_runs lookup with room="verify_runs".
        rooms = [c["room"] for c in palace.search_calls]
        self.assertIn("verify_runs", rooms)


class TestAutoRecallInjection(unittest.TestCase):
    def test_init_loop_memory_does_not_inject_by_default(self):
        """Task-start recall is opt-in; the agent should call memory explicitly."""

        messages = [{"role": "user", "content": "Workspace: workspaces/JKX\nBuild it."}]
        ctx = SimpleNamespace(host_repo_root=Path("/tmp"), repo_dir=Path("/tmp"))

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(
                memory_hooks, "recall_for_task_start", return_value="[MEMORY_RECALL] x"
            ) as recall,
        ):
            _repo_root, ws = memory_hooks.init_loop_memory(messages, ctx)

        self.assertEqual(ws, "JKX")
        self.assertEqual(len(messages), 1)
        recall.assert_not_called()

    def test_init_loop_memory_can_be_opted_out(self):
        messages = [{"role": "user", "content": "Workspace: workspaces/JKX\nBuild it."}]
        ctx = SimpleNamespace(host_repo_root=Path("/tmp"), repo_dir=Path("/tmp"))

        with (
            patch.dict("os.environ", {"OUROBOROS_TASK_START_RECALL": "0"}, clear=True),
            patch.object(
                memory_hooks, "recall_for_task_start", return_value="[MEMORY_RECALL] x"
            ) as recall,
        ):
            _repo_root, ws = memory_hooks.init_loop_memory(messages, ctx)

        self.assertEqual(ws, "JKX")
        self.assertEqual(len(messages), 1)
        recall.assert_not_called()

    def test_init_loop_memory_injects_when_explicitly_enabled(self):
        messages = [{"role": "user", "content": "Workspace: workspaces/JKX\nBuild it."}]
        ctx = SimpleNamespace(host_repo_root=Path("/tmp"), repo_dir=Path("/tmp"))

        with (
            patch.dict("os.environ", {"OUROBOROS_TASK_START_RECALL": "1"}, clear=True),
            patch.object(
                memory_hooks, "recall_for_task_start", return_value="[MEMORY_RECALL] x"
            ),
        ):
            _repo_root, ws = memory_hooks.init_loop_memory(messages, ctx)

        self.assertEqual(ws, "JKX")
        self.assertEqual(messages[-1]["content"], "[MEMORY_RECALL] x")

    def test_init_loop_memory_injects_core_overlay_for_umbrella_managed(self):
        messages = [{"role": "user", "content": "Workspace: workspaces/JKX\nBuild it."}]
        ctx = SimpleNamespace(
            host_repo_root=Path("/tmp"),
            repo_dir=Path("/tmp"),
            umbrella_managed=True,
            umbrella_phase_id="research",
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(
                memory_hooks,
                "recall_core_overlay_for_task_start",
                return_value="## [ALWAYS-LOADED MEMORY]\n### BKB\n",
            ) as core_recall,
            patch.object(memory_hooks, "recall_for_task_start") as legacy_recall,
        ):
            _repo_root, ws = memory_hooks.init_loop_memory(messages, ctx)

        self.assertEqual(ws, "JKX")
        core_recall.assert_called_once()
        legacy_recall.assert_not_called()
        self.assertIn("[ALWAYS-LOADED MEMORY]", messages[-1]["content"])

    def test_init_loop_memory_skips_core_overlay_when_umbrella_task_already_has_proactive_memory(
        self,
    ):
        messages = [{"role": "user", "content": "Workspace: workspaces/JKX\nBuild it."}]
        ctx = SimpleNamespace(
            host_repo_root=Path("/tmp"),
            repo_dir=Path("/tmp"),
            umbrella_managed=True,
            context_overlays={
                "prevent_ouroboros_auto_core_overlay": True,
                "proactive_memory_rendered_in_task_input": True,
            },
        )

        with patch.object(
            memory_hooks,
            "recall_core_overlay_for_task_start",
            return_value="## [ALWAYS-LOADED MEMORY]\n### BKB\n",
        ) as core_recall:
            _repo_root, ws = memory_hooks.init_loop_memory(messages, ctx)

        core_recall.assert_not_called()
        self.assertEqual(len(messages), 1)

    def test_periodic_recall_does_not_inject_by_default(self):
        messages: list[dict] = []
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(
                memory_hooks, "recall_periodic", return_value="[MEMORY_RECALL] x"
            ) as recall,
        ):
            last = memory_hooks.maybe_inject_periodic_recall(
                workspace_id="JKX",
                round_idx=100,
                last_recall_round=0,
                recent_actions_summary="changed x",
                repo_root=Path("/tmp"),
                messages=messages,
            )

        self.assertEqual(last, 0)
        self.assertEqual(messages, [])
        recall.assert_not_called()

    def test_periodic_recall_injects_when_opted_in(self):
        messages: list[dict] = []
        with (
            patch.dict("os.environ", {"OUROBOROS_PERIODIC_RECALL": "1"}, clear=True),
            patch.object(
                memory_hooks, "recall_periodic", return_value="[MEMORY_RECALL] x"
            ),
        ):
            last = memory_hooks.maybe_inject_periodic_recall(
                workspace_id="JKX",
                round_idx=100,
                last_recall_round=0,
                recent_actions_summary="changed x",
                repo_root=Path("/tmp"),
                messages=messages,
            )

        self.assertEqual(last, 100)
        self.assertEqual(messages[-1]["content"], "[MEMORY_RECALL] x")

    def test_periodic_recall_filters_stale_run_scoped_change_memory(self):
        workspace_palace = _FakePalace(
            recent_payload=[
                {
                    "id": "old",
                    "content": "old run write",
                    "room": "changes",
                    "task_id": "run-old:execute",
                },
                {
                    "id": "current",
                    "content": "current run write",
                    "room": "changes",
                    "task_id": "run-new:execute",
                },
                {
                    "id": "lesson",
                    "content": "durable lesson",
                    "room": "lessons",
                },
            ],
            search_payload=[],
        )
        manager_palace = _FakePalace(recent_payload=[], search_payload=[])

        def _safe(_repo_root, workspace_id):
            return workspace_palace if workspace_id else manager_palace

        with patch.object(memory_hooks, "_safe_palace", side_effect=_safe):
            out = memory_hooks.recall_periodic(
                workspace_id="JKX",
                round_idx=3,
                recent_actions_summary="changed x",
                repo_root=Path("/tmp"),
                task_id="run-new:execute",
            )

        self.assertIn("current run write", out)
        self.assertIn("durable lesson", out)
        self.assertNotIn("old run write", out)


class TestRecordWorkspaceChange(unittest.TestCase):
    def test_records_to_changes_room_on_success(self):
        palace = _FakePalace()
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            memory_hooks.record_workspace_change(
                workspace_id="JKX",
                tool_name="update_workspace_seed",
                args_summary="file_path=jkx/pipeline.py",
                result_summary="ok",
                repo_root=Path("/tmp"),
                success=True,
            )
        self.assertEqual(len(palace.add_calls), 1)
        call = palace.add_calls[0]
        self.assertEqual(call["workspace_id"], "JKX")
        self.assertEqual(call["room"], "changes")
        self.assertEqual(call["event_type"], "change")
        self.assertIn("update_workspace_seed", call["tags"])

    def test_records_task_and_run_metadata_for_auto_changes(self):
        palace = _FakePalace()
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            memory_hooks.record_workspace_change(
                workspace_id="JKX",
                tool_name="apply_workspace_patch",
                args_summary="file_path=src/app.py",
                result_summary="ok",
                repo_root=Path("/tmp"),
                success=True,
                task_id="run-42:execute",
            )
        call = palace.add_calls[0]
        self.assertEqual(call["task_id"], "run-42:execute")
        self.assertEqual(call["metadata_extra"]["run_id"], "run-42")

    def test_records_to_errors_room_on_failure(self):
        palace = _FakePalace()
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            memory_hooks.record_workspace_change(
                workspace_id="JKX",
                tool_name="commit_workspace_changes",
                args_summary="commit_message=...",
                result_summary="git error",
                repo_root=Path("/tmp"),
                success=False,
            )
        call = palace.add_calls[0]
        self.assertEqual(call["room"], "errors")
        self.assertEqual(call["event_type"], "error")

    def test_skips_when_workspace_id_empty(self):
        palace = _FakePalace()
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            memory_hooks.record_workspace_change(
                workspace_id="",
                tool_name="update_workspace_seed",
                args_summary="x",
                result_summary="y",
                repo_root=Path("/tmp"),
            )
        self.assertEqual(palace.add_calls, [])

    def test_skips_silently_when_palace_unavailable(self):
        with patch.object(memory_hooks, "_safe_palace", return_value=None):
            memory_hooks.record_workspace_change(
                workspace_id="JKX",
                tool_name="update_workspace_seed",
                args_summary="x",
                result_summary="y",
                repo_root=Path("/tmp"),
            )
        # No exception is the assertion.


class TestMirrorSubtaskToMemory(unittest.TestCase):
    def test_mirror_subtask_writes_to_palace(self):
        """mirror_subtask_to_memory writes to palace.subtask via MemPalace (non-fatal on failure)."""
        plan = SimpleNamespace(
            task_id="task1", run_id="run1", cursor=0, revisions=0, subtasks=[object()]
        )
        subtask = SimpleNamespace(
            id="st-1",
            status="done",
            title="Implement CLI",
            description="Build the command",
            success_check="CLI works",
            summary="Done",
            evidence=["manual note: looks good"],
        )
        palace_calls = []

        with patch("umbrella.memory.palace.facade.MemPalace") as mock_palace_cls:
            mock_palace = mock_palace_cls.return_value
            mock_palace.add.side_effect = lambda **kw: palace_calls.append(kw)
            memory_hooks.mirror_subtask_to_memory(
                plan=plan,
                subtask=subtask,
                repo_root=Path("/tmp/repo"),
                workspace_id="JKX",
            )

        self.assertEqual(len(palace_calls), 1)
        call = palace_calls[0]
        self.assertEqual(call["store"], "palace.subtask")
        self.assertEqual(call["scope"], "subtask_scoped")
        self.assertEqual(call["subtask_id"], "st-1")
        self.assertEqual(call["run_id"], "run1")
        self.assertEqual(call["extra"]["task_id"], "task1")
        self.assertNotIn("workspace_id", call)

    def test_mirror_subtask_does_not_raise_on_palace_error(self):
        """mirror_subtask_to_memory is non-fatal when MemPalace raises."""
        plan = SimpleNamespace(task_id="t", cursor=0, revisions=0, subtasks=[object()])
        subtask = SimpleNamespace(id="st-2", title="T", description="D", evidence=[])
        with patch("umbrella.memory.palace.facade.MemPalace", side_effect=Exception("unavailable")):
            memory_hooks.mirror_subtask_to_memory(
                plan=plan,
                subtask=subtask,
                repo_root=Path("/tmp/repo"),
                workspace_id="ws",
            )


class TestObserveToolCalls(unittest.TestCase):
    def test_auto_records_failed_write_as_error(self):
        calls: list[dict] = []

        def _record(**kwargs):
            calls.append(kwargs)

        tool_calls = [
            {
                "function": {
                    "name": "update_workspace_seed",
                    "arguments": json.dumps(
                        {
                            "workspace_id": "JKX",
                            "file_path": "app.py",
                        }
                    ),
                },
            }
        ]
        recent_results = [
            {
                "tool": "update_workspace_seed",
                "result": "⚠️ TOOL_PREFLIGHT_ERROR: bad args",
                "is_error": True,
            }
        ]
        verify_gate = SimpleNamespace(observe=lambda *_a, **_kw: None)

        with patch.object(memory_hooks, "record_workspace_change", side_effect=_record):
            new_ws = memory_hooks.observe_tool_calls(
                tool_calls=tool_calls,
                recent_tool_results=recent_results,
                write_tool_names=frozenset({"update_workspace_seed"}),
                verify_gate=verify_gate,
                repo_root=Path("/tmp"),
                current_workspace_id="",
            )

        self.assertEqual(new_ws, "JKX")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["success"])
        self.assertIn("TOOL_PREFLIGHT_ERROR", calls[0]["result_summary"])


class TestRecordVerifyOutcome(unittest.TestCase):
    def test_writes_to_verify_runs_room(self):
        palace = _FakePalace()
        with patch.object(memory_hooks, "_safe_palace", return_value=palace):
            memory_hooks.record_verify_outcome(
                workspace_id="JKX",
                passed=False,
                pass_rate=0.4,
                summary="2/5 steps passed",
                details="step1: PASS\nstep2: FAIL because X",
                repo_root=Path("/tmp"),
            )
        call = palace.add_calls[0]
        self.assertEqual(call["room"], "verify_runs")
        self.assertEqual(call["event_type"], "test")
        self.assertIn("verify_runs", call["tags"])
        self.assertIn("fail", call["tags"])
        self.assertIn("FAIL", call["title"])
        self.assertIn("40", call["title"])  # 0.4 -> "40.0%"


if __name__ == "__main__":
    unittest.main()
