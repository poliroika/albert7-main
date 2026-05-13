"""PalaceBackend mirrors selected events into HierarchicalMemory (ideas.jsonl)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from umbrella.memory.hierarchical import HierarchicalMemory
from umbrella.memory.palace_backend import PalaceBackend
from umbrella.memory.paths import hierarchical_root_for_palace


@pytest.fixture
def palace_dir(tmp_path: Path) -> Path:
    p = tmp_path / "palace"
    p.mkdir()
    return p


def test_palace_add_mirrors_idea_to_hierarchical(palace_dir: Path, monkeypatch) -> None:
    col = MagicMock()
    col.upsert = MagicMock()

    pb = PalaceBackend(palace_dir)

    def _fake_collection():
        return col

    monkeypatch.setattr(pb, "_get_collection", _fake_collection)

    pb.add(
        workspace_id="demo",
        event_type="idea",
        room="gmas",
        title="Test idea",
        content="hello",
        kind="info",
        tags=["x"],
    )

    root = hierarchical_root_for_palace(pb.palace_path)
    hm = HierarchicalMemory(root)
    rows = hm.read_all()
    assert len(rows) == 1
    assert rows[0].title == "Test idea"
    assert "ideas" in rows[0].palace_path
