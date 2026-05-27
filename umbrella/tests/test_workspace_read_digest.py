from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from umbrella.deep_agent_tools.workspace_ops import _stale_read_before_patch_block
from umbrella.deep_agent_tools.workspace_read import (
    _read_cache_clear,
    read_workspace_file,
)


def test_line_read_marks_full_file_digest_for_patch_freshness(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "demo"
    target = workspace / "src" / "demo.py"
    target.parent.mkdir(parents=True)
    target.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")
    ctx = SimpleNamespace(
        host_repo_root=tmp_path,
        repo_dir=tmp_path,
        workspace_root_overrides={"demo": str(workspace)},
    )

    payload = json.loads(
        read_workspace_file(
            ctx,
            "demo",
            "src/demo.py",
            line_start=2,
            line_count=1,
        )
    )

    full_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    assert payload["content_sha256"] == full_sha
    assert payload["preview_sha256"] != full_sha
    marker = ctx.loop_state_view["file_read_digests"]["demo"]["src/demo.py"]
    assert marker["sha256"] == full_sha
    assert _stale_read_before_patch_block(ctx, "demo", "src/demo.py", target) is None


def test_cached_line_read_preserves_full_file_digest(tmp_path: Path) -> None:
    _read_cache_clear()
    workspace = tmp_path / "workspaces" / "demo"
    target = workspace / "src" / "demo.py"
    target.parent.mkdir(parents=True)
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    ctx = SimpleNamespace(
        host_repo_root=tmp_path,
        repo_dir=tmp_path,
        workspace_root_overrides={"demo": str(workspace)},
    )

    first = json.loads(
        read_workspace_file(ctx, "demo", "src/demo.py", line_start=2, line_count=1)
    )
    ctx.loop_state_view = {}
    second = json.loads(
        read_workspace_file(ctx, "demo", "src/demo.py", line_start=2, line_count=1)
    )

    full_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    assert first["content_sha256"] == second["content_sha256"] == full_sha
    marker = ctx.loop_state_view["file_read_digests"]["demo"]["src/demo.py"]
    assert marker["sha256"] == full_sha
    assert _stale_read_before_patch_block(ctx, "demo", "src/demo.py", target) is None
