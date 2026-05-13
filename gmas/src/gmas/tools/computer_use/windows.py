"""
Windows native computer-use runtime using Win32 and Pillow.

Provides full desktop automation on Windows: mouse, keyboard, screenshots,
window management, and clipboard access via pywin32 and Pillow.

Requirements:
    pip install pywin32 pillow  (or: pip install "frontier-ai-gmas[computer-use]")
"""

import concurrent.futures
import contextlib
import ctypes
import inspect
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import win32api
import win32con
import win32gui
from PIL import Image, ImageGrab

from gmas.config.logging import logger

from .models import (
    ComputerAction,
    ComputerActionResult,
    ComputerActionType,
    ComputerArtifact,
    ComputerBounds,
    ComputerCoordinate,
    ComputerObservation,
    ComputerRuntimeCapabilities,
    ComputerSession,
    ComputerSessionConfig,
    ComputerViewport,
    MouseButton,
    ObservationMode,
    ObservationRequest,
    SafetyMode,
    UIElementRef,
    WindowInfo,
)
from .runtime import ComputerRuntime

# ------------------------------------------------------------------
# Win32 constants and ctypes structures
# ------------------------------------------------------------------

user32 = ctypes.windll.user32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000  # horizontal scroll wheel
SW_RESTORE = 9
CF_UNICODETEXT = 13


class KEYBDINPUT(ctypes.Structure):
    _fields_: ClassVar[tuple[tuple[str, object], ...]] = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("dwExtraInfo", ctypes.c_ulonglong),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_: ClassVar[tuple[tuple[str, object], ...]] = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("dwExtraInfo", ctypes.c_ulonglong),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_: ClassVar[tuple[tuple[str, object], ...]] = (
        ("uMsg", ctypes.c_uint),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    )


class InputUnion(ctypes.Union):
    _fields_: ClassVar[tuple[tuple[str, object], ...]] = (
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    )


class INPUT(ctypes.Structure):
    _fields_: ClassVar[tuple[tuple[str, object], ...]] = (
        ("type", ctypes.c_uint),
        ("union", InputUnion),
    )


# ------------------------------------------------------------------
# Virtual-key map
# ------------------------------------------------------------------

VIRTUAL_KEYS: dict[str, int] = {
    "enter": win32con.VK_RETURN,
    "tab": win32con.VK_TAB,
    "esc": win32con.VK_ESCAPE,
    "escape": win32con.VK_ESCAPE,
    "space": win32con.VK_SPACE,
    "backspace": win32con.VK_BACK,
    "delete": win32con.VK_DELETE,
    "del": win32con.VK_DELETE,
    "insert": win32con.VK_INSERT,
    "home": win32con.VK_HOME,
    "end": win32con.VK_END,
    "pageup": win32con.VK_PRIOR,
    "pagedown": win32con.VK_NEXT,
    "left": win32con.VK_LEFT,
    "right": win32con.VK_RIGHT,
    "up": win32con.VK_UP,
    "down": win32con.VK_DOWN,
    "shift": win32con.VK_SHIFT,
    "ctrl": win32con.VK_CONTROL,
    "control": win32con.VK_CONTROL,
    "alt": win32con.VK_MENU,
    "win": win32con.VK_LWIN,
    "meta": win32con.VK_LWIN,
    "f1": win32con.VK_F1,
    "f2": win32con.VK_F2,
    "f3": win32con.VK_F3,
    "f4": win32con.VK_F4,
    "f5": win32con.VK_F5,
    "f6": win32con.VK_F6,
    "f7": win32con.VK_F7,
    "f8": win32con.VK_F8,
    "f9": win32con.VK_F9,
    "f10": win32con.VK_F10,
    "f11": win32con.VK_F11,
    "f12": win32con.VK_F12,
}

# ------------------------------------------------------------------
# Security constants (used by _validate_action in PROMPT mode)
# ------------------------------------------------------------------

# Executable names that are blocked from being launched by OPEN_APP in
# PROMPT mode because they provide unrestricted shell/scripting access.
_DANGEROUS_APPS: frozenset[str] = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "wscript",
        "wscript.exe",
        "cscript",
        "cscript.exe",
        "mshta",
        "mshta.exe",
        "regsvr32",
        "regsvr32.exe",
        "rundll32",
        "rundll32.exe",
    }
)

# file:// URL fragments that indicate sensitive OS paths blocked by NAVIGATE
# in PROMPT mode (checked after normalising backslashes to forward slashes).
_BLOCKED_PATH_FRAGMENTS: frozenset[str] = frozenset(
    {
        "windows/system32",
        "windows/syswow64",
        "windows/system",
    }
)

# ------------------------------------------------------------------
# Runtime
# ------------------------------------------------------------------


class WindowsComputerRuntime(ComputerRuntime):
    """
    Desktop runtime backed by native Windows APIs.

    Uses Win32 (via pywin32) for mouse/keyboard/window management and
    Pillow's ImageGrab for screenshots.  All long-running resources
    (launched processes) are tracked per session and terminated on
    ``close_session``.

    Thread-safety
    -------------
    ``_lock`` (RLock) protects all mutations of ``_state`` — the
    per-session counter increments and process-list appends — so that the
    runtime is safe to use with the controller's concurrent-session model.
    IO-bound operations (screenshot capture, mouse/keyboard injection) are
    performed *outside* the lock; only the bookkeeping dictionary writes are
    held under it.
    """

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._supports_window_capture = "window" in inspect.signature(ImageGrab.grab).parameters
        # Enable per-monitor DPI awareness so pixel coordinates returned by
        # GetCursorPos / GetWindowRect match what the user sees on screen.
        self._init_dpi_awareness()

    # ------------------------------------------------------------------
    # ComputerRuntime interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "windows_native"

    def capabilities(self) -> ComputerRuntimeCapabilities:
        return ComputerRuntimeCapabilities(
            supports_browser=False,
            supports_desktop=True,
            supports_semantic_targeting=False,
            supports_screenshots=True,
            supports_text_extraction=True,
            supports_keyboard=True,
            supports_mouse=True,
            supports_downloads=False,
            supports_windows=True,
            supports_window_management=True,
            supports_clipboard=True,
            metadata={
                "runtime": self.name,
                "window_capture": self._supports_window_capture,
                "semantic_targeting": "window-level and bounds-based only",
            },
        )

    def start_session(self, config: ComputerSessionConfig) -> ComputerSession:
        artifact_dir = Path(config.artifact_root) / config.session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._state[config.session_id] = {
                "artifact_dir": artifact_dir,
                "observation_index": 0,
                "action_index": 0,
                "last_url": config.start_url,
                "last_text": "",
                "launched_processes": [],  # list[subprocess.Popen]
            }
        if config.start_url:
            try:
                os.startfile(config.start_url)  # noqa: S606
                time.sleep(1.0)
            except OSError as exc:
                logger.warning("computer_use: could not open start_url {!r}: {}", config.start_url, exc)
        return ComputerSession(
            session_id=config.session_id,
            runtime_name=self.name,
            max_steps=config.max_steps,
            safety_mode=config.safety_mode,
            artifact_root=artifact_dir,
            observation_request=config.observation,
            metadata={"os_name": config.os_name, **config.metadata},
        )

    def get_observation(
        self,
        session: ComputerSession,
        request: ObservationRequest | None = None,
    ) -> ComputerObservation:
        request = request or session.observation_request

        # Mode-based filtering: SCREENSHOT_ONLY disables all text / DOM /
        # element extraction regardless of the individual include_* flags.
        # This is the fastest capture path — no clipboard or window enumeration.
        screenshot_only = request.mode == ObservationMode.SCREENSHOT_ONLY

        # Read volatile session state under lock to avoid tearing.
        with self._lock:
            raw_state = self._state.get(session.session_id, {})
            last_text: str = raw_state.get("last_text", "")
            last_url: str | None = raw_state.get("last_url")
            action_index: int = raw_state.get("action_index", 0)

        foreground = self._get_foreground_window_info()
        windows = self._list_windows(request.max_windows) if request.include_windows and not screenshot_only else []
        viewport = self._get_viewport()
        screenshot = self._capture_screenshot(session, request, foreground)
        clipboard_text = self._read_clipboard_text() if request.include_clipboard and not screenshot_only else None

        text_excerpt = None
        dom_excerpt = None
        elements: list[UIElementRef] = []
        metadata: dict[str, Any] = {}

        if request.include_text and not screenshot_only:
            parts = []
            if foreground and foreground.title:
                parts.append(f"active_window={foreground.title}")
            if last_text:
                parts.append(last_text)
            if clipboard_text:
                parts.append(f"clipboard={clipboard_text}")
            text_excerpt = "\n".join(part for part in parts if part) or None

        if request.include_dom and not screenshot_only:
            dom_bits = [f"runtime={self.name}", f"step={session.step_count}"]
            if foreground and foreground.class_name:
                dom_bits.append(f"class={foreground.class_name}")
            if last_url:
                dom_bits.append(f"last_url={last_url}")
            if request.mode == ObservationMode.DETAILED:
                # Detailed mode exposes extra diagnostic fields.
                dom_bits.append(f"action_index={action_index}")
            dom_excerpt = "; ".join(dom_bits)

        if request.include_elements and not screenshot_only:
            elements.extend(self._window_elements(foreground, windows, request.max_windows))

        if request.include_metadata:
            cursor_x, cursor_y = win32api.GetCursorPos()
            metadata = {
                "runtime": self.name,
                "cursor": {"x": cursor_x, "y": cursor_y},
                "observation_mode": request.mode.value,
                "last_action_index": action_index,
            }
            if clipboard_text:
                metadata["clipboard_text"] = clipboard_text

        return ComputerObservation(
            url=last_url,
            title=foreground.title if foreground else None,
            viewport=viewport,
            screenshot=screenshot,
            dom_excerpt=dom_excerpt,
            text_excerpt=text_excerpt,
            elements=elements,
            active_window=foreground,
            windows=windows,
            metadata=metadata,
        )

    def execute(self, session: ComputerSession, action: ComputerAction) -> ComputerActionResult:
        validation_error = self._validate_action(session, action)
        if validation_error is not None:
            return ComputerActionResult(
                action_id=action.action_id,
                success=False,
                error=validation_error,
                summary="action blocked by safety policy",
            )

        t0 = time.monotonic()
        try:
            summary = self._execute_action(session, action)
            action_elapsed_ms = (time.monotonic() - t0) * 1000

            # Update session state under lock to prevent counter races.
            with self._lock:
                state = self._state.get(session.session_id)
                if state is not None:
                    state["action_index"] += 1
                    if action.action_type == ComputerActionType.NAVIGATE and action.url:
                        state["last_url"] = action.url
                    if action.action_type == ComputerActionType.TYPE and action.text:
                        state["last_text"] = action.text

            # Post-action pause.  Skipped for WAIT because _execute_action
            # already sleeps for the full wait_ms duration — doubling would
            # cause the action to wait twice as long as requested.
            if action.wait_ms > 0 and action.action_type != ComputerActionType.WAIT:
                time.sleep(action.wait_ms / 1000)

            observation = self.get_observation(session, session.observation_request)
            artifact = ComputerArtifact(kind="trace", mime_type="text/plain", content=summary)
            total_elapsed_ms = (time.monotonic() - t0) * 1000
            metadata: dict[str, Any] = {
                "timing": {
                    "action_ms": round(action_elapsed_ms, 1),
                    "total_ms": round(total_elapsed_ms, 1),
                },
            }

            # EXTRACT_TEXT: perform the extraction once here and override the
            # placeholder summary that _execute_action returned.
            if action.action_type == ComputerActionType.EXTRACT_TEXT:
                extracted_text = self._extract_text(action)
                summary = f"extracted text ({len(extracted_text)} chars)"
                metadata["extracted_text"] = extracted_text
                observation = observation.model_copy(update={"text_excerpt": extracted_text})

            return ComputerActionResult(
                action_id=action.action_id,
                success=True,
                summary=summary,
                observation=observation,
                artifacts=[artifact],
                metadata=metadata,
            )
        except Exception as error:  # noqa: BLE001
            elapsed_ms = (time.monotonic() - t0) * 1000
            return ComputerActionResult(
                action_id=action.action_id,
                success=False,
                summary=f"{action.action_type.value} failed",
                error=str(error),
                metadata={"timing": {"total_ms": round(elapsed_ms, 1)}},
            )

    def close_session(self, session: ComputerSession) -> ComputerSession:
        observation = self.get_observation(session, session.observation_request)
        with self._lock:
            state = self._state.pop(session.session_id, {})
        # Terminate any processes launched during this session.
        # Give each process a short grace period after terminate() before
        # escalating to kill() so that well-behaved apps can flush data.
        for proc in state.get("launched_processes", []):
            with contextlib.suppress(OSError):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        with contextlib.suppress(OSError):
                            proc.kill()
        return session.model_copy(update={"status": "closed", "last_observation": observation})

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def _execute_action(
        self,
        session: ComputerSession,
        action: ComputerAction,
    ) -> str:
        """
        Dispatch *action* to the appropriate handler and return a summary.

        If ``action.timeout_ms`` is set (> 0), the handler is executed in a
        thread-pool thread with a deadline.  If it exceeds the deadline a
        ``TimeoutError`` is raised, which the caller converts into a failed
        ``ComputerActionResult``.
        """
        handler = self._ACTION_DISPATCH.get(action.action_type)
        if handler is None:
            message = f"unsupported action: {action.action_type}"
            raise ValueError(message)

        timeout_s = action.timeout_ms / 1000 if action.timeout_ms > 0 else None
        if timeout_s is not None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(handler, self, session, action)
                try:
                    return future.result(timeout=timeout_s)
                except concurrent.futures.TimeoutError:
                    msg = f"{action.action_type.value} timed out after {action.timeout_ms} ms"
                    raise TimeoutError(msg) from None
        return handler(self, session, action)

    # --- Individual action handlers (called via _ACTION_DISPATCH) ---

    def _do_click(self, _session: ComputerSession, action: ComputerAction) -> str:
        point = self._resolve_target(action.target)
        if point is None:
            msg = "action target is required"
            raise ValueError(msg)
        self._move_cursor(point)
        if action.action_type == ComputerActionType.HOVER:
            return f"hovered at {point.x},{point.y}"
        button = MouseButton.RIGHT if action.action_type == ComputerActionType.RIGHT_CLICK else action.button
        clicks = 2 if action.action_type == ComputerActionType.DOUBLE_CLICK else 1
        self._click(button, clicks=clicks)
        return f"clicked {button.value} at {point.x},{point.y}"

    def _do_drag(self, _session: ComputerSession, action: ComputerAction) -> str:
        start = self._resolve_target(action.target)
        end = self._resolve_target(action.end_target)
        if start is None or end is None:
            msg = "drag requires target and end_target"
            raise ValueError(msg)
        self._drag(start, end, action.button)
        return f"dragged from {start.x},{start.y} to {end.x},{end.y}"

    def _do_scroll(self, _session: ComputerSession, action: ComputerAction) -> str:
        if action.target is not None:
            point = self._resolve_target(action.target)
            if point is not None:
                self._move_cursor(point)
        self._scroll(action.delta_y or 0, action.delta_x or 0)
        return f"scrolled by ({action.delta_x or 0}, {action.delta_y or 0})"

    def _do_type(self, _session: ComputerSession, action: ComputerAction) -> str:
        if action.target is not None:
            point = self._resolve_target(action.target)
            if point is not None:
                self._move_cursor(point)
                self._click(MouseButton.LEFT)
        self._type_text(action.text or "")
        return f"typed {len(action.text or '')} chars"

    def _do_hotkey(self, _session: ComputerSession, action: ComputerAction) -> str:
        if not action.keys:
            msg = "hotkey requires keys"
            raise ValueError(msg)
        self._send_hotkey(action.keys)
        return f"pressed hotkey {'+'.join(action.keys)}"

    def _do_key_press(self, _session: ComputerSession, action: ComputerAction) -> str:
        if not action.keys:
            msg = "key_press requires keys"
            raise ValueError(msg)
        for key in action.keys:
            self._send_hotkey([key])
        return f"pressed keys {','.join(action.keys)}"

    def _do_wait(self, _session: ComputerSession, action: ComputerAction) -> str:
        time.sleep((action.wait_ms or 0) / 1000)
        return f"waited {action.wait_ms} ms"

    def _do_navigate(self, _session: ComputerSession, action: ComputerAction) -> str:
        if not action.url:
            msg = "navigate requires url"
            raise ValueError(msg)
        os.startfile(action.url)  # noqa: S606
        return f"navigated to {action.url}"

    def _do_open_app(self, session: ComputerSession, action: ComputerAction) -> str:
        target_path = action.path or action.text
        if not target_path:
            msg = "open_app requires path or text"
            raise ValueError(msg)
        # Store the Popen object (not just the PID) so close_session can
        # terminate the process if it is still running.
        process = subprocess.Popen([target_path, *action.arguments])  # noqa: S603
        with self._lock:
            state = self._state.get(session.session_id)
            if state is not None:
                state["launched_processes"].append(process)
        return f"opened app {target_path}"

    def _do_focus_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "focus_window requires title in text or metadata.title"
            raise ValueError(msg)
        hwnd = self._find_window_by_title(title)
        if hwnd is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        self._focus_window(hwnd)
        return f"focused window {title}"

    def _do_resize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "resize_window requires title in text or metadata.title"
            raise ValueError(msg)
        hwnd = self._find_window_by_title(title)
        if hwnd is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        width = action.width
        height = action.height
        if width is None or height is None:
            msg = "resize_window requires width and height"
            raise ValueError(msg)
        self._resize_window(hwnd, width, height)
        return f"resized window {title!r} to {width}×{height}"

    def _do_minimize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "minimize_window requires title in text or metadata.title"
            raise ValueError(msg)
        hwnd = self._find_window_by_title(title)
        if hwnd is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return f"minimized window {title!r}"

    def _do_maximize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "maximize_window requires title in text or metadata.title"
            raise ValueError(msg)
        hwnd = self._find_window_by_title(title)
        if hwnd is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        return f"maximized window {title!r}"

    def _do_screenshot(self, _session: ComputerSession, _action: ComputerAction) -> str:
        return "captured screenshot"

    def _do_extract_text(self, _session: ComputerSession, _action: ComputerAction) -> str:
        # Actual extraction and summary are performed in execute() after
        # this method returns, so we avoid calling _extract_text twice.
        return "extract_text"

    # Class-level dispatch table — maps action types to handler methods.
    _ACTION_DISPATCH: ClassVar[dict[ComputerActionType, Any]] = {
        ComputerActionType.CLICK: _do_click,
        ComputerActionType.DOUBLE_CLICK: _do_click,
        ComputerActionType.RIGHT_CLICK: _do_click,
        ComputerActionType.HOVER: _do_click,
        ComputerActionType.DRAG: _do_drag,
        ComputerActionType.SCROLL: _do_scroll,
        ComputerActionType.TYPE: _do_type,
        ComputerActionType.HOTKEY: _do_hotkey,
        ComputerActionType.KEY_PRESS: _do_key_press,
        ComputerActionType.WAIT: _do_wait,
        ComputerActionType.NAVIGATE: _do_navigate,
        ComputerActionType.OPEN_APP: _do_open_app,
        ComputerActionType.FOCUS_WINDOW: _do_focus_window,
        ComputerActionType.RESIZE_WINDOW: _do_resize_window,
        ComputerActionType.MINIMIZE_WINDOW: _do_minimize_window,
        ComputerActionType.MAXIMIZE_WINDOW: _do_maximize_window,
        ComputerActionType.SCREENSHOT: _do_screenshot,
        ComputerActionType.EXTRACT_TEXT: _do_extract_text,
    }

    # ------------------------------------------------------------------
    # Safety validation
    # ------------------------------------------------------------------

    def _validate_action(self, session: ComputerSession, action: ComputerAction) -> str | None:
        if session.safety_mode == SafetyMode.UNRESTRICTED:
            return None

        # --- Dangerous hotkey check (applies to PROMPT and ALLOWLIST) ---
        guarded_actions = {ComputerActionType.HOTKEY, ComputerActionType.KEY_PRESS}
        if action.action_type in guarded_actions and action.keys:
            dangerous_hotkeys = {
                frozenset(["alt", "f4"]),
                frozenset(["ctrl", "alt", "delete"]),
                frozenset(["win", "l"]),
            }
            normalized = frozenset(key.lower() for key in action.keys)
            if normalized in dangerous_hotkeys:
                return "dangerous hotkey blocked by safety policy"

        # --- Additional security checks for PROMPT mode ---
        if session.safety_mode == SafetyMode.PROMPT:
            # Block launching of privileged shells and scripting hosts.
            if action.action_type == ComputerActionType.OPEN_APP:
                target = (action.path or action.text or "").lower()
                app_name = Path(target).name.strip()
                if app_name in _DANGEROUS_APPS:
                    return f"launching {app_name!r} is blocked by safety policy"

            # Block navigation to sensitive OS file-system paths via file://.
            if action.action_type == ComputerActionType.NAVIGATE:
                url = (action.url or "").lower().replace("\\", "/")
                if url.startswith("file://"):
                    for fragment in _BLOCKED_PATH_FRAGMENTS:
                        if fragment in url:
                            return "navigating to system paths is blocked by safety policy"

        # --- Allowlist check ---
        if session.safety_mode == SafetyMode.ALLOWLIST:
            allowed_actions = set(session.metadata.get("allowed_actions", []))
            if action.action_type.value not in allowed_actions:
                return f"action {action.action_type.value} is not in allowlist"

        return None

    # ------------------------------------------------------------------
    # Observation helpers
    # ------------------------------------------------------------------

    def _window_elements(
        self,
        foreground: WindowInfo | None,
        windows: list[WindowInfo],
        max_windows: int,
    ) -> list[UIElementRef]:
        elements: list[UIElementRef] = []
        if foreground and foreground.bounds is not None:
            elements.append(
                UIElementRef(
                    role="window",
                    name=foreground.title,
                    bounds=foreground.bounds,
                    metadata={"hwnd": foreground.hwnd, "class_name": foreground.class_name},
                )
            )
        for window in windows[: max(0, max_windows - len(elements))]:
            if window.bounds is None:
                continue
            elements.append(
                UIElementRef(
                    role="window",
                    name=window.title,
                    bounds=window.bounds,
                    metadata={"hwnd": window.hwnd, "class_name": window.class_name},
                )
            )
        return elements

    # ------------------------------------------------------------------
    # Mouse helpers
    # ------------------------------------------------------------------

    def _resolve_target(self, target: ComputerCoordinate | UIElementRef | None) -> ComputerCoordinate | None:
        if target is None:
            return None
        if isinstance(target, ComputerCoordinate):
            return target
        if target.bounds is not None:
            return ComputerCoordinate(
                x=target.bounds.x + max(1, target.bounds.width) // 2,
                y=target.bounds.y + max(1, target.bounds.height) // 2,
            )
        x = target.metadata.get("x")
        y = target.metadata.get("y")
        if isinstance(x, int) and isinstance(y, int):
            return ComputerCoordinate(x=x, y=y)
        return None

    def _validate_coordinate(self, point: ComputerCoordinate) -> None:
        """
        Raise ValueError if *point* lies outside the virtual screen.

        Correctly handles multi-monitor configurations where the virtual screen
        origin may be at negative coordinates (e.g. a monitor placed to the
        left of the primary).
        """
        # SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN give the top-left corner of the
        # combined virtual desktop — can be negative for left/top monitors.
        x_origin = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        y_origin = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if not (x_origin <= point.x < x_origin + width and y_origin <= point.y < y_origin + height):
            msg = (
                f"coordinate ({point.x}, {point.y}) is outside the virtual screen "
                f"(origin: {x_origin},{y_origin}  size: {width}\u00d7{height})"
            )
            raise ValueError(msg)

    def _move_cursor(self, point: ComputerCoordinate) -> None:
        self._validate_coordinate(point)
        win32api.SetCursorPos((point.x, point.y))
        time.sleep(0.05)

    def _click(self, button: MouseButton, clicks: int = 1) -> None:
        down_flag, up_flag = {
            MouseButton.LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            MouseButton.RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            MouseButton.MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[button]
        for _ in range(clicks):
            self._send_mouse(down_flag)
            self._send_mouse(up_flag)
            time.sleep(0.05)

    def _drag(
        self,
        start: ComputerCoordinate,
        end: ComputerCoordinate,
        button: MouseButton,
    ) -> None:
        down_flag, up_flag = {
            MouseButton.LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            MouseButton.RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            MouseButton.MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[button]
        self._move_cursor(start)
        self._send_mouse(down_flag)
        time.sleep(0.05)
        self._move_cursor(end)
        self._send_mouse(up_flag)

    def _scroll(self, delta_y: int, delta_x: int = 0) -> None:
        """
        Inject vertical and/or horizontal scroll wheel events.

        The sign convention matches the LLM-facing schema:

        Args:
            delta_y: Vertical scroll distance (positive = **down**, negative = **up**).
                Each unit equals one notch (120 WHEEL_DELTA units internally).
                Win32 uses the opposite sign (positive = up), so we negate here.
            delta_x: Horizontal scroll distance (positive = right, negative = left).
                Requires a horizontal scroll wheel or touch-pad.

        """
        if delta_y != 0:
            # Win32 WHEEL_DELTA: positive = scroll up.
            # LLM schema: positive = scroll down.  Negate to reconcile.
            wheel_delta = -int(delta_y) * 120
            event = INPUT(
                type=INPUT_MOUSE,
                union=InputUnion(mi=MOUSEINPUT(0, 0, wheel_delta, MOUSEEVENTF_WHEEL, 0, 0)),
            )
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if delta_x != 0:
            hwheel_delta = int(delta_x) * 120
            event = INPUT(
                type=INPUT_MOUSE,
                union=InputUnion(mi=MOUSEINPUT(0, 0, hwheel_delta, MOUSEEVENTF_HWHEEL, 0, 0)),
            )
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

    def _send_mouse(self, flags: int) -> None:
        event = INPUT(
            type=INPUT_MOUSE,
            union=InputUnion(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0)),
        )
        user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

    # ------------------------------------------------------------------
    # Keyboard helpers
    # ------------------------------------------------------------------

    def _type_text(self, text: str) -> None:
        for char in text:
            self._send_unicode_char(char)
            time.sleep(0.005)

    def _send_unicode_char(self, char: str) -> None:
        down = INPUT(
            type=INPUT_KEYBOARD,
            union=InputUnion(ki=KEYBDINPUT(0, ord(char), KEYEVENTF_UNICODE, 0, 0)),
        )
        up = INPUT(
            type=INPUT_KEYBOARD,
            union=InputUnion(ki=KEYBDINPUT(0, ord(char), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)),
        )
        user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))

    def _send_hotkey(self, keys: list[str]) -> None:
        vks = [self._resolve_virtual_key(key) for key in keys]
        for vk in vks:
            self._send_key_vk(vk, keyup=False)
            time.sleep(0.01)
        for vk in reversed(vks):
            self._send_key_vk(vk, keyup=True)
            time.sleep(0.01)

    def _send_key_vk(self, vk: int, keyup: bool) -> None:
        flags = KEYEVENTF_KEYUP if keyup else 0
        event = INPUT(
            type=INPUT_KEYBOARD,
            union=InputUnion(ki=KEYBDINPUT(vk, 0, flags, 0, 0)),
        )
        user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

    def _resolve_virtual_key(self, key: str) -> int:
        normalized = key.lower()
        if normalized in VIRTUAL_KEYS:
            return VIRTUAL_KEYS[normalized]
        if len(normalized) == 1:
            return ord(normalized.upper())
        message = f"unsupported key: {key}"
        raise ValueError(message)

    # ------------------------------------------------------------------
    # Screenshot helpers
    # ------------------------------------------------------------------

    def _capture_screenshot(
        self,
        session: ComputerSession,
        request: ObservationRequest,
        foreground: WindowInfo | None,
    ) -> ComputerArtifact | None:
        if not request.include_screenshot:
            return None

        # Increment the counter and read artifact_dir under lock to avoid
        # two concurrent observations writing to the same file path.
        with self._lock:
            state = self._state.get(session.session_id)
            if state is None:
                return None
            state["observation_index"] += 1
            obs_index: int = state["observation_index"]
            artifact_dir: Path = state["artifact_dir"]

        artifact_path = artifact_dir / f"obs_{obs_index:04d}.png"

        should_capture_window = request.active_window_only and foreground is not None and foreground.bounds is not None
        metadata: dict[str, Any] = {"active_window_only": request.active_window_only, "mode": request.mode.value}

        if should_capture_window and foreground is not None and foreground.bounds is not None:
            if self._supports_window_capture:
                image = ImageGrab.grab(window=foreground.hwnd)
                if self._image_is_probably_blank(image):
                    metadata["window_capture_fallback"] = "bbox"
                    bbox = (
                        foreground.bounds.x,
                        foreground.bounds.y,
                        foreground.bounds.x + foreground.bounds.width,
                        foreground.bounds.y + foreground.bounds.height,
                    )
                    image = ImageGrab.grab(bbox=bbox, all_screens=True)
            else:
                bbox = (
                    foreground.bounds.x,
                    foreground.bounds.y,
                    foreground.bounds.x + foreground.bounds.width,
                    foreground.bounds.y + foreground.bounds.height,
                )
                image = ImageGrab.grab(bbox=bbox, all_screens=True)
        else:
            image = ImageGrab.grab(all_screens=True)

        if self._image_is_probably_blank(image):
            metadata["warning"] = "blank_screenshot_detected"
            logger.debug(
                "computer_use: blank screenshot detected for session {}",
                session.session_id,
            )

        # Resize if the request specifies a maximum dimension.
        max_dim = request.screenshot_max_dimension
        if max_dim is not None and max_dim > 0:
            w, h = image.size
            longest = max(w, h)
            if longest > max_dim:
                scale = max_dim / longest
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                # Use LANCZOS resampling (fallback to Resampling.LANCZOS for newer Pillow)
                # LANCZOS was moved to Image.Resampling in Pillow 10.0.0
                if hasattr(Image, "LANCZOS"):
                    resample = Image.LANCZOS
                elif hasattr(Image, "Resampling") and hasattr(Image.Resampling, "LANCZOS"):
                    resample = Image.Resampling.LANCZOS
                else:
                    # Fallback to numeric constant (LANCZOS = 1 in Pillow)
                    resample = 1
                image = image.resize((new_w, new_h), resample=resample)
                metadata["resized_from"] = {"width": w, "height": h}
                metadata["resized_to"] = {"width": new_w, "height": new_h}

        # Determine output format (PNG or JPEG).
        fmt = request.screenshot_format.upper()
        if fmt in ("JPEG", "JPG"):
            fmt = "JPEG"
            ext = ".jpg"
            mime = "image/jpeg"
            save_kwargs: dict[str, Any] = {"quality": request.screenshot_quality}
            # JPEG doesn't support RGBA — convert to RGB.
            if image.mode in ("RGBA", "LA", "PA"):
                image = image.convert("RGB")
        else:
            fmt = "PNG"
            ext = ".png"
            mime = "image/png"
            save_kwargs = {}

        # Replace extension in the artifact path if needed.
        artifact_path = artifact_path.with_suffix(ext)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(artifact_path, format=fmt, **save_kwargs)
        metadata["format"] = fmt.lower()
        return ComputerArtifact(
            kind="screenshot",
            mime_type=mime,
            path=str(artifact_path),
            metadata=metadata,
        )

    def _image_is_probably_blank(self, image: Image.Image) -> bool:
        return image.getbbox() is None

    # ------------------------------------------------------------------
    # Viewport / window helpers
    # ------------------------------------------------------------------

    def _get_viewport(self) -> ComputerViewport:
        width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            width = user32.GetSystemMetrics(0)  # SM_CXSCREEN
            height = user32.GetSystemMetrics(1)  # SM_CYSCREEN
        return ComputerViewport(width=width, height=height, pixel_ratio=1.0)

    def _get_foreground_window_info(self) -> WindowInfo | None:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd == 0:
            return None
        return self._window_info(hwnd, is_foreground=True)

    def _window_info(self, hwnd: int, is_foreground: bool = False) -> WindowInfo | None:
        if not win32gui.IsWindow(hwnd):
            return None
        title = win32gui.GetWindowText(hwnd)
        try:
            class_name = win32gui.GetClassName(hwnd)
        except Exception:  # noqa: BLE001
            class_name = None
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            bounds = ComputerBounds(
                x=left,
                y=top,
                width=max(0, right - left),
                height=max(0, bottom - top),
            )
        except Exception:  # noqa: BLE001
            bounds = None
        return WindowInfo(
            hwnd=hwnd,
            title=title,
            class_name=class_name,
            bounds=bounds,
            is_foreground=is_foreground,
        )

    def _list_windows(self, max_windows: int) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        foreground_hwnd = win32gui.GetForegroundWindow()

        def callback(hwnd: int, _lparam: int) -> bool:
            if len(windows) >= max_windows:
                return False
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title.strip():
                return True
            info = self._window_info(hwnd, is_foreground=hwnd == foreground_hwnd)
            if info is not None:
                windows.append(info)
            return True

        win32gui.EnumWindows(callback, 0)
        return windows

    def _find_window_by_title(self, title: str) -> int | None:
        lowered = title.lower()
        for window in self._list_windows(100):
            if lowered in window.title.lower():
                return window.hwnd
        return None

    def _focus_window(self, hwnd: int) -> None:
        win32gui.ShowWindow(hwnd, SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.1)

    def _resize_window(self, hwnd: int, width: int, height: int) -> None:
        """Resize a window to the given dimensions, keeping its current position."""
        try:
            left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
        except Exception:  # noqa: BLE001
            left, top = 0, 0
        win32gui.ShowWindow(hwnd, SW_RESTORE)
        repaint = True
        win32gui.MoveWindow(hwnd, left, top, width, height, repaint)
        time.sleep(0.1)

    # ------------------------------------------------------------------
    # Text extraction helpers
    # ------------------------------------------------------------------

    def _extract_text(self, action: ComputerAction) -> str:
        strategy = str(action.metadata.get("strategy", "clipboard"))
        if strategy == "selection_copy":
            self._send_hotkey(["ctrl", "c"])
            time.sleep(0.1)
            return self._read_clipboard_text() or ""
        if strategy == "window_title":
            foreground = self._get_foreground_window_info()
            return foreground.title if foreground else ""
        if strategy == "ocr":
            return self._ocr_extract(action)
        return self._read_clipboard_text() or ""

    def _ocr_extract(self, action: ComputerAction) -> str:
        """
        Run OCR on the latest screenshot (or a cropped region).

        Requires ``pytesseract`` and a Tesseract installation.  If either is
        unavailable the method falls back to clipboard text with a warning.

        The optional ``action.metadata["region"]`` dict ``{x, y, width, height}``
        crops the screenshot to that bounding box before running OCR, which is
        both faster and more accurate for targeted extraction.

        ``action.metadata["lang"]`` can specify Tesseract language codes
        (default ``"eng"``).
        """
        try:
            import pytesseract
        except ImportError:
            logger.warning(
                "computer_use: pytesseract not installed — falling back to clipboard. "
                "Install with: pip install pytesseract"
            )
            return self._read_clipboard_text() or ""

        # Grab the current screen (or active window).
        image = ImageGrab.grab(all_screens=True)

        # Optionally crop to a region.
        region = action.metadata.get("region")
        if isinstance(region, dict):
            rx = int(region.get("x", 0))
            ry = int(region.get("y", 0))
            rw = int(region.get("width", image.width))
            rh = int(region.get("height", image.height))
            image = image.crop((rx, ry, rx + rw, ry + rh))

        lang = str(action.metadata.get("lang", "eng"))
        try:
            text: str = pytesseract.image_to_string(image, lang=lang)
            return text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("computer_use: OCR failed ({}) — falling back to clipboard", exc)
            return self._read_clipboard_text() or ""

    def _read_clipboard_text(self, retries: int = 3, delay: float = 0.05) -> str | None:
        """
        Read Unicode text from the clipboard with retry logic.

        The Windows clipboard can be temporarily locked by another process
        (e.g. after Ctrl+C), causing ``OpenClipboard`` to raise
        ``pywintypes.error``.  Retrying a few times with a short back-off
        handles the common transient-lock scenario reliably.
        """
        import win32clipboard

        for attempt in range(retries):
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(CF_UNICODETEXT):
                        return win32clipboard.GetClipboardData(CF_UNICODETEXT)
                    return None
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:  # noqa: BLE001
                if attempt < retries - 1:
                    time.sleep(delay * (attempt + 1))
        return None

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _init_dpi_awareness(self) -> None:
        """
        Enable per-monitor DPI awareness for accurate pixel coordinates.

        Without this, GetCursorPos / GetWindowRect return virtualised
        (DPI-scaled) values that don't match the actual screen pixels on
        high-DPI displays.  We try the modern API first (Windows 8.1+) and
        fall back to the legacy Vista-era call.
        """
        try:
            # 2 = PROCESS_PER_MONITOR_DPI_AWARE (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                ctypes.windll.user32.SetProcessDPIAware()  # Vista+
