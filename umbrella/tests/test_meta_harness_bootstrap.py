"""Tests for Meta-Harness environment snapshot."""

import json

import pytest

from umbrella.meta_harness.bootstrap import (
    gather_environment_snapshot,
    render_environment_snapshot_section,
)


@pytest.fixture
def fake_repo(tmp_path):
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces").mkdir()
    ws = tmp_path / "workspaces" / "test_ws"
    ws.mkdir()
    (ws / "TASK_MAIN.md").write_text("# Test task", encoding="utf-8")
    (ws / "workspace.toml").write_text("[workspace]\nname = 'test'", encoding="utf-8")
    return tmp_path


class TestGatherSnapshot:
    def test_basic_fields(self, fake_repo):
        snap = gather_environment_snapshot(fake_repo)
        assert "python_version" in snap
        assert "os" in snap
        assert "repo_root" in snap
        assert "git" in snap
        assert isinstance(snap["git"], dict)

    def test_workspace_fields(self, fake_repo):
        ws_path = fake_repo / "workspaces" / "test_ws"
        snap = gather_environment_snapshot(fake_repo, ws_path)
        assert snap["has_task_main"] is True
        assert snap["has_workspace_toml"] is True
        assert snap["has_seed_profile"] is False

    def test_no_workspace(self, fake_repo):
        snap = gather_environment_snapshot(fake_repo, None)
        assert snap["workspace_top_level"] == []
        assert snap["has_task_main"] is False

    def test_nonexistent_workspace(self, fake_repo):
        snap = gather_environment_snapshot(fake_repo, fake_repo / "workspaces" / "nope")
        assert snap["has_task_main"] is False

    def test_no_secrets_in_snapshot(self, fake_repo):
        (fake_repo / ".env").write_text(
            "SECRET_KEY=abc123\nAPI_KEY=xyz", encoding="utf-8"
        )
        snap = gather_environment_snapshot(fake_repo)
        snap_text = json.dumps(snap)
        assert "abc123" not in snap_text
        assert "xyz" not in snap_text

    def test_recent_failure_hints(self, fake_repo):
        signals_dir = fake_repo / ".umbrella" / "memory"
        signals_dir.mkdir(parents=True)
        signals = [
            json.dumps({"strength": -0.5, "evidence_summary": "Test failure hint"}),
            json.dumps({"strength": 0.5, "evidence_summary": "Positive signal"}),
        ]
        (signals_dir / "signals.jsonl").write_text("\n".join(signals), encoding="utf-8")

        snap = gather_environment_snapshot(fake_repo)
        assert len(snap["recent_failure_hints"]) == 1
        assert "Test failure hint" in snap["recent_failure_hints"][0]


class TestRenderSnapshot:
    def test_renders_markdown(self, fake_repo):
        ws_path = fake_repo / "workspaces" / "test_ws"
        result = render_environment_snapshot_section(fake_repo, ws_path)
        assert "Python:" in result
        assert "OS:" in result

    def test_fail_soft(self, tmp_path):
        result = render_environment_snapshot_section(tmp_path / "nonexistent")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_workspace_config_listed(self, fake_repo):
        ws_path = fake_repo / "workspaces" / "test_ws"
        result = render_environment_snapshot_section(fake_repo, ws_path)
        assert "TASK_MAIN.md" in result
