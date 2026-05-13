"""migrate_to_per_workspace splits legacy manager JSONL."""

import json

from umbrella.memory.migrations import migrate_to_per_workspace
from umbrella.memory.paths import manager_memory_root, workspace_memory_root


def test_migrate_lessons_idempotent(tmp_path) -> None:
    repo = tmp_path / "repo"
    mgr = manager_memory_root(repo)
    mgr.mkdir(parents=True)

    lessons = [
        {
            "lesson_type": "workspace",
            "id": "w1",
            "task_id": "t",
            "workspace_id": "ws_a",
            "change_summary": "a",
            "expected_effect": "e",
            "observed_effect": "o",
            "conclusion": "c",
            "evidence_summary": "ev",
            "tags": [],
        },
        {
            "lesson_type": "manager",
            "id": "m1",
            "task_id": "t",
            "workspace_id": "",
            "change_summary": "mgr",
            "expected_effect": "e",
            "observed_effect": "o",
            "conclusion": "c",
            "evidence_summary": "ev",
            "tags": [],
        },
    ]
    (mgr / "lessons.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lessons) + "\n",
        encoding="utf-8",
    )

    out = migrate_to_per_workspace(repo)
    assert any(s.get("migrated_lines") == 2 for s in out["streams"])

    assert (mgr / "lessons.legacy.jsonl").exists()
    mgr_lines = [
        json.loads(ln)
        for ln in (mgr / "lessons.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(mgr_lines) == 1
    assert mgr_lines[0]["lesson_type"] == "manager"

    ws_file = workspace_memory_root(repo, "ws_a") / "lessons.jsonl"
    assert ws_file.exists()
    ws_lines = [
        json.loads(ln)
        for ln in ws_file.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(ws_lines) == 1
    assert ws_lines[0]["workspace_id"] == "ws_a"

    out2 = migrate_to_per_workspace(repo)
    assert all(s.get("skipped") for s in out2["streams"] if s.get("name") == "lessons")
