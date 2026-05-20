import json
import pathlib
import pytest
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.migrators import migrate_lessons, migrate_ideas, migrate_gaps_signals


@pytest.fixture
def palace(tmp_path):
    return MemPalace(repo_root=tmp_path, workspace_id="test_ws")


def test_migrate_lessons_verified(palace, tmp_path):
    jl = tmp_path / "lessons.jsonl"
    jl.write_text(json.dumps({"content": "lesson one", "tags": ["test"]}) + "\n")
    count = migrate_lessons(palace, jl)
    assert count == 1
    assert not jl.exists()
    assert (tmp_path / "lessons.jsonl.migrated").exists()


def test_migrate_ideas_verified_flag(palace, tmp_path):
    jl = tmp_path / "ideas.jsonl"
    jl.write_text(
        json.dumps({"content": "unverified idea", "evidence_kind": "hypothesis"}) + "\n"
        + json.dumps({"content": "verified idea", "evidence_kind": "verified_outcome"}) + "\n"
    )
    count = migrate_ideas(palace, jl)
    assert count == 2


def test_migrate_gaps(palace, tmp_path):
    jl = tmp_path / "gaps.jsonl"
    jl.write_text(json.dumps({"content": "gap in auth"}) + "\n")
    count = migrate_gaps_signals(palace, jl, tag="gap")
    assert count == 1


def test_empty_file_no_error(palace, tmp_path):
    jl = tmp_path / "nonexistent.jsonl"
    count = migrate_lessons(palace, jl)
    assert count == 0
