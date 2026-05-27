import json
import pathlib
import time
import pytest
from umbrella.phases.base import WatcherSignal
from umbrella.orchestrator.watcher import WatcherPollLoop
from umbrella.orchestrator.watcher_semantic import accepted_research_finding_ids
from umbrella.orchestrator.watcher_triggers import TriggerEvent


@pytest.fixture
def tmp_drive(tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "state").mkdir()
    (drive / "logs").mkdir()
    return drive


def test_write_and_read_signal(tmp_drive):
    watcher = WatcherPollLoop(tmp_drive)
    sig = WatcherSignal(
        signal_id="test-123",
        created_at=time.time(),
        kind="abort_phase",
        reason="stall detected",
        trigger="stall",
        payload=None,
    )
    watcher.write_signal(sig)
    pending = watcher.read_pending_signal()
    assert pending is not None
    assert pending.signal_id == "test-123"
    assert pending.kind == "abort_phase"


def test_mark_processed_deduplicates(tmp_drive):
    watcher = WatcherPollLoop(tmp_drive)
    sig = WatcherSignal(
        signal_id="test-456",
        created_at=time.time(),
        kind="restart_phase",
        reason="repeat error",
        trigger="repeat_error",
        payload=None,
    )
    watcher.write_signal(sig)
    watcher.mark_processed("test-456")
    pending = watcher.read_pending_signal()
    assert pending is None


def test_no_signal_when_no_file(tmp_drive):
    watcher = WatcherPollLoop(tmp_drive)
    assert watcher.read_pending_signal() is None


def test_parse_watcher_response_accepts_kind_field(tmp_drive):
    watcher = WatcherPollLoop(tmp_drive)
    kind, reason = watcher._parse_watcher_response(
        '{"kind": "force_verify", "reason": "fresh proof required"}',
        TriggerEvent("stall", {}),
    )
    assert kind == "force_verify"
    assert reason == "fresh proof required"


def test_tick_no_trigger(tmp_drive):
    watcher = WatcherPollLoop(tmp_drive, poll_sec=5)
    result = watcher.tick(phase="research", phase_started_at=time.time())
    assert result is None


def test_stall_detection(tmp_drive):
    from umbrella.orchestrator.watcher_triggers import WatcherTriggers
    triggers = WatcherTriggers(tmp_drive, stall_sec=1)
    time.sleep(0.1)
    triggers._last_event_time = time.time() - 2
    ev = triggers.check(phase="execute", phase_started_at=time.time() - 10)
    assert ev is not None
    assert ev.kind == "stall"


def test_repeat_semantic_failure_injects_lesson_at_default_threshold(tmp_drive, monkeypatch):
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_INJECT_M", "3")
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_RESTART_M", "15")
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_ABORT_M", "30")
    tools = tmp_drive / "logs" / "tools.jsonl"
    row = {
        "tool": "palace_add",
        "result_preview": (
            "ERROR: palace_add research_finding requires a source_id tied "
            "to current research-phase evidence."
        ),
    }
    tools.write_text(
        "\n".join(json.dumps(row) for _ in range(3)) + "\n",
        encoding="utf-8",
    )
    watcher = WatcherPollLoop(tmp_drive, poll_sec=1)

    signal = watcher.tick(phase="research", phase_started_at=time.time() - 5)

    assert signal is not None
    assert signal.kind == "inject_lesson"
    assert signal.trigger == "repeat_semantic_failure"
    assert "research_memory_provenance_error" in str(signal.payload.get("watcher_semantic_category") or "")
    assert str(signal.payload.get("watcher_lesson") or "").strip()
    assert watcher.read_pending_signal() is not None


def test_accepted_research_finding_ids_ignores_observations(tmp_drive):
    tools = tmp_drive / "logs" / "tools.jsonl"
    rows = [
        {
            "tool": "palace_add",
            "args": {"kind": "observation"},
            "result_preview": json.dumps(
                {"saved": True, "id": "observation-id", "kind": "observation"}
            ),
        },
        {
            "tool": "palace_add",
            "args": {"kind": "research_finding"},
            "result_preview": json.dumps(
                {
                    "saved": True,
                    "id": "finding-id",
                    "kind": "research_finding",
                }
            ),
        },
    ]
    tools.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    assert accepted_research_finding_ids(tools) == ["finding-id"]


def test_repeat_semantic_failure_aborts_at_configured_ceiling(tmp_drive, monkeypatch):
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_INJECT_M", "3")
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_RESTART_M", "15")
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_ABORT_M", "30")
    tools = tmp_drive / "logs" / "tools.jsonl"
    row = {
        "tool": "palace_add",
        "result_preview": (
            "ERROR: palace_add research_finding requires a source_id tied "
            "to current research-phase evidence."
        ),
    }
    tools.write_text(
        "\n".join(json.dumps(row) for _ in range(30)) + "\n",
        encoding="utf-8",
    )
    watcher = WatcherPollLoop(tmp_drive, poll_sec=1)
    watcher._semantic_emit_keys.clear()

    signal = watcher.tick(phase="research", phase_started_at=time.time() - 5)

    assert signal is not None
    assert signal.kind == "abort_phase"
    assert signal.trigger == "repeat_semantic_failure"


@pytest.mark.parametrize(
    ("category", "expected_kind"),
    [
        ("proof_not_passing", "restart_phase"),
        ("proof_runtime_import_error", "restart_phase"),
        ("completion_contract_invalid", "inject_lesson"),
        ("fake_evidence_ref", "abort_phase"),
    ],
)
def test_repeat_semantic_failure_category_mapping(
    tmp_drive, category: str, expected_kind: str, monkeypatch
) -> None:
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_INJECT_M", "3")
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_RESTART_M", "15")
    monkeypatch.setenv("OUROBOROS_WATCHER_SEMANTIC_ABORT_M", "30")
    tools = tmp_drive / "logs" / "tools.jsonl"
    row = {
        "tool": "run_subtask_proof",
        "result_preview": json.dumps({"passed": False, "category": category}),
    }
    if category == "completion_contract_invalid":
        row = {
            "tool": "mark_subtask_complete",
            "result_preview": (
                "ERROR: mark_subtask_complete contract rejected: "
                "missing completion_contract"
            ),
        }
    elif category == "fake_evidence_ref":
        row = {
            "tool": "mark_subtask_complete",
            "result_preview": "ERROR: fake_evidence_ref in completion contract",
        }
    elif category.startswith("proof_runtime_"):
        row = {
            "tool": "run_subtask_proof",
            "result_preview": json.dumps(
                {
                    "passed": False,
                    "shell_result": {"stderr": "ModuleNotFoundError: demo"},
                }
            ),
        }
    repeat_count = 30 if expected_kind == "abort_phase" else 15 if expected_kind == "restart_phase" else 3
    tools.write_text(
        "\n".join(json.dumps(row) for _ in range(repeat_count)) + "\n",
        encoding="utf-8",
    )
    watcher = WatcherPollLoop(tmp_drive, poll_sec=1)
    watcher._semantic_emit_keys.clear()
    signal = watcher.tick(phase="execute", phase_started_at=time.time() - 5)
    assert signal is not None
    assert signal.kind == expected_kind
    assert signal.trigger == "repeat_semantic_failure"
