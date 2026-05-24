import json
import time

from umbrella.orchestrator.watcher_triggers import WatcherTriggers


def test_repeat_structural_layout_trigger(tmp_path) -> None:
    drive = tmp_path / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    tools = logs / "tools.jsonl"
    row = {
        "tool": "apply_workspace_patch",
        "output": {
            "status": "blocked",
            "reason": "greenfield_python_src_layout_policy",
            "file_path": "backend/src/app.py",
        },
    }
    with tools.open("a", encoding="utf-8") as handle:
        for _ in range(4):
            handle.write(json.dumps(row) + "\n")
    triggers = WatcherTriggers(drive, repeat_m=3)
    event = triggers.check(phase="execute", phase_started_at=time.time() - 5)
    assert event is not None
    assert event.kind == "repeat_structural_layout"
    assert event.context["file_path"] == "backend/src/app.py"


def test_repeat_semantic_failure_trigger_reads_result_preview(tmp_path) -> None:
    drive = tmp_path / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    tools = logs / "tools.jsonl"
    row = {
        "tool": "mark_subtask_complete",
        "args": {"completion_contract": {"subtask_id": "game-state"}},
        "result_preview": (
            "ERROR: mark_subtask_complete contract rejected: "
            "workspace_hash_mismatch: Verification report was produced for "
            "a different workspace hash."
        ),
    }
    with tools.open("a", encoding="utf-8") as handle:
        for _ in range(3):
            handle.write(json.dumps(row) + "\n")
    triggers = WatcherTriggers(drive, repeat_m=3)
    event = triggers.check(phase="execute", phase_started_at=time.time() - 5)
    assert event is not None
    assert event.kind == "repeat_semantic_failure"
    assert event.context["category"] == "completion_hash_mismatch"


def test_repeat_research_memory_error_trigger_reads_result_preview(tmp_path) -> None:
    drive = tmp_path / "drive"
    logs = drive / "logs"
    logs.mkdir(parents=True)
    tools = logs / "tools.jsonl"
    row = {
        "tool": "palace_add",
        "result_preview": (
            "ERROR: palace_add research_finding requires a source_id tied "
            "to current research-phase evidence."
        ),
    }
    with tools.open("a", encoding="utf-8") as handle:
        for _ in range(3):
            handle.write(json.dumps(row) + "\n")
    triggers = WatcherTriggers(drive, repeat_m=3)
    event = triggers.check(phase="research", phase_started_at=time.time() - 5)
    assert event is not None
    assert event.kind == "repeat_semantic_failure"
    assert event.context["category"] == "research_memory_provenance_error"
