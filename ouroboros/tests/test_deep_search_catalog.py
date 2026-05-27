import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from ouroboros.tools import deep_search as ds


@dataclass
class _Ctx:
    repo_dir: Path
    drive_root: Path
    host_repo_root: Path
    task_id: str = "t1"
    pending_events: list = field(default_factory=list)


def test_deep_search_slim_results(tmp_path: Path) -> None:
    ds.reset_budget_for_task("t1")
    repo = tmp_path / "ws"
    drive = repo / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = _Ctx(
        repo_dir=repo,
        drive_root=drive,
        host_repo_root=tmp_path,
    )
    fake_results = [
        {
            "title": "Doc",
            "url": "https://example.com/a",
            "snippet": "short",
            "content": "x" * 5000,
        }
    ]
    payload = {
        "status": "ok",
        "provider": "test",
        "results": fake_results,
        "answer": "y" * 8000,
        "sources": [],
        "attempts": [],
    }
    with patch.object(ds, "_enabled", return_value=True), patch.object(
        ds, "_gmas_search", return_value=payload
    ):
        out = json.loads(
            ds._deep_search(ctx, query="api design", intent="planner_research")
        )
    assert out["status"] == "ok"
    assert "catalog_ids" in out
    assert "content" not in (out["results"][0] or {})
    assert len(out.get("answer_preview", "")) <= 400
