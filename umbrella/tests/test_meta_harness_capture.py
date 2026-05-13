"""Tests for Meta-Harness candidate capture."""

import pytest

from umbrella.meta_harness.capture import capture_ouroboros_candidate
from umbrella.meta_harness.store import MetaHarnessStore


@pytest.fixture
def fake_repo(tmp_path):
    (tmp_path / "umbrella" / "prompts").mkdir(parents=True)
    (tmp_path / "umbrella" / "prompts" / "ouroboros_workspace_task.md").write_text(
        "# Test prompt",
        encoding="utf-8",
    )
    (tmp_path / "umbrella" / "policies").mkdir(parents=True)
    (tmp_path / "umbrella" / "policies" / "default_policy.yaml").write_text(
        "runtime:\n  max_iterations: 10\n",
        encoding="utf-8",
    )
    (tmp_path / ".umbrella" / "meta_harness").mkdir(parents=True)
    (tmp_path / ".umbrella" / "ouroboros_drive" / "memory" / "knowledge").mkdir(
        parents=True
    )
    (
        tmp_path / ".umbrella" / "ouroboros_drive" / "memory" / "knowledge" / "test.md"
    ).write_text(
        "# Test knowledge",
        encoding="utf-8",
    )
    return tmp_path


class TestCaptureCandidate:
    def test_basic_capture(self, fake_repo):
        manifest = capture_ouroboros_candidate(
            repo_root=fake_repo,
            task_id="test_task",
            workspace_id="ws1",
            task_description="Test task description",
            run_status="complete",
            llm_tool_invocations=5,
            workspace_write_tool_calls=2,
            final_message="Done!",
        )
        assert manifest.candidate_id.startswith("cand_")
        assert manifest.task_id == "test_task"
        assert manifest.workspace_id == "ws1"
        assert manifest.run_status == "complete"
        assert manifest.tool_calls == 5
        assert manifest.write_calls == 2

    def test_capture_creates_store_entry(self, fake_repo):
        manifest = capture_ouroboros_candidate(
            repo_root=fake_repo,
            task_id="test_task",
            workspace_id="ws1",
        )
        store = MetaHarnessStore(fake_repo / ".umbrella" / "meta_harness")
        found = store.find_candidate(manifest.candidate_id)
        assert found is not None
        assert found.candidate_id == manifest.candidate_id

    def test_capture_with_events(self, fake_repo):
        events = [
            {"type": "tool_call", "tool": "read_file"},
            {"type": "tool_call", "tool": "write_file"},
        ]
        manifest = capture_ouroboros_candidate(
            repo_root=fake_repo,
            task_id="test_task",
            workspace_id="ws1",
            events=events,
        )
        store = MetaHarnessStore(fake_repo / ".umbrella" / "meta_harness")
        loaded_events = store.get_execution_events(manifest.candidate_id)
        assert len(loaded_events) == 2

    def test_capture_with_changes(self, fake_repo):
        manifest = capture_ouroboros_candidate(
            repo_root=fake_repo,
            task_id="test_task",
            workspace_id="ws1",
            changes_made=["file1.py", "file2.py"],
            promoted_files=["file1.py"],
        )
        assert manifest.changed_files == ["file1.py", "file2.py"]
        assert manifest.promoted_files == ["file1.py"]

    def test_capture_error_run(self, fake_repo):
        manifest = capture_ouroboros_candidate(
            repo_root=fake_repo,
            task_id="test_task",
            workspace_id="ws1",
            run_status="error",
            error="Something went wrong",
        )
        assert manifest.run_status == "error"
        assert "Something went wrong" in manifest.error

    def test_prompt_snapshot_saved(self, fake_repo):
        manifest = capture_ouroboros_candidate(
            repo_root=fake_repo,
            task_id="test_task",
            workspace_id="ws1",
        )
        store = MetaHarnessStore(fake_repo / ".umbrella" / "meta_harness")
        cand_dir = store.find_candidate_dir(manifest.candidate_id)
        prompt_file = cand_dir / "prompt_snapshot" / "ouroboros_workspace_task.md"
        assert prompt_file.exists()
        assert "Test prompt" in prompt_file.read_text(encoding="utf-8")

    def test_policy_snapshot_saved(self, fake_repo):
        manifest = capture_ouroboros_candidate(
            repo_root=fake_repo,
            task_id="test_task",
            workspace_id="ws1",
        )
        store = MetaHarnessStore(fake_repo / ".umbrella" / "meta_harness")
        cand_dir = store.find_candidate_dir(manifest.candidate_id)
        policy_file = cand_dir / "policy_snapshot" / "default_policy.yaml"
        assert policy_file.exists()

    def test_memory_input_saved(self, fake_repo):
        manifest = capture_ouroboros_candidate(
            repo_root=fake_repo,
            task_id="test_task",
            workspace_id="ws1",
        )
        store = MetaHarnessStore(fake_repo / ".umbrella" / "meta_harness")
        cand_dir = store.find_candidate_dir(manifest.candidate_id)
        memory_file = cand_dir / "memory_input" / "test.md"
        assert memory_file.exists()


class TestSafeWorktreeDiffTruncation:
    def test_oversized_diff_marked_unsafe_for_apply(self, fake_repo, monkeypatch):
        from umbrella.meta_harness import capture as capture_mod

        big_diff = "x" * (capture_mod._DIFF_SIZE_LIMIT + 5_000)

        class _R:
            returncode = 0
            stdout = big_diff
            stderr = ""

        monkeypatch.setattr(capture_mod.subprocess, "run", lambda *a, **kw: _R())

        diff = capture_mod._safe_worktree_diff(fake_repo)

        assert capture_mod._DIFF_TRUNCATED_MARKER in diff
        assert len(diff) <= capture_mod._DIFF_SIZE_LIMIT + 200
