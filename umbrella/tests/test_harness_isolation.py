"""Harness candidate workspace isolation helpers."""

from pathlib import Path

from umbrella.control_plane.ouroboros_integration import (
    _collect_candidate_workspace_changes,
    _prepare_candidate_workspace,
)
from umbrella.meta_harness.models import CandidateManifest
from umbrella.meta_harness.promotion import _apply_candidate_workspace_files


def test_candidate_workspace_changes_are_collected_without_touching_live_seed(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    live = repo / "workspaces" / "ws"
    live.mkdir(parents=True)
    (live / "a.txt").write_text("live\n", encoding="utf-8")
    (live / ".memory" / "drive" / "logs").mkdir(parents=True)
    (live / ".memory" / "drive" / "logs" / "events.jsonl").write_text(
        "live log\n", encoding="utf-8"
    )

    candidate = _prepare_candidate_workspace(repo, "ws", "harness_web_x__s1__c1")
    assert candidate is not None

    (candidate / "a.txt").write_text("candidate\n", encoding="utf-8")
    (candidate / "b.txt").write_text("new\n", encoding="utf-8")
    (candidate / ".memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    (candidate / ".memory" / "knowledge" / "note.md").write_text(
        "note\n", encoding="utf-8"
    )
    (candidate / ".memory" / "drive" / "logs" / "events.jsonl").write_text(
        "candidate log\n", encoding="utf-8"
    )

    changes = _collect_candidate_workspace_changes(repo, "ws", candidate)

    assert "workspaces/ws/a.txt" in changes
    assert "workspaces/ws/b.txt" in changes
    assert "workspaces/ws/.memory/knowledge/note.md" in changes
    assert "workspaces/ws/.memory/drive/logs/events.jsonl" not in changes
    assert (live / "a.txt").read_text(encoding="utf-8") == "live\n"


def test_apply_candidate_workspace_files_copies_only_changed_files(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    live = repo / "workspaces" / "ws"
    live.mkdir(parents=True)
    (live / "a.txt").write_text("live\n", encoding="utf-8")
    candidate = repo / ".umbrella" / "meta_harness" / "workspaces" / "cand"
    candidate.mkdir(parents=True)
    (candidate / "a.txt").write_text("candidate\n", encoding="utf-8")
    (candidate / "nested").mkdir()
    (candidate / "nested" / "b.txt").write_text("new\n", encoding="utf-8")

    manifest = CandidateManifest(
        candidate_id="cand_test",
        workspace_id="ws",
        instance_path=str(candidate),
        changed_files=[
            "workspaces/ws/a.txt",
            "workspaces/ws/nested/b.txt",
        ],
    )

    assert _apply_candidate_workspace_files(repo, manifest) is True
    assert (live / "a.txt").read_text(encoding="utf-8") == "candidate\n"
    assert (live / "nested" / "b.txt").read_text(encoding="utf-8") == "new\n"
