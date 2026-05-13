"""
Computer-use tool client adapter, JSON-schema builder, and multimodal helpers.

Provides:
- ``build_computer_use_tool_schema``  — simplified, LLM-friendly schema.
- ``build_computer_use_full_schema``  — full Pydantic-generated schema for
  programmatic validation.
- ``artifact_to_base64_url``          — convert a screenshot artifact to a
  ``data:`` URL for multimodal LLM messages.
- ``observation_to_openai_content``   — convert an observation to an
  OpenAI-compatible multimodal content list.
- ``ComputerUseClient``               — thin wrapper around the controller for
  standalone use outside the framework tools layer.
"""

import base64
import json
from pathlib import Path
from typing import Any

from .controller import ComputerUseController
from .models import ComputerArtifact, ComputerObservation, ComputerUseCommand, ComputerUseResponse

# ---------------------------------------------------------------------------
# Tool description (shared by schema builder and client wrapper)
# ---------------------------------------------------------------------------

_DESCRIPTION = (
    "Operate a stateful desktop or browser session.\n"
    "\n"
    "Operations:\n"
    "- start: Open a new session. Supply 'config' with runtime settings and an optional "
    "'start_url'. Returns 'session_id', an initial observation, and runtime capabilities.\n"
    "- observe: Read the current screen state (screenshot, windows, visible elements, text). "
    "Supply 'session_id'. Optionally include 'observation' to control what is captured.\n"
    "- act: Execute one interaction. Supply 'session_id' and 'action' with an 'action_type'.\n"
    "  Supported action types: click, double_click, right_click, hover, drag, scroll, type,\n"
    "  hotkey, key_press, wait, navigate, open_app, focus_window, resize_window,\n"
    "  minimize_window, maximize_window, screenshot, extract_text.\n"
    "- close: End the session and release all resources. Supply 'session_id'.\n"
    "\n"
    "Use the 'session_id' from the start response in every subsequent call. "
    "Inspect 'observation' in each response to understand the current screen state before acting.\n"
    "\n"
    "Screenshots can be returned as PNG (lossless) or JPEG (lossy, smaller). "
    "Set 'screenshot_format' in the observation config to choose.\n"
    "\n"
    "Text extraction supports strategies: 'clipboard' (default), 'selection_copy', "
    "'window_title', and 'ocr' (requires Tesseract). "
    "OCR can be scoped to a region via metadata.region: {x, y, width, height}."
)

# ---------------------------------------------------------------------------
# Shared sub-schemas
# ---------------------------------------------------------------------------

_OBSERVATION_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["screenshot_only", "standard", "detailed"],
            "default": "standard",
            "description": (
                "screenshot_only: fastest, only captures a screenshot; "
                "standard: screenshot + text + elements; "
                "detailed: maximum information."
            ),
        },
        "include_screenshot": {"type": "boolean", "default": True},
        "include_text": {"type": "boolean", "default": True},
        "include_dom": {"type": "boolean", "default": True},
        "include_elements": {"type": "boolean", "default": True},
        "include_metadata": {"type": "boolean", "default": True},
        "include_windows": {"type": "boolean", "default": True},
        "include_clipboard": {
            "type": "boolean",
            "default": False,
            "description": "Also capture clipboard text.",
        },
        "active_window_only": {
            "type": "boolean",
            "default": False,
            "description": "Capture screenshot of the active window only.",
        },
        "max_windows": {
            "type": "integer",
            "default": 10,
            "description": "Maximum number of windows to include in the window list.",
        },
        "screenshot_max_dimension": {
            "type": "integer",
            "description": (
                "If set, resize the screenshot so the longest side is at most this many pixels "
                "(preserves aspect ratio).  Useful for keeping base64 payloads small."
            ),
        },
        "screenshot_format": {
            "type": "string",
            "enum": ["png", "jpeg"],
            "default": "png",
            "description": (
                "Image format: 'png' (lossless, default) or 'jpeg' (lossy, much smaller). "
                "JPEG is recommended when screenshots are sent as base64 payloads to LLMs."
            ),
        },
        "screenshot_quality": {
            "type": "integer",
            "default": 85,
            "description": "JPEG quality (1–100). Ignored when format is 'png'.",
        },
    },
}

_TARGET_SCHEMA: dict[str, Any] = {
    "description": (
        "Action target. Use {x, y} for screen pixel coordinates, "
        "or {role, name, selector, bounds} to reference a UI element."
    ),
    "type": "object",
    "properties": {
        "x": {"type": "integer", "description": "Horizontal pixel coordinate."},
        "y": {"type": "integer", "description": "Vertical pixel coordinate."},
        "role": {"type": "string", "description": "Element ARIA role (button, textbox, …)."},
        "name": {"type": "string", "description": "Element accessible name."},
        "selector": {"type": "string", "description": "CSS or XPath selector."},
        "bounds": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Simplified, LLM-friendly schema (returned by build_computer_use_tool_schema)
# ---------------------------------------------------------------------------

_LLM_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "required": ["operation"],
    "additionalProperties": False,
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["start", "observe", "act", "close"],
            "description": (
                "start: open a new session; "
                "observe: read current screen state; "
                "act: execute one action; "
                "close: end the session."
            ),
        },
        "session_id": {
            "type": "string",
            "description": ("Session ID returned by start. Required for observe, act, and close."),
        },
        "config": {
            "type": "object",
            "description": "Session configuration. Required for start.",
            "additionalProperties": False,
            "properties": {
                "start_url": {
                    "type": "string",
                    "description": "URL or file path to open when the session starts.",
                },
                "max_steps": {
                    "type": "integer",
                    "default": 50,
                    "description": "Maximum number of act steps before the session is exhausted.",
                },
                "safety_mode": {
                    "type": "string",
                    "enum": ["prompt", "allowlist", "unrestricted"],
                    "default": "prompt",
                    "description": (
                        "prompt: blocks dangerous hotkeys (Alt+F4, Ctrl+Alt+Delete, Win+L); "
                        "allowlist: only actions listed in metadata.allowed_actions are permitted; "
                        "unrestricted: no restrictions."
                    ),
                },
                "observation": {
                    **_OBSERVATION_REQUEST_SCHEMA,
                    "description": "Default observation settings for every step in this session.",
                },
            },
        },
        "observation": {
            **_OBSERVATION_REQUEST_SCHEMA,
            "description": "One-off observation settings for this observe call.",
        },
        "action": {
            "type": "object",
            "description": "The action to execute. Required for act.",
            "required": ["action_type"],
            "additionalProperties": False,
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": [
                        "click",
                        "double_click",
                        "right_click",
                        "hover",
                        "drag",
                        "scroll",
                        "type",
                        "hotkey",
                        "key_press",
                        "wait",
                        "navigate",
                        "open_app",
                        "focus_window",
                        "resize_window",
                        "minimize_window",
                        "maximize_window",
                        "screenshot",
                        "extract_text",
                    ],
                    "description": (
                        "click/double_click/right_click/hover: require target; "
                        "drag: requires target and end_target; "
                        "scroll: optional target, set delta_y (positive=down, negative=up); "
                        "type: requires text; "
                        "hotkey/key_press: require keys list (e.g. ['ctrl','c']); "
                        "wait: sleeps for wait_ms milliseconds; "
                        "navigate: requires url; "
                        "open_app: requires path or text (app name/path); "
                        "focus_window: requires text (window title substring); "
                        "resize_window: requires text (title) and width/height; "
                        "minimize_window/maximize_window: require text (title); "
                        "screenshot: captures the screen; "
                        "extract_text: extracts text (set metadata.strategy to "
                        "'clipboard', 'selection_copy', 'window_title', or 'ocr')."
                    ),
                },
                "target": _TARGET_SCHEMA,
                "end_target": {
                    **_TARGET_SCHEMA,
                    "description": "Drag end position. Same format as target.",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Text to type (type action), window title substring (focus_window), or app name (open_app)."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": "URL to navigate to (navigate action).",
                },
                "path": {
                    "type": "string",
                    "description": "Executable path or file path (open_app action).",
                },
                "arguments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Command-line arguments for open_app. Example: ['--new-window', 'https://example.com']."
                    ),
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Key names for hotkey or key_press. "
                        "Supported modifiers: ctrl, shift, alt, win. "
                        "Special keys: enter, tab, esc, space, backspace, delete, insert, "
                        "home, end, pageup, pagedown, left, right, up, down, f1-f12. "
                        "Example: ['ctrl', 'c'] for copy."
                    ),
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                    "description": "Mouse button for click and drag actions.",
                },
                "delta_x": {
                    "type": "integer",
                    "description": "Horizontal scroll amount (scroll action).",
                },
                "delta_y": {
                    "type": "integer",
                    "description": (
                        "Vertical scroll amount (scroll action). Positive = scroll down, negative = scroll up."
                    ),
                },
                "width": {
                    "type": "integer",
                    "description": "Target width in pixels (resize_window action).",
                },
                "height": {
                    "type": "integer",
                    "description": "Target height in pixels (resize_window action).",
                },
                "wait_ms": {
                    "type": "integer",
                    "default": 0,
                    "description": (
                        "Milliseconds to wait (wait action), or post-action pause added after any other action type."
                    ),
                },
                "timeout_ms": {
                    "type": "integer",
                    "default": 30000,
                    "description": (
                        "Maximum time in milliseconds the action is allowed to run. "
                        "If exceeded the action fails with a timeout error. "
                        "Set to 0 to disable the timeout."
                    ),
                },
                "metadata": {
                    "type": "object",
                    "description": (
                        "Runtime-specific hints. "
                        "For extract_text, set strategy: 'clipboard', 'selection_copy', "
                        "'window_title', or 'ocr'. "
                        "OCR options: lang (Tesseract language, default 'eng'), "
                        "region: {x, y, width, height} to crop before OCR."
                    ),
                },
            },
        },
    },
}


def build_computer_use_tool_schema() -> dict[str, Any]:
    """
    Return the simplified, LLM-friendly JSON-schema envelope for the tool.

    This schema hides internal fields (session_id generation, artifact paths,
    runtime selection) and provides clear descriptions on every field so that
    LLMs can reliably construct valid tool calls.

    For programmatic validation use ``build_computer_use_full_schema()`` instead.
    """
    return {
        "type": "function",
        "function": {
            "name": "computer_use",
            "description": _DESCRIPTION,
            "parameters": _LLM_PARAMETERS,
        },
    }


def build_computer_use_full_schema() -> dict[str, Any]:
    """
    Return the full Pydantic-generated JSON-schema for ``ComputerUseCommand``.

    Useful for programmatic validation, documentation generation, or when you
    need the complete schema including all internal fields.  For LLM tool
    registration use ``build_computer_use_tool_schema()`` instead.
    """
    return {
        "type": "function",
        "function": {
            "name": "computer_use",
            "description": _DESCRIPTION,
            "parameters": ComputerUseCommand.model_json_schema(),
        },
    }


# ---------------------------------------------------------------------------
# Multimodal / base64 helpers
# ---------------------------------------------------------------------------


def artifact_to_base64_url(artifact: ComputerArtifact) -> str | None:
    """
    Convert a ``ComputerArtifact`` to a ``data:`` URL for multimodal LLM messages.

    Checks ``artifact.content`` first (in-memory string), then falls back to
    reading ``artifact.path`` from disk (binary file).  Returns ``None`` when
    neither source is available or readable.

    Example:
        url = artifact_to_base64_url(observation.screenshot)
        if url:
            messages.append({"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
            ]})

    """
    mime = artifact.mime_type or "application/octet-stream"

    if artifact.content is not None:
        raw = base64.b64encode(artifact.content.encode("utf-8")).decode("ascii")
        return f"data:{mime};base64,{raw}"

    if artifact.path is not None:
        try:
            raw = base64.b64encode(Path(artifact.path).read_bytes()).decode("ascii")
        except OSError:
            return None
        else:
            return f"data:{mime};base64,{raw}"

    return None


def observation_to_openai_content(
    observation: ComputerObservation,
    *,
    max_elements: int = 20,
) -> list[dict[str, Any]]:
    """
    Convert a ``ComputerObservation`` to an OpenAI-compatible multimodal content list.

    The returned list can be used directly as the ``content`` value of a
    ``{"role": "user", ...}`` or ``{"role": "assistant", ...}`` message.

    Args:
        observation: Observation to convert.
        max_elements: Maximum number of UI elements to include in the text
            part (to avoid overflowing the context window).

    Returns:
        List of content blocks.  If a screenshot is present and readable,
        an ``image_url`` block is prepended.  Text metadata (URL, title,
        window, text excerpt, DOM, elements) follows as a ``text`` block.

    Example:
        from openai import OpenAI

        client = OpenAI(...)
        content = observation_to_openai_content(observation)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": content}],
        )

    """
    content: list[dict[str, Any]] = []

    # Screenshot as an image_url block (if available and readable).
    if observation.screenshot is not None:
        url = artifact_to_base64_url(observation.screenshot)
        if url is not None:
            content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})

    # Text metadata block.
    text_parts: list[str] = []

    if observation.url:
        text_parts.append(f"URL: {observation.url}")
    if observation.title:
        text_parts.append(f"Title: {observation.title}")
    if observation.active_window and observation.active_window.title:
        text_parts.append(f"Active window: {observation.active_window.title}")
    if observation.viewport:
        vp = observation.viewport
        text_parts.append(f"Viewport: {vp.width}×{vp.height}")
    if observation.text_excerpt:
        text_parts.append(f"Page text:\n{observation.text_excerpt}")
    if observation.dom_excerpt:
        text_parts.append(f"DOM info: {observation.dom_excerpt}")
    if observation.elements:
        elem_lines = []
        for elem in observation.elements[:max_elements]:
            label = elem.name or elem.text or elem.selector or "?"
            role = elem.role or "element"
            coords = ""
            if elem.bounds:
                coords = f" [{elem.bounds.x},{elem.bounds.y} {elem.bounds.width}×{elem.bounds.height}]"
            elem_lines.append(f"  - {role}: {label}{coords}")
        if len(observation.elements) > max_elements:
            elem_lines.append(f"  … and {len(observation.elements) - max_elements} more element(s)")
        text_parts.append("UI elements:\n" + "\n".join(elem_lines))
    if observation.windows:
        win_lines = [
            f"  - {w.title!r} (hwnd={w.hwnd})" + (" [foreground]" if w.is_foreground else "")
            for w in observation.windows
        ]
        text_parts.append("Open windows:\n" + "\n".join(win_lines))

    if text_parts:
        content.append({"type": "text", "text": "\n".join(text_parts)})

    return content


# ---------------------------------------------------------------------------
# Standalone client wrapper
# ---------------------------------------------------------------------------


class ComputerUseClient:
    """
    Thin client wrapper around the stateful computer-use controller.

    Designed for standalone use (outside the framework tools layer).
    For agent integration use ``ComputerUseTool`` from ``framework.py`` instead.

    Example:
        runtime = MockComputerRuntime()
        client = ComputerUseClient(ComputerUseController(runtime))

        start = client.execute(
            operation="start",
            config={"max_steps": 10},
        )
        client.execute(
            operation="close",
            session_id=start.session.session_id,
        )

    """

    def __init__(self, controller: ComputerUseController) -> None:
        self.controller = controller

    @property
    def name(self) -> str:
        return "computer_use"

    @property
    def description(self) -> str:
        return _DESCRIPTION

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return build_computer_use_tool_schema()["function"]["parameters"]

    def to_openai_schema(self) -> dict[str, Any]:
        """Return the simplified OpenAI function-calling schema for this tool."""
        return build_computer_use_tool_schema()

    def execute(self, **kwargs: Any) -> ComputerUseResponse:
        """Execute a command envelope and return a structured response."""
        return self.controller.handle(ComputerUseCommand(**kwargs))

    def execute_json(self, payload: str) -> ComputerUseResponse:
        """Execute a JSON-encoded command envelope."""
        return self.execute(**json.loads(payload))
