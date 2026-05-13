from pathlib import Path

from umbrella.meta_harness.capture import _reconcile_artifacts


def test_reconcile_artifacts_reports_missing_declared_file(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("ok", encoding="utf-8")

    payload = _reconcile_artifacts(
        repo_root=tmp_path,
        instance_path=None,
        changed_files=["real.txt", "missing.txt"],
        promoted_files=[],
        events=[
            {
                "type": "tool_call",
                "tool": "update_workspace_seed",
            }
        ],
    )

    assert payload["status"] == "artifact_mismatch"
    assert payload["missing_changed_files"] == ["missing.txt"]
    assert payload["tool_write_event_count"] == 1
