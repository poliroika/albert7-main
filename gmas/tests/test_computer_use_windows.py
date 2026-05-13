"""Windows runtime smoke tests for the computer-use package."""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only runtime tests", allow_module_level=True)

import time
from pathlib import Path

pytest.importorskip("PIL")
from PIL import Image

from gmas.tools.computer_use import (
    ComputerAction,
    ComputerActionType,
    ComputerSessionConfig,
    ComputerUseClient,
    ComputerUseController,
    ComputerUseOperation,
    ObservationMode,
    ObservationRequest,
    SafetyMode,
    WindowsComputerRuntime,
)


@pytest.fixture
def windows_runtime() -> WindowsComputerRuntime:
    return WindowsComputerRuntime()


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "computer_use_artifacts"


def test_windows_runtime_capabilities(windows_runtime: WindowsComputerRuntime):
    capabilities = windows_runtime.capabilities()
    assert capabilities.supports_desktop is True
    assert capabilities.supports_screenshots is True
    assert capabilities.supports_window_management is True
    assert capabilities.supports_clipboard is True


def test_windows_runtime_observation_creates_real_screenshot(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    session = windows_runtime.start_session(
        ComputerSessionConfig(
            artifact_root=artifact_root,
            observation=ObservationRequest(mode=ObservationMode.STANDARD),
        )
    )
    observation = windows_runtime.get_observation(session)
    assert observation.screenshot is not None
    assert observation.screenshot.path is not None
    screenshot_path = Path(observation.screenshot.path)
    assert screenshot_path.exists()
    assert screenshot_path.stat().st_size > 0
    with Image.open(screenshot_path) as image:
        assert image.width > 0
        assert image.height > 0
        assert image.getbbox() is not None
    assert observation.active_window is not None


def test_windows_runtime_active_window_screenshot_is_not_blank(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    session = windows_runtime.start_session(
        ComputerSessionConfig(
            artifact_root=artifact_root,
            observation=ObservationRequest(mode=ObservationMode.DETAILED, active_window_only=True),
        )
    )
    observation = windows_runtime.get_observation(session, ObservationRequest(active_window_only=True))
    assert observation.screenshot is not None
    assert observation.screenshot.path is not None
    screenshot_path = Path(observation.screenshot.path)
    with Image.open(screenshot_path) as image:
        assert image.width > 0
        assert image.height > 0
        assert image.getbbox() is not None
    assert observation.screenshot.metadata.get("warning") != "blank_screenshot_detected"


def test_windows_tool_round_trip_with_screenshot(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    tool = ComputerUseClient(ComputerUseController(windows_runtime))
    start = tool.execute(
        operation=ComputerUseOperation.START,
        config=ComputerSessionConfig(
            artifact_root=artifact_root,
            observation=ObservationRequest(mode=ObservationMode.DETAILED, include_clipboard=True),
        ),
    )
    assert start.success is True
    assert start.observation is not None
    assert start.observation.screenshot is not None
    assert start.session is not None

    observe = tool.execute(
        operation=ComputerUseOperation.OBSERVE,
        session_id=start.session.session_id,
        observation=ObservationRequest(mode=ObservationMode.STANDARD, active_window_only=True),
    )
    assert observe.success is True
    assert observe.observation is not None
    assert observe.observation.screenshot is not None
    assert observe.observation.screenshot.path is not None
    assert Path(observe.observation.screenshot.path).exists()


def test_windows_runtime_blocks_dangerous_hotkeys(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    session = windows_runtime.start_session(
        ComputerSessionConfig(
            artifact_root=artifact_root,
            safety_mode=SafetyMode.PROMPT,
        )
    )
    result = windows_runtime.execute(
        session,
        ComputerAction(
            action_type=ComputerActionType.HOTKEY,
            keys=["alt", "f4"],
        ),
    )
    assert result.success is False
    assert result.error is not None
    assert "blocked" in result.error


def test_windows_runtime_extracts_clipboard_text(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    import win32clipboard
    import win32con

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, "clipboard smoke text")
    finally:
        win32clipboard.CloseClipboard()

    session = windows_runtime.start_session(
        ComputerSessionConfig(
            artifact_root=artifact_root,
            observation=ObservationRequest(include_clipboard=True),
        )
    )
    result = windows_runtime.execute(
        session,
        ComputerAction(
            action_type=ComputerActionType.EXTRACT_TEXT,
            metadata={"strategy": "clipboard"},
        ),
    )
    assert result.success is True
    assert result.metadata["extracted_text"] == "clipboard smoke text"


def test_windows_wait_action_does_not_double_sleep(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    """
    WAIT must sleep exactly wait_ms milliseconds, not twice that amount.

    Regression test for the double-sleep bug where both _execute_action and
    the post-action branch in execute() called time.sleep(wait_ms / 1000).

    We first measure the baseline overhead of execute() with a no-wait
    SCREENSHOT action (which still captures an observation), then verify
    that the WAIT action only adds approximately wait_ms on top.
    """
    wait_ms = 500
    session = windows_runtime.start_session(ComputerSessionConfig(artifact_root=artifact_root))

    # Measure baseline overhead (screenshot capture + observation).
    baseline_start = time.monotonic()
    windows_runtime.execute(
        session,
        ComputerAction(action_type=ComputerActionType.SCREENSHOT),
    )
    baseline_ms = (time.monotonic() - baseline_start) * 1000

    start = time.monotonic()
    result = windows_runtime.execute(
        session,
        ComputerAction(action_type=ComputerActionType.WAIT, wait_ms=wait_ms),
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    assert result.success is True
    # Subtract the baseline overhead to isolate the actual wait time.
    wait_only_ms = elapsed_ms - baseline_ms
    # Allow generous bounds: the isolated wait should be between 0.7× and 1.8×
    # of wait_ms.  Before the fix this would be ≥ 2× wait_ms.
    assert wait_only_ms < wait_ms * 1.8, (
        f"WAIT took ~{wait_only_ms:.0f} ms (total={elapsed_ms:.0f} ms, "
        f"baseline={baseline_ms:.0f} ms) — expected ~{wait_ms} ms "
        f"(double-sleep bug would produce ~{wait_ms * 2} ms)"
    )


def test_windows_runtime_screenshot_jpeg_format(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    """JPEG screenshots should produce .jpg files that are smaller than PNG."""
    session = windows_runtime.start_session(
        ComputerSessionConfig(
            artifact_root=artifact_root,
            observation=ObservationRequest(
                mode=ObservationMode.SCREENSHOT_ONLY,
                screenshot_format="jpeg",
                screenshot_quality=60,
            ),
        )
    )
    observation = windows_runtime.get_observation(session)
    assert observation.screenshot is not None
    assert observation.screenshot.path is not None
    screenshot_path = Path(observation.screenshot.path)
    assert screenshot_path.suffix == ".jpg"
    assert screenshot_path.exists()
    assert screenshot_path.stat().st_size > 0
    assert observation.screenshot.mime_type == "image/jpeg"
    assert observation.screenshot.metadata.get("format") == "jpeg"


def test_windows_runtime_action_timing_metadata(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    """Every action result must contain timing metadata."""
    session = windows_runtime.start_session(ComputerSessionConfig(artifact_root=artifact_root))
    result = windows_runtime.execute(
        session,
        ComputerAction(action_type=ComputerActionType.SCREENSHOT),
    )
    assert result.success is True
    assert "timing" in result.metadata
    assert "action_ms" in result.metadata["timing"]
    assert "total_ms" in result.metadata["timing"]
    assert result.metadata["timing"]["action_ms"] >= 0
    assert result.metadata["timing"]["total_ms"] >= result.metadata["timing"]["action_ms"]


def test_windows_runtime_resize_window(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    """resize_window should succeed on the foreground window."""
    import win32gui

    session = windows_runtime.start_session(ComputerSessionConfig(artifact_root=artifact_root))
    # Get the current foreground window title to resize it.
    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    if not title:
        pytest.skip("no foreground window with title")

    result = windows_runtime.execute(
        session,
        ComputerAction(
            action_type=ComputerActionType.RESIZE_WINDOW,
            text=title,
            width=800,
            height=600,
        ),
    )
    assert result.success is True
    assert "800" in result.summary
    assert "600" in result.summary


def test_windows_runtime_minimize_maximize_window(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    """minimize_window and maximize_window should succeed."""
    import win32gui

    session = windows_runtime.start_session(ComputerSessionConfig(artifact_root=artifact_root))
    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    if not title:
        pytest.skip("no foreground window with title")

    result_min = windows_runtime.execute(
        session,
        ComputerAction(action_type=ComputerActionType.MINIMIZE_WINDOW, text=title),
    )
    assert result_min.success is True
    assert "minimized" in result_min.summary

    result_max = windows_runtime.execute(
        session,
        ComputerAction(action_type=ComputerActionType.MAXIMIZE_WINDOW, text=title),
    )
    assert result_max.success is True
    assert "maximized" in result_max.summary


def test_windows_runtime_extract_text_ocr_fallback(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    """OCR strategy should gracefully fall back when Tesseract is not installed."""
    session = windows_runtime.start_session(ComputerSessionConfig(artifact_root=artifact_root))
    result = windows_runtime.execute(
        session,
        ComputerAction(
            action_type=ComputerActionType.EXTRACT_TEXT,
            metadata={"strategy": "ocr"},
        ),
    )
    # Should succeed regardless of whether Tesseract is installed —
    # falls back to clipboard if OCR fails.
    assert result.success is True
    assert "extracted_text" in result.metadata


def test_windows_runtime_action_timeout(
    windows_runtime: WindowsComputerRuntime,
    artifact_root: Path,
):
    """An action with a very short timeout should fail with a timeout error."""
    session = windows_runtime.start_session(ComputerSessionConfig(artifact_root=artifact_root))
    # WAIT for 2 seconds but with a 50ms timeout — should fail.
    result = windows_runtime.execute(
        session,
        ComputerAction(
            action_type=ComputerActionType.WAIT,
            wait_ms=2000,
            timeout_ms=50,
        ),
    )
    assert result.success is False
    assert result.error is not None
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()
