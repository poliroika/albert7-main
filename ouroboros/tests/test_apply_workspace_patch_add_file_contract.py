import json
from pathlib import Path

import pytest

from ouroboros.tools.umbrella_tools import apply_workspace_patch


class _FakeCtx:
    def __init__(self, repo_root: Path, drive_root: Path) -> None:
        self.repo_dir = repo_root
        self.host_repo_root = repo_root
        self.drive_root = drive_root
        self.loop_state_view = {}


def _make_workspace(tmp_path: Path, workspace_id: str = "demo_ws") -> Path:
    (tmp_path / "umbrella").mkdir()
    workspace = tmp_path / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "TASK_MAIN.md").write_text("# task\n", encoding="utf-8")
    return workspace


@pytest.mark.parametrize("marker_line", ["@@", "+@@"])
def test_apply_workspace_patch_rejects_add_file_literal_hunk_marker(
    tmp_path: Path,
    marker_line: str,
) -> None:
    workspace = _make_workspace(tmp_path)
    ctx = _FakeCtx(tmp_path, tmp_path / ".umbrella" / "drive")

    raw = apply_workspace_patch(
        ctx,
        workspace_id="demo_ws",
        patch=(
            "*** Begin Patch\n"
            "*** Add File: docs/architecture.md\n"
            f"{marker_line}\n"
            "+# Architecture\n"
            "+\n"
            "+Generated project notes.\n"
            "*** End Patch\n"
        ),
    )

    payload = json.loads(raw)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "patch_add_file_literal_hunk_marker"
    assert payload["file_path"] == "docs/architecture.md"
    assert payload["line_numbers"] == [1]
    assert "without the `@@` line" in payload["next_step"]
    assert not (workspace / "docs" / "architecture.md").exists()
