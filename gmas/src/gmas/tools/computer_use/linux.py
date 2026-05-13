"""
Linux native computer-use runtime.

Provides full desktop automation on Linux: mouse, keyboard, screenshots,
window management, and clipboard access via Xlib/xdotool/xclip and Pillow.

Requirements:
    pip install pillow python-xlib  (or: pip install "frontier-ai-gmas[computer-use]")

System packages (Debian/Ubuntu):
    sudo apt install xdotool xclip xsel tesseract-ocr scrot
"""

import concurrent.futures
import contextlib
import os
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
# Key name map — maps friendly names to X11 keysym names
# ------------------------------------------------------------------

_MIN_GEOM_PARTS = 2

X11_KEYS: dict[str, str] = {
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    "shift": "shift",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "super": "super",
    "win": "super",
    "meta": "super",
    "f1": "F1",
    "f2": "F2",
    "f3": "F3",
    "f4": "F4",
    "f5": "F5",
    "f6": "F6",
    "f7": "F7",
    "f8": "F8",
    "f9": "F9",
    "f10": "F10",
    "f11": "F11",
    "f12": "F12",
}

# ------------------------------------------------------------------
# Security constants
# ------------------------------------------------------------------

_DANGEROUS_APPS: frozenset[str] = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "fish",
        "dash",
        "csh",
        "tcsh",
        "ksh",
        "python",
        "python3",
        "perl",
        "ruby",
        "node",
        "sudo",
        "su",
        "pkexec",
        "doas",
    }
)

_BLOCKED_PATH_FRAGMENTS: frozenset[str] = frozenset(
    {
        "/etc/shadow",
        "/etc/passwd",
        "/proc/",
        "/sys/",
        "/dev/",
        "/boot/",
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


def _run_ok(args: list[str], *, timeout: float = 10.0) -> bool:
    try:
        _subprocess_run(args, capture_output=True, timeout=timeout, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return True


def _has_command(name: str) -> bool:
    return shutil.which(name) is not None


# ------------------------------------------------------------------
# Runtime
# ------------------------------------------------------------------


class LinuxComputerRuntime(ComputerRuntime):
    """
    Desktop runtime backed by native Linux/X11 tools.

    Uses xdotool for mouse/keyboard/window management, scrot/import for
    screenshots, xclip for clipboard, and pytesseract for OCR.

    Thread-safety
    -------------
    ``_lock`` (RLock) protects all mutations of ``_state``.
    IO-bound operations (screenshot capture, xdotool calls) are performed
    outside the lock.
    """

    @staticmethod
    def is_available() -> bool:
        # Binaries alone are not enough: headless CI images often ship scrot/import
        # without an X server, so screenshots fail at runtime. Require a display.
        if not (_has_command("scrot") or _has_command("import")):
            return False
        return bool(os.environ.get("DISPLAY", "").strip())

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._has_xdotool = _has_command("xdotool")
        self._has_xclip = _has_command("xclip")
        self._has_xsel = _has_command("xsel")
        self._has_scrot = _has_command("scrot")
        self._has_import = _has_command("import")  # ImageMagick
        self._has_xrandr = _has_command("xrandr")
        self._has_wmctrl = _has_command("wmctrl")
        self._has_xprop = _has_command("xprop")

        if not self._has_xdotool:
            logger.warning(
                "computer_use: xdotool not found — mouse/keyboard/window actions will fail. "
                "Install with: sudo apt install xdotool"
            )

    # ------------------------------------------------------------------
    # ComputerRuntime interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "linux_native"

    def capabilities(self) -> ComputerRuntimeCapabilities:
        return ComputerRuntimeCapabilities(
            supports_browser=False,
            supports_desktop=True,
            supports_semantic_targeting=False,
            supports_screenshots=self._has_scrot or self._has_import,
            supports_text_extraction=True,
            supports_keyboard=self._has_xdotool,
            supports_mouse=self._has_xdotool,
            supports_downloads=False,
            supports_windows=self._has_xdotool,
            supports_window_management=self._has_xdotool,
            supports_clipboard=self._has_xclip or self._has_xsel,
            metadata={
                "runtime": self.name,
                "xdotool": self._has_xdotool,
                "scrot": self._has_scrot,
                "xclip": self._has_xclip,
                "wmctrl": self._has_wmctrl,
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
                _subprocess_popen(["xdg-open", config.start_url])
                time.sleep(1.0)
        return ComputerSession(
            session_id=config.session_id,
            runtime_name=self.name,
            max_steps=config.max_steps,
            safety_mode=config.safety_mode,
            artifact_root=artifact_dir,
            observation_request=config.observation,
            metadata={"os_name": "linux", **config.metadata},
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
        self._move_cursor(point)
        if action.action_type == ComputerActionType.HOVER:
            return f"hovered at {point.x},{point.y}"
        button_num = {MouseButton.LEFT: "1", MouseButton.RIGHT: "3", MouseButton.MIDDLE: "2"}
        btn = MouseButton.RIGHT if action.action_type == ComputerActionType.RIGHT_CLICK else action.button
        clicks = 2 if action.action_type == ComputerActionType.DOUBLE_CLICK else 1
        for _ in range(clicks):
            _run(["xdotool", "click", button_num[btn]])
            time.sleep(0.05)
        return f"clicked {btn.value} at {point.x},{point.y}"

    def _do_drag(self, _session: ComputerSession, action: ComputerAction) -> str:
        start = self._resolve_target(action.target)
        end = self._resolve_target(action.end_target)
        if start is None or end is None:
            msg = "drag requires target and end_target"
            raise ValueError(msg)
        self._move_cursor(start)
        _run(["xdotool", "mousedown", "1"])
        time.sleep(0.05)
        _run(["xdotool", "mousemove", "--sync", str(end.x), str(end.y)])
        _run(["xdotool", "mouseup", "1"])
        return f"dragged from {start.x},{start.y} to {end.x},{end.y}"

    def _do_scroll(self, _session: ComputerSession, action: ComputerAction) -> str:
        if action.target is not None:
            point = self._resolve_target(action.target)
            if point is not None:
                self._move_cursor(point)
        dy = action.delta_y or 0
        if dy > 0:
            for _ in range(abs(dy)):
                _run(["xdotool", "click", "5"])
        elif dy < 0:
            for _ in range(abs(dy)):
                _run(["xdotool", "click", "4"])
        dx = action.delta_x or 0
        if dx > 0:
            for _ in range(abs(dx)):
                _run(["xdotool", "click", "7"])
        elif dx < 0:
            for _ in range(abs(dx)):
                _run(["xdotool", "click", "6"])
        return f"scrolled by ({dx}, {dy})"

    def _do_type(self, _session: ComputerSession, action: ComputerAction) -> str:
        if action.target is not None:
            point = self._resolve_target(action.target)
            if point is not None:
                self._move_cursor(point)
                _run(["xdotool", "click", "1"])
        text = action.text or ""
        _run(["xdotool", "type", "--clearmodifiers", "--delay", "5", text])
        return f"typed {len(text)} chars"

    def _do_hotkey(self, _session: ComputerSession, action: ComputerAction) -> str:
        if not action.keys:
            msg = "hotkey requires keys"
            raise ValueError(msg)
        combo = "+".join(self._resolve_key(k) for k in action.keys)
        _run(["xdotool", "key", "--clearmodifiers", combo])
        return f"pressed hotkey {'+'.join(action.keys)}"

    def _do_key_press(self, _session: ComputerSession, action: ComputerAction) -> str:
        if not action.keys:
            msg = "key_press requires keys"
            raise ValueError(msg)
        for key in action.keys:
            _run(["xdotool", "key", "--clearmodifiers", self._resolve_key(key)])
        return f"pressed keys {','.join(action.keys)}"

    def _do_wait(self, _session: ComputerSession, action: ComputerAction) -> str:
        time.sleep((action.wait_ms or 0) / 1000)
        return f"waited {action.wait_ms} ms"

    def _do_navigate(self, _session: ComputerSession, action: ComputerAction) -> str:
        if not action.url:
            msg = "navigate requires url"
            raise ValueError(msg)
        _subprocess_popen(["xdg-open", action.url])
        return f"navigated to {action.url}"

    def _do_open_app(self, session: ComputerSession, action: ComputerAction) -> str:
        target_path = action.path or action.text
        if not target_path:
            msg = "open_app requires path or text"
            raise ValueError(msg)
        argv = [target_path, *action.arguments]
        process = _subprocess_popen(argv)
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
        wid = self._find_window_by_title(title)
        if wid is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        _run(["xdotool", "windowactivate", "--sync", wid])
        return f"focused window {title}"

    def _do_resize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "resize_window requires title in text or metadata.title"
            raise ValueError(msg)
        wid = self._find_window_by_title(title)
        if wid is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        width = action.width
        height = action.height
        if width is None or height is None:
            msg = "resize_window requires width and height"
            raise ValueError(msg)
        _run(["xdotool", "windowsize", wid, str(width), str(height)])
        return f"resized window {title!r} to {width}×{height}"

    def _do_minimize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "minimize_window requires title in text or metadata.title"
            raise ValueError(msg)
        wid = self._find_window_by_title(title)
        if wid is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        _run(["xdotool", "windowminimize", wid])
        return f"minimized window {title!r}"

    def _do_maximize_window(self, _session: ComputerSession, action: ComputerAction) -> str:
        title = action.text or action.metadata.get("title")
        if not title:
            msg = "maximize_window requires title in text or metadata.title"
            raise ValueError(msg)
        wid = self._find_window_by_title(title)
        if wid is None:
            msg = f"window not found: {title}"
            raise ValueError(msg)
        if self._has_wmctrl:
            _run(["wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"])
        else:
            _run(["xdotool", "windowactivate", "--sync", wid])
            _run(["xdotool", "key", "super+Up"])
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
                frozenset(["alt", "f4"]),
                frozenset(["ctrl", "alt", "delete"]),
                frozenset(["ctrl", "alt", "backspace"]),
                frozenset(["super", "l"]),
            }
            normalized = frozenset(key.lower() for key in action.keys)
            if normalized in dangerous_hotkeys:
                return "dangerous hotkey blocked by safety policy"

        if session.safety_mode == SafetyMode.PROMPT:
            if action.action_type == ComputerActionType.OPEN_APP:
                target = (action.path or action.text or "").lower()
                app_name = Path(target).name.strip()
                if app_name in _DANGEROUS_APPS:
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

    def _move_cursor(self, point: ComputerCoordinate) -> None:
        _run(["xdotool", "mousemove", "--sync", str(point.x), str(point.y)])
        time.sleep(0.05)

    def _get_cursor_pos(self) -> tuple[int, int] | None:
        with contextlib.suppress(ValueError, OSError, FileNotFoundError, subprocess.SubprocessError):
            output = _run(["xdotool", "getmouselocation", "--shell"])
            coords: dict[str, int] = {}
            for line in output.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k in ("X", "Y"):
                        coords[k] = int(v)
            if "X" in coords and "Y" in coords:
                return (coords["X"], coords["Y"])
        return None

    # ------------------------------------------------------------------
    # Keyboard helpers
    # ------------------------------------------------------------------

    def _resolve_key(self, key: str) -> str:
        normalized = key.lower()
        if normalized in X11_KEYS:
            return X11_KEYS[normalized]
        return key

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

        if request.active_window_only and foreground is not None and self._has_xdotool:
            wid = str(foreground.hwnd)
            if self._has_import:
                _run(["import", "-window", wid, str(artifact_path)])
            elif self._has_scrot:
                _run(["scrot", "-u", str(artifact_path)])
            else:
                self._grab_full_screen(artifact_path)
                metadata["fallback"] = "full_screen"
        elif self._has_scrot:
            _run(["scrot", str(artifact_path)])
        elif self._has_import:
            _run(["import", "-window", "root", str(artifact_path)])
        else:
            self._grab_full_screen(artifact_path)

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
    def _grab_full_screen(path: Path) -> None:
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab()
            image.save(path, format="PNG")
        except (OSError, AttributeError, ImportError) as exc:
            msg = "No screenshot tool available. Install scrot or ImageMagick: sudo apt install scrot"
            raise RuntimeError(msg) from exc

    @staticmethod
    def _image_is_probably_blank(image: Image.Image) -> bool:
        return image.getbbox() is None

    # ------------------------------------------------------------------
    # Viewport / window helpers
    # ------------------------------------------------------------------

    def _get_viewport(self) -> ComputerViewport:
        if self._has_xrandr:
            with contextlib.suppress(Exception):
                output = _run(["xrandr", "--current"])
                for line in output.splitlines():
                    if " connected " in line and "x" in line:
                        for part in line.split():
                            if "x" in part and part[0].isdigit():
                                dims = part.split("+")[0]
                                w, h = dims.split("x")
                                return ComputerViewport(width=int(w), height=int(h), pixel_ratio=1.0)
        if self._has_xdotool:
            with contextlib.suppress(Exception):
                output = _run(["xdotool", "getdisplaygeometry"])
                parts = output.split()
                if len(parts) >= _MIN_GEOM_PARTS:
                    return ComputerViewport(width=int(parts[0]), height=int(parts[1]), pixel_ratio=1.0)
        return ComputerViewport(width=1920, height=1080, pixel_ratio=1.0)

    def _get_foreground_window_info(self) -> WindowInfo | None:
        if not self._has_xdotool:
            return None
        with contextlib.suppress(Exception):
            wid = _run(["xdotool", "getactivewindow"])
            if not wid.strip():
                return None
            return self._window_info(wid.strip(), is_foreground=True)
        return None

    def _window_info(self, wid: str, is_foreground: bool = False) -> WindowInfo | None:
        title = ""
        class_name = None
        bounds = None
        with contextlib.suppress(Exception):
            title = _run(["xdotool", "getwindowname", wid])
        with contextlib.suppress(Exception):
            geom = _run(["xdotool", "getwindowgeometry", "--shell", wid])
            coords: dict[str, int] = {}
            for line in geom.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    with contextlib.suppress(ValueError):
                        coords[k] = int(v)
            if "X" in coords and "Y" in coords and "WIDTH" in coords and "HEIGHT" in coords:
                bounds = ComputerBounds(
                    x=coords["X"],
                    y=coords["Y"],
                    width=coords["WIDTH"],
                    height=coords["HEIGHT"],
                )
        if self._has_xprop:
            with contextlib.suppress(Exception):
                output = _run(["xprop", "-id", wid, "WM_CLASS"])
                if "=" in output:
                    class_name = output.split("=", 1)[1].strip().strip('"')

        hwnd = int(wid) if wid.isdigit() else abs(hash(wid)) % 1_000_000_000
        return WindowInfo(
            hwnd=hwnd,
            title=title,
            class_name=class_name,
            bounds=bounds,
            is_foreground=is_foreground,
        )

    def _list_windows(self, max_windows: int) -> list[WindowInfo]:
        if not self._has_xdotool:
            return []
        windows: list[WindowInfo] = []
        with contextlib.suppress(Exception):
            active_wid = _run(["xdotool", "getactivewindow"]).strip()
            output = _run(["xdotool", "search", "--onlyvisible", "--name", ""])
            for raw_wid in output.splitlines()[:max_windows]:
                wid = raw_wid.strip()
                if not wid:
                    continue
                info = self._window_info(wid, is_foreground=wid == active_wid)
                if info is not None and info.title.strip():
                    windows.append(info)
        return windows

    def _find_window_by_title(self, title: str) -> str | None:
        if not self._has_xdotool:
            return None
        with contextlib.suppress(Exception):
            output = _run(["xdotool", "search", "--name", title])
            for line in output.splitlines():
                wid = line.strip()
                if wid:
                    return wid
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
            _run(["xdotool", "key", "--clearmodifiers", "ctrl+c"])
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
        if self._has_xclip:
            with contextlib.suppress(Exception):
                return _run(["xclip", "-selection", "clipboard", "-o"]) or None
        if self._has_xsel:
            with contextlib.suppress(Exception):
                return _run(["xsel", "--clipboard", "--output"]) or None
        return None
