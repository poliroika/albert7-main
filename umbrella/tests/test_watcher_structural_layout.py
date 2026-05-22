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
