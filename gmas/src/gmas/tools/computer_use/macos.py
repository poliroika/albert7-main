"""
macOS native computer-use runtime.

Provides full desktop automation on macOS: mouse, keyboard, screenshots,
window management, and clipboard access via native macOS APIs and utilities.

Uses:
- screencapture: screenshots (built-in)
- cliclick: mouse/keyboard (brew install cliclick)
- osascript / AppleScript: window management, app control, clipboard
- pytesseract: OCR text extraction (optional)

Requirements:
    pip install pillow  (or: pip install "frontier-ai-gmas[computer-use]")
    brew install cliclick  (for mouse/keyboard)
"""

import concurrent.futures
import contextlib
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image

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

_subprocess_run = subprocess.run
_subprocess_popen = subprocess.Popen

# ------------------------------------------------------------------
# Key name map — maps friendly names to macOS key codes for AppleScript
# ------------------------------------------------------------------

_MIN_COORD_PARTS = 2
_MIN_BOUNDS_PARTS = 4
_MIN_RESOLUTION_PARTS = 3

MACOS_KEYS: dict[str, int] = {
    "enter": 36,
    "return": 36,
    "tab": 48,
    "esc": 53,
    "escape": 53,
    "space": 49,
    "backspace": 51,
    "delete": 117,
    "del": 117,
    "insert": -1,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
    "left": 123,
    "right": 124,
    "up": 126,
    "down": 125,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}

MACOS_MODIFIER_FLAGS: dict[str, str] = {
    "shift": "shift down",
    "ctrl": "control down",
    "control": "control down",
    "alt": "option down",
    "option": "option down",
    "cmd": "command down",
    "command": "command down",
    "super": "command down",
    "win": "command down",
    "meta": "command down",
}

# ------------------------------------------------------------------
# Security constants
# ------------------------------------------------------------------

_DANGEROUS_APPS: frozenset[str] = frozenset(
    {
        "Terminal",
        "Terminal.app",
        "iTerm",
        "iTerm.app",
        "iTerm2",
        "bash",
        "sh",
        "zsh",
        "fish",
        "python",
        "python3",
        "perl",
        "ruby",
        "node",
        "sudo",
        "su",
        "osascript",
    }
)

_BLOCKED_PATH_FRAGMENTS: frozenset[str] = frozenset(
    {
        "/private/etc/",
        "/System/",
        "/usr/sbin/",
        "/Library/LaunchDaemons/",
        "/Library/LaunchAgents/",
    }
)


# ------------------------------------------------------------------
# Helper: run a command and return stdout
# ------------------------------------------------------------------


def _run(args: list[str], *, timeout: float = 10.0) -> str:
    result = _subprocess_run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _osascript(script: str, *, timeout: float = 10.0) -> str:
    return _run(["osascript", "-e", script], timeout=timeout)


def _has_command(name: str) -> bool:
    return shutil.which(name) is not None


# ------------------------------------------------------------------
# Runtime
# ------------------------------------------------------------------


class MacOSComputerRuntime(ComputerRuntime):
    """
    Desktop runtime backed by native macOS APIs and tools.

    Uses screencapture for screenshots, cliclick for mouse/keyboard,
    osascript (AppleScript) for window management and clipboard,
    and pytesseract for OCR.

    Thread-safety
    -------------
    ``_lock`` (RLock) protects all mutations of ``_state``.
    IO-bound operations are performed outside the lock.
    """

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._has_cliclick = _has_command("cliclick")
        if not self._has_cliclick:
            logger.warning(
                "computer_use: cliclick not found — mouse/keyboard will use AppleScript fallback. "
                "Install with: brew install cliclick"
            )

    # ------------------------------------------------------------------
    # ComputerRuntime interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "macos_native"

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
                "cliclick": self._has_cliclick,
                "screencapture": True,
                "osascript": True,
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
                "launched_processes": [],
            }
        if config.start_url:
            with contextlib.suppress(Exception):
                _subprocess_popen(["open", config.start_url])
                time.sleep(1.0)
        return ComputerSession(
            session_id=config.session_id,
            runtime_name=self.name,
            max_steps=config.max_steps,
            safety_mode=config.safety_mode,
            artifact_root=artifact_dir,
            observation_request=config.observation,
            metadata={"os_name": "macos", **config.metadata},
        )

    def get_observation(
        self,
        session: ComputerSession,
        request: ObservationRequest | None = None,
    ) -> ComputerObservation:
        request = request or session.observation_request
        screenshot_only = request.mode == ObservationMode.SCREENSHOT_ONLY

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
            parts: list[str] = []
            if foreground and foreground.title:
                parts.append(f"active_window={foreground.title}")
            if last_text:
                parts.append(last_text)
            if clipboard_text:
                parts.append(f"clipboard={clipboard_text}")
            text_excerpt = "\n".join(p for p in parts if p) or None

        if request.include_dom and not screenshot_only:
            dom_bits = [f"runtime={self.name}", f"step={session.step_count}"]
            if foreground and foreground.class_name:
                dom_bits.append(f"class={foreground.class_name}")
            if last_url:
                dom_bits.append(f"last_url={last_url}")
            if request.mode == ObservationMode.DETAILED:
                dom_bits.append(f"action_index={action_index}")
            dom_excerpt = "; ".join(dom_bits)

        if request.include_elements and not screenshot_only:
            elements.extend(self._window_elements(foreground, windows, request.max_windows))

        if request.include_metadata:
            cursor = self._get_cursor_pos()
            metadata = {
                "runtime": self.name,
                "cursor": {"x": cursor[0], "y": cursor[1]} if cursor else None,
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

            with self._lock:
                state = self._state.get(session.session_id)
                if state is not None:
                    state["action_index"] += 1
                    if action.action_type == ComputerActionType.NAVIGATE and action.url:
                        state["last_url"] = action.url
                    if action.action_type == ComputerActionType.TYPE and action.text:
                        state["last_text"] = action.text

            if action.wait_ms > 0 and action.action_type != ComputerActionType.WAIT:
                time.sleep(action.wait_ms / 1000)

            observation = self.get_observation(session, session.observation_request)
            artifact = ComputerArtifact(kind="trace", mime_type="text/plain", content=summary)
            total_elapsed_ms = (time.monotonic() - t0) * 1000
            result_metadata: dict[str, Any] = {
                "timing": {
                    "action_ms": round(action_elapsed_ms, 1),
                    "total_ms": round(total_elapsed_ms, 1),
                },
            }

            if action.action_type == ComputerActionType.EXTRACT_TEXT:
                extracted_text = self._extract_text(action)
                summary = f"extracted text ({len(extracted_text)} chars)"
                result_metadata["extracted_text"] = extracted_text
                observation = observation.model_copy(update={"text_excerpt": extracted_text})

            return ComputerActionResult(
                action_id=action.action_id,
                success=True,
                summary=summary,
                observation=observation,
                artifacts=[artifact],
                metadata=result_metadata,
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

    def _execute_action(self, session: ComputerSession, action: ComputerAction) -> str:
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

    # --- Individual action handlers ---

    def _do_click(self, _session: ComputerSession, action: ComputerAction) -> str:
        point = self._resolve_target(action.target)
        if point is None:
            msg = "action target is required"
            raise ValueError(msg)
        if action.action_type == ComputerActionType.HOVER:
            self._mouse_move(point)
            return f"hovered at {point.x},{point.y}"
        btn = MouseButton.RIGHT if action.action_type == ComputerActionType.RIGHT_CLICK else action.button
        is_double = action.action_type == ComputerActionType.DOUBLE_CLICK
        self._mouse_click(point, btn, double=is_double)
        return f"clicked {btn.value} at {point.x},{point.y}"

    def _do_drag(self, _session: ComputerSession, action: ComputerAction) -> str:
        start = self._resolve_target(action.target)
        end = self._resolve_target(action.end_target)
        if start is None or end is None:
            msg = "drag requires target and end_target"
            raise ValueError(msg)
        if self._has_cliclick:
            _run(["cliclick", f"dd:{start.x},{start.y}", f"du:{end.x},{end.y}"])
        else:
            script = f'tell application "System Events" to click at {{{start.x}, {start.y}}}'
            _osascript(script)
        return f"dragged from {start.x},{start.y} to {end.x},{end.y}"

    def _do_scroll(self, _session: ComputerSession, action: ComputerAction) -> str:
        if action.target is not None:
            point = self._resolve_target(action.target)
            if point is not None:
                self._mouse_move(point)
        dy = action.delta_y or 0
        dx = action.delta_x or 0
        if self._has_cliclick:
            if dy != 0:
                _run(["cliclick", "kd:fn", f"scroll:{0},{-dy}", "ku:fn"])
        elif dy != 0:
            script = f'tell application "System Events" to scroll area 1 of scroll area 1 to scroll by {-dy}'
            with contextlib.suppress(Exception):
                _osascript(script)
        return f"scrolled by ({dx}, {dy})"

    def _do_type(self, _session: ComputerSession, action: ComputerAction) -> str:
        if action.target is not None:
            point = self._resolve_target(action.target)
            if point is not None:
                self._mouse_click(point, MouseButton.LEFT)
        text = action.text or ""
        if self._has_cliclick:
            _run(["cliclick", f"t:{text}"])
        else:
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            _osascript(f'tell application "System Events" to keystroke "{escaped}"')
        return f"typed {len(text)} chars"

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
        _subprocess_popen(["open", action.url])
        return f"navigated to {action.url}"

    def _do_open_app(self, session: ComputerSession, action: ComputerAction) -> str:
        target_path = action.path or action.text
        if not target_path:
            msg = "open_app requires path or text"
            raise ValueError(msg)
        if target_path.endswith(".app") or "/" in target_path:
            process = _subprocess_popen(["open", "-a", target_path])
        else:
            process = _subprocess_popen(["open", "-a", target_path])
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
        app_name = self._find_app_by_window_title(title)
        if app_name is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        _osascript(f'tell application "{app_name}" to activate')
        return f"focused window {title}"

    def _do_resize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "resize_window requires title in text or metadata.title"
            raise ValueError(msg)
        width = action.width
        height = action.height
        if width is None or height is None:
            msg = "resize_window requires width and height"
            raise ValueError(msg)
        app_name = self._find_app_by_window_title(title)
        if app_name is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        script = f'tell application "{app_name}" to set the bounds of the front window to {{0, 0, {width}, {height}}}'
        _osascript(script)
        return f"resized window {title!r} to {width}×{height}"

    def _do_minimize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "minimize_window requires title in text or metadata.title"
            raise ValueError(msg)
        app_name = self._find_app_by_window_title(title)
        if app_name is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        _osascript(f'tell application "{app_name}" to set miniaturized of front window to true')
        return f"minimized window {title!r}"

    def _do_maximize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "maximize_window requires title in text or metadata.title"
            raise ValueError(msg)
        app_name = self._find_app_by_window_title(title)
        if app_name is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        viewport = self._get_viewport()
        script = (
            f'tell application "{app_name}" to set the bounds of the front window '
            f"to {{0, 0, {viewport.width}, {viewport.height}}}"
        )
        _osascript(script)
        return f"maximized window {title!r}"

    def _do_screenshot(self, _session: ComputerSession, _action: ComputerAction) -> str:
        return "captured screenshot"

    def _do_extract_text(self, _session: ComputerSession, _action: ComputerAction) -> str:
        return "extract_text"

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

        guarded_actions = {ComputerActionType.HOTKEY, ComputerActionType.KEY_PRESS}
        if action.action_type in guarded_actions and action.keys:
            dangerous_hotkeys = {
                frozenset(["cmd", "q"]),
                frozenset(["command", "q"]),
                frozenset(["ctrl", "alt", "delete"]),
                frozenset(["cmd", "option", "escape"]),
                frozenset(["command", "option", "escape"]),
            }
            normalized = frozenset(key.lower() for key in action.keys)
            if normalized in dangerous_hotkeys:
                return "dangerous hotkey blocked by safety policy"

        if session.safety_mode == SafetyMode.PROMPT:
            if action.action_type == ComputerActionType.OPEN_APP:
                target = action.path or action.text or ""
                app_name = Path(target).stem.strip()
                if app_name in _DANGEROUS_APPS or target in _DANGEROUS_APPS:
                    return f"launching {app_name!r} is blocked by safety policy"

            if action.action_type == ComputerActionType.NAVIGATE:
                url = (action.url or "").lower()
                if url.startswith("file://"):
                    for fragment in _BLOCKED_PATH_FRAGMENTS:
                        if fragment in url:
                            return "navigating to system paths is blocked by safety policy"

        if session.safety_mode == SafetyMode.ALLOWLIST:
            allowed_actions = set(session.metadata.get("allowed_actions", []))
            if action.action_type.value not in allowed_actions:
                return f"action {action.action_type.value} is not in allowlist"

        return None

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

    def _mouse_move(self, point: ComputerCoordinate) -> None:
        if self._has_cliclick:
            _run(["cliclick", f"m:{point.x},{point.y}"])
        else:
            _osascript(f'tell application "System Events" to set position of the mouse to {{{point.x}, {point.y}}}')
        time.sleep(0.05)

    def _mouse_click(self, point: ComputerCoordinate, button: MouseButton, *, double: bool = False) -> None:
        if self._has_cliclick:
            if double:
                _run(["cliclick", f"dc:{point.x},{point.y}"])
            elif button == MouseButton.RIGHT:
                _run(["cliclick", f"rc:{point.x},{point.y}"])
            else:
                _run(["cliclick", f"c:{point.x},{point.y}"])
        else:
            _osascript(f'tell application "System Events" to click at {{{point.x}, {point.y}}}')
        time.sleep(0.05)

    def _get_cursor_pos(self) -> tuple[int, int] | None:
        if self._has_cliclick:
            with contextlib.suppress(Exception):
                output = _run(["cliclick", "p:."])
                parts = output.strip().split(",")
                if len(parts) >= _MIN_COORD_PARTS:
                    return (int(parts[0]), int(parts[1]))
        return None

    # ------------------------------------------------------------------
    # Keyboard helpers
    # ------------------------------------------------------------------

    def _send_hotkey(self, keys: list[str]) -> None:
        modifiers = []
        key_codes = []
        for key in keys:
            lower = key.lower()
            if lower in MACOS_MODIFIER_FLAGS:
                modifiers.append(MACOS_MODIFIER_FLAGS[lower])
            elif lower in MACOS_KEYS:
                key_codes.append(MACOS_KEYS[lower])
            elif len(lower) == 1:
                escaped = lower.replace("\\", "\\\\").replace('"', '\\"')
                using = " using {" + ", ".join(modifiers) + "}" if modifiers else ""
                _osascript(f'tell application "System Events" to keystroke "{escaped}"{using}')
                return
            else:
                logger.warning("computer_use: unsupported key {!r} on macOS, skipping", key)
                continue

        if key_codes:
            for kc in key_codes:
                if kc < 0:
                    continue
                using = " using {" + ", ".join(modifiers) + "}" if modifiers else ""
                _osascript(f'tell application "System Events" to key code {kc}{using}')
        elif modifiers and not key_codes:
            logger.warning("computer_use: modifier-only hotkey, no key to press")

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

        with self._lock:
            state = self._state.get(session.session_id)
            if state is None:
                return None
            state["observation_index"] += 1
            obs_index: int = state["observation_index"]
            artifact_dir: Path = state["artifact_dir"]

        artifact_path = artifact_dir / f"obs_{obs_index:04d}.png"
        metadata: dict[str, Any] = {"active_window_only": request.active_window_only, "mode": request.mode.value}

        if request.active_window_only and foreground is not None:
            wid = str(foreground.hwnd)
            _run(["screencapture", "-l", wid, str(artifact_path)])
        else:
            _run(["screencapture", "-x", str(artifact_path)])

        if not artifact_path.exists():
            return None

        image = Image.open(artifact_path)

        if self._image_is_probably_blank(image):
            metadata["warning"] = "blank_screenshot_detected"

        max_dim = request.screenshot_max_dimension
        if max_dim is not None and max_dim > 0:
            w, h = image.size
            longest = max(w, h)
            if longest > max_dim:
                scale = max_dim / longest
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
                metadata["resized_from"] = {"width": w, "height": h}
                metadata["resized_to"] = {"width": new_w, "height": new_h}

        fmt = request.screenshot_format.upper()
        if fmt in ("JPEG", "JPG"):
            fmt = "JPEG"
            ext = ".jpg"
            mime = "image/jpeg"
            save_kwargs: dict[str, Any] = {"quality": request.screenshot_quality}
            if image.mode in ("RGBA", "LA", "PA"):
                image = image.convert("RGB")
        else:
            fmt = "PNG"
            ext = ".png"
            mime = "image/png"
            save_kwargs = {}

        artifact_path = artifact_path.with_suffix(ext)
        image.save(artifact_path, format=fmt, **save_kwargs)
        metadata["format"] = fmt.lower()
        return ComputerArtifact(
            kind="screenshot",
            mime_type=mime,
            path=str(artifact_path),
            metadata=metadata,
        )

    @staticmethod
    def _image_is_probably_blank(image: Image.Image) -> bool:
        return image.getbbox() is None

    # ------------------------------------------------------------------
    # Viewport / window helpers
    # ------------------------------------------------------------------

    def _get_viewport(self) -> ComputerViewport:
        with contextlib.suppress(Exception):
            output = _osascript('tell application "Finder" to get bounds of window of desktop')
            parts = [p.strip() for p in output.split(",")]
            if len(parts) >= _MIN_BOUNDS_PARTS:
                return ComputerViewport(width=int(parts[2]), height=int(parts[3]), pixel_ratio=2.0)
        with contextlib.suppress(Exception):
            output = _run(["system_profiler", "SPDisplaysDataType"])
            for line in output.splitlines():
                if "Resolution" in line:
                    parts = line.split(":")[-1].strip().split()
                    if len(parts) >= _MIN_RESOLUTION_PARTS and parts[1].lower() == "x":
                        return ComputerViewport(
                            width=int(parts[0]),
                            height=int(parts[2]),
                            pixel_ratio=2.0 if "Retina" in line else 1.0,
                        )
        return ComputerViewport(width=1440, height=900, pixel_ratio=2.0)

    def _get_foreground_window_info(self) -> WindowInfo | None:
        with contextlib.suppress(Exception):
            app_name = _osascript(
                'tell application "System Events" to get name of first application process whose frontmost is true'
            )
            if not app_name.strip():
                return None
            title = app_name
            with contextlib.suppress(Exception):
                title = _osascript(f'tell application "{app_name}" to get name of front window')
            bounds = self._get_front_window_bounds(app_name)
            hwnd = abs(hash(app_name)) % 1_000_000_000
            return WindowInfo(
                hwnd=hwnd,
                title=title.strip(),
                class_name=app_name.strip(),
                bounds=bounds,
                is_foreground=True,
            )
        return None

    def _get_front_window_bounds(self, app_name: str) -> ComputerBounds | None:
        with contextlib.suppress(Exception):
            pos = _osascript(f'tell application "{app_name}" to get position of front window')
            size = _osascript(f'tell application "{app_name}" to get size of front window')
            pos_parts = [int(p.strip()) for p in pos.split(",")]
            size_parts = [int(p.strip()) for p in size.split(",")]
            if len(pos_parts) >= _MIN_COORD_PARTS and len(size_parts) >= _MIN_COORD_PARTS:
                return ComputerBounds(
                    x=pos_parts[0],
                    y=pos_parts[1],
                    width=size_parts[0],
                    height=size_parts[1],
                )
        return None

    def _list_windows(self, max_windows: int) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        with contextlib.suppress(Exception):
            output = _osascript(
                'tell application "System Events" to get name of every application process whose visible is true'
            )
            app_names = [name.strip() for name in output.split(",")]
            active = self._get_foreground_window_info()
            for app_name in app_names[:max_windows]:
                if not app_name.strip():
                    continue
                title = app_name
                with contextlib.suppress(Exception):
                    title = _osascript(f'tell application "{app_name}" to get name of front window')
                bounds = self._get_front_window_bounds(app_name)
                hwnd = abs(hash(app_name)) % 1_000_000_000
                is_fg = active is not None and active.class_name == app_name
                windows.append(
                    WindowInfo(
                        hwnd=hwnd,
                        title=title.strip(),
                        class_name=app_name.strip(),
                        bounds=bounds,
                        is_foreground=is_fg,
                    )
                )
        return windows

    def _find_app_by_window_title(self, title: str) -> str | None:
        lowered = title.lower()
        with contextlib.suppress(Exception):
            output = _osascript(
                'tell application "System Events" to get name of every application process whose visible is true'
            )
            for raw_app_name in output.split(","):
                app_name = raw_app_name.strip()
                if not app_name:
                    continue
                if lowered in app_name.lower():
                    return app_name
                with contextlib.suppress(Exception):
                    win_title = _osascript(f'tell application "{app_name}" to get name of front window')
                    if lowered in win_title.lower():
                        return app_name
        return None

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
    # Text extraction helpers
    # ------------------------------------------------------------------

    def _extract_text(self, action: ComputerAction) -> str:
        strategy = str(action.metadata.get("strategy", "clipboard"))
        if strategy == "selection_copy":
            self._send_hotkey(["cmd", "c"])
            time.sleep(0.1)
            return self._read_clipboard_text() or ""
        if strategy == "window_title":
            foreground = self._get_foreground_window_info()
            return foreground.title if foreground else ""
        if strategy == "ocr":
            return self._ocr_extract(action)
        return self._read_clipboard_text() or ""

    def _ocr_extract(self, action: ComputerAction) -> str:
        try:
            import pytesseract
        except ImportError:
            logger.warning(
                "computer_use: pytesseract not installed — falling back to clipboard. "
                "Install with: pip install pytesseract"
            )
            return self._read_clipboard_text() or ""

        from PIL import ImageGrab

        image = ImageGrab.grab()
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

    def _read_clipboard_text(self) -> str | None:
        with contextlib.suppress(Exception):
            return _osascript("the clipboard")
        return None
