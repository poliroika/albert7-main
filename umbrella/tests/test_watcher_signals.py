import json
import pathlib
import time
import pytest
from umbrella.phases.base import WatcherSignal
from umbrella.orchestrator.watcher import WatcherPollLoop


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
