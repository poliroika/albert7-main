"""
Typed models for computer-use sessions, observations, and actions.

Defines the Pydantic models shared by all runtime backends and the controller.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _default_artifact_root() -> Path:
    return Path.cwd() / ".gmas" / "artifacts" / "computer_use"


class SafetyMode(StrEnum):
    """Safety policy for a session."""

    PROMPT = "prompt"
    ALLOWLIST = "allowlist"
    UNRESTRICTED = "unrestricted"


class ObservationMode(StrEnum):
    """Observation payload size policy."""

    SCREENSHOT_ONLY = "screenshot_only"
    STANDARD = "standard"
    DETAILED = "detailed"


class MouseButton(StrEnum):
    """Supported mouse buttons."""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class ComputerUseOperation(StrEnum):
    """High-level commands exposed to an LLM-facing tool."""

    START = "start"
    OBSERVE = "observe"
    ACT = "act"
    CLOSE = "close"


class ComputerActionType(StrEnum):
    """Supported low-level actions."""

    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    HOVER = "hover"
    DRAG = "drag"
    SCROLL = "scroll"
    TYPE = "type"
    HOTKEY = "hotkey"
    KEY_PRESS = "key_press"
    WAIT = "wait"
    NAVIGATE = "navigate"
    OPEN_APP = "open_app"
    FOCUS_WINDOW = "focus_window"
    RESIZE_WINDOW = "resize_window"
    MINIMIZE_WINDOW = "minimize_window"
    MAXIMIZE_WINDOW = "maximize_window"
    SCREENSHOT = "screenshot"
    EXTRACT_TEXT = "extract_text"


class ComputerCoordinate(BaseModel):
    """A screen coordinate."""

    x: int
    y: int


class ComputerBounds(BaseModel):
    """Rectangular bounds on the screen."""

    x: int
    y: int
    width: int
    height: int


class UIElementRef(BaseModel):
    """A semantic reference to a UI element."""

    element_id: str | None = None
    role: str | None = None
    name: str | None = None
    text: str | None = None
    selector: str | None = None
    bounds: ComputerBounds | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputerViewport(BaseModel):
    """Viewport metadata."""

    width: int
    height: int
    pixel_ratio: float = 1.0


class WindowInfo(BaseModel):
    """Window metadata for the current desktop state."""

    hwnd: int
    title: str
    class_name: str | None = None
    bounds: ComputerBounds | None = None
    is_foreground: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputerArtifact(BaseModel):
    """Artifact produced by the runtime."""

    artifact_id: str = Field(default_factory=lambda: _new_id("artifact"))
    kind: str
    mime_type: str | None = None
    path: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservationRequest(BaseModel):
    """Controls how much state the runtime should capture."""

    model_config = ConfigDict(extra="forbid")
    mode: ObservationMode = ObservationMode.STANDARD
    include_screenshot: bool = True
    include_text: bool = True
    include_dom: bool = True
    include_elements: bool = True
    include_metadata: bool = True
    include_windows: bool = True
    include_clipboard: bool = False
    active_window_only: bool = False
    max_windows: int = 10
    screenshot_max_dimension: int | None = None
    """If set, the longest side of the screenshot will be resized to this value
    (aspect-ratio preserved) before saving.  Useful for keeping base64 payloads
    within LLM context-window limits.  ``None`` means no resizing."""
    screenshot_format: str = "png"
    """Image format for saved screenshots: ``"png"`` (lossless, default) or
    ``"jpeg"`` (lossy, much smaller).  JPEG is recommended when screenshots
    are sent as base64 payloads to LLMs."""
    screenshot_quality: int = 85
    """JPEG quality (1–100).  Ignored when ``screenshot_format`` is ``"png"``."""


class ComputerObservation(BaseModel):
    """What the runtime observed after a step."""

    url: str | None = None
    title: str | None = None
    viewport: ComputerViewport | None = None
    screenshot: ComputerArtifact | None = None
    dom_excerpt: str | None = None
    text_excerpt: str | None = None
    elements: list[UIElementRef] = Field(default_factory=list)
    active_window: WindowInfo | None = None
    windows: list[WindowInfo] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputerAction(BaseModel):
    """One requested action."""

    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(default_factory=lambda: _new_id("action"))
    action_type: ComputerActionType
    target: ComputerCoordinate | UIElementRef | None = None
    end_target: ComputerCoordinate | UIElementRef | None = None
    text: str | None = None
    url: str | None = None
    path: str | None = None
    arguments: list[str] = Field(default_factory=list)
    keys: list[str] = Field(default_factory=list)
    button: MouseButton = MouseButton.LEFT
    delta_x: int | None = None
    delta_y: int | None = None
    width: int | None = None
    height: int | None = None
    wait_ms: int = 0
    timeout_ms: int = 30000
    """Maximum time (in milliseconds) the action is allowed to run before being
    treated as failed.  Default is 30 000 ms (30 s).  Set to 0 to disable."""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputerActionResult(BaseModel):
    """Result of executing one action."""

    action_id: str
    success: bool = True
    summary: str = ""
    error: str | None = None
    observation: ComputerObservation | None = None
    artifacts: list[ComputerArtifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputerRuntimeCapabilities(BaseModel):
    """Declared runtime capabilities for planning and validation."""

    supports_browser: bool = False
    supports_desktop: bool = False
    supports_semantic_targeting: bool = False
    supports_screenshots: bool = True
    supports_text_extraction: bool = False
    supports_keyboard: bool = True
    supports_mouse: bool = True
    supports_downloads: bool = False
    supports_windows: bool = False
    supports_window_management: bool = False
    supports_clipboard: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputerSessionConfig(BaseModel):
    """Configuration for starting a session."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    session_id: str = Field(default_factory=lambda: _new_id("session"))
    runtime_name: str = "mock"
    os_name: str = "windows"
    start_url: str | None = None
    headless: bool = False
    artifact_root: Path = Field(default_factory=_default_artifact_root)
    max_steps: int = 50
    safety_mode: SafetyMode = SafetyMode.PROMPT
    observation: ObservationRequest = Field(default_factory=ObservationRequest)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputerSession(BaseModel):
    """
    Immutable snapshot of a running computer-use session.

    All updates are performed via ``model_copy(update=...)`` — direct
    mutation of any field is prevented by ``frozen=True``.  The
    ``history`` field is a tuple so that accidental in-place mutation
    (e.g. ``.append()``) raises an ``AttributeError`` immediately.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    session_id: str
    runtime_name: str
    status: str = "open"
    step_count: int = 0
    max_steps: int = 50
    safety_mode: SafetyMode = SafetyMode.PROMPT
    artifact_root: Path = Field(default_factory=_default_artifact_root)
    observation_request: ObservationRequest = Field(default_factory=ObservationRequest)
    last_observation: ComputerObservation | None = None
    history: tuple[ComputerActionResult, ...] = Field(default_factory=tuple)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputerUseCommand(BaseModel):
    """Single command envelope to expose as a tool."""

    model_config = ConfigDict(extra="forbid")

    operation: ComputerUseOperation
    session_id: str | None = None
    config: ComputerSessionConfig | None = None
    observation: ObservationRequest | None = None
    action: ComputerAction | None = None


class ComputerUseResponse(BaseModel):
    """Response returned by the controller."""

    success: bool = True
    session: ComputerSession | None = None
    observation: ComputerObservation | None = None
    action_result: ComputerActionResult | None = None
    capabilities: ComputerRuntimeCapabilities | None = None
    artifacts: list[ComputerArtifact] = Field(default_factory=list)
    error: str | None = None
