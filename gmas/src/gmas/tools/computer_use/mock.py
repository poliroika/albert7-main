"""
Mock computer-use runtime for tests and bootstrap examples.

Provides a fully deterministic, in-memory runtime that implements the complete
ComputerRuntime interface without requiring any real system resources.
All capabilities are reported as supported so that prompt/schema tests work
without a live desktop session.

State is cleaned up on close_session so the mock is safe for long-running
test suites without accumulating memory.
"""

from pathlib import Path
from typing import Any

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
    ObservationMode,
    ObservationRequest,
    UIElementRef,
    WindowInfo,
)
from .runtime import ComputerRuntime


class MockComputerRuntime(ComputerRuntime):
    """Deterministic runtime used for bootstrap examples and tests."""

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "mock"

    def capabilities(self) -> ComputerRuntimeCapabilities:
        return ComputerRuntimeCapabilities(
            supports_browser=True,
            supports_desktop=True,
            supports_semantic_targeting=True,
            supports_screenshots=True,
            supports_text_extraction=True,
            supports_keyboard=True,
            supports_mouse=True,
            supports_downloads=True,
            supports_windows=True,
            supports_window_management=True,
            supports_clipboard=True,
            metadata={"runtime": self.name},
        )

    def start_session(self, config: ComputerSessionConfig) -> ComputerSession:
        initial_url = config.start_url or "about:blank"
        self._state[config.session_id] = {
            "url": initial_url,
            "title": "Mock Desktop",
            "typed_text": "",
            "last_action": "session started",
            "viewport": {"width": 1280, "height": 800, "pixel_ratio": 1.0},
            "artifact_root": Path(config.artifact_root),
            "window_title": "Mock Browser",
            "window_hwnd": 123,
        }
        return ComputerSession(
            session_id=config.session_id,
            runtime_name=self.name,
            max_steps=config.max_steps,
            safety_mode=config.safety_mode,
            artifact_root=config.artifact_root,
            observation_request=config.observation,
            metadata={"os_name": config.os_name, **config.metadata},
        )

    def get_observation(
        self,
        session: ComputerSession,
        request: ObservationRequest | None = None,
    ) -> ComputerObservation:
        state = self._state[session.session_id]
        request = request or session.observation_request
        screenshot = None
        text_excerpt = None
        dom_excerpt = None
        elements: list[UIElementRef] = []
        metadata: dict[str, Any] = {}
        bounds = ComputerBounds(x=50, y=60, width=900, height=700)
        active_window = WindowInfo(
            hwnd=state["window_hwnd"],
            title=state["window_title"],
            class_name="MockWindow",
            bounds=bounds,
            is_foreground=True,
        )

        if request.include_screenshot:
            screenshot = ComputerArtifact(
                kind="screenshot",
                mime_type="image/png",
                path=str(state["artifact_root"] / f"{session.session_id}_latest.png"),
                metadata={"runtime": self.name, "mode": request.mode.value},
            )

        if request.include_text:
            text_excerpt = state["typed_text"] or ""

        if request.include_dom:
            dom_excerpt = f"last_action={state['last_action']}"
            if request.mode == ObservationMode.DETAILED:
                dom_excerpt += f"; url={state['url']}"

        if request.include_elements:
            elements = [
                UIElementRef(role="window", name=state["window_title"], bounds=bounds),
                UIElementRef(role="textbox", name="Search", selector="input[name=q]"),
                UIElementRef(role="button", name="Submit", selector="button[type=submit]"),
            ]

        if request.include_metadata:
            metadata = {
                "runtime": self.name,
                "window_title": state["window_title"],
            }

        windows = [active_window] if request.include_windows else []

        return ComputerObservation(
            url=state["url"],
            title=state["title"],
            viewport=ComputerViewport(**state["viewport"]),
            screenshot=screenshot,
            text_excerpt=text_excerpt,
            dom_excerpt=dom_excerpt,
            elements=elements,
            active_window=active_window,
            windows=windows,
            metadata=metadata,
        )

    def execute(self, session: ComputerSession, action: ComputerAction) -> ComputerActionResult:
        state = self._state[session.session_id]
        summary = f"{action.action_type.value} executed"  # safe fallback

        # --- Navigation ---
        if action.action_type == ComputerActionType.NAVIGATE and action.url:
            state["url"] = action.url
            state["title"] = f"Page: {action.url}"
            summary = f"navigated to {action.url}"

        # --- Mouse actions ---
        elif action.action_type in {
            ComputerActionType.CLICK,
            ComputerActionType.DOUBLE_CLICK,
            ComputerActionType.RIGHT_CLICK,
            ComputerActionType.HOVER,
        }:
            label = self._target_label(action.target)
            verb = {
                ComputerActionType.CLICK: "clicked",
                ComputerActionType.DOUBLE_CLICK: "double-clicked",
                ComputerActionType.RIGHT_CLICK: "right-clicked",
                ComputerActionType.HOVER: "hovered over",
            }[action.action_type]
            suffix = " (x2)" if action.action_type == ComputerActionType.DOUBLE_CLICK else ""
            summary = f"{verb} {label}{suffix}"

        elif action.action_type == ComputerActionType.DRAG:
            start = self._target_label(action.target)
            end = self._target_label(action.end_target)
            summary = f"dragged from {start} to {end}"

        elif action.action_type == ComputerActionType.SCROLL:
            summary = f"scrolled by ({action.delta_x or 0}, {action.delta_y or 0})"

        # --- Keyboard actions ---
        elif action.action_type == ComputerActionType.TYPE and action.text:
            state["typed_text"] += action.text
            summary = f"typed {len(action.text)} chars"

        elif action.action_type == ComputerActionType.HOTKEY and action.keys:
            summary = f"pressed hotkey {'+'.join(action.keys)}"

        elif action.action_type == ComputerActionType.KEY_PRESS and action.keys:
            summary = f"pressed key(s) {', '.join(action.keys)}"

        # --- Timing ---
        elif action.action_type == ComputerActionType.WAIT:
            summary = f"waited {action.wait_ms} ms"

        # --- App management ---
        elif action.action_type == ComputerActionType.OPEN_APP:
            app = action.path or action.text or "unknown"
            summary = f"opened app {app!r}"

        elif action.action_type == ComputerActionType.FOCUS_WINDOW:
            title = action.text or str(action.metadata.get("title", "unknown"))
            state["window_title"] = title
            summary = f"focused window {title!r}"

        elif action.action_type == ComputerActionType.RESIZE_WINDOW:
            title = action.text or str(action.metadata.get("title", "unknown"))
            summary = f"resized window {title!r} to {action.width}×{action.height}"

        elif action.action_type == ComputerActionType.MINIMIZE_WINDOW:
            title = action.text or str(action.metadata.get("title", "unknown"))
            summary = f"minimized window {title!r}"

        elif action.action_type == ComputerActionType.MAXIMIZE_WINDOW:
            title = action.text or str(action.metadata.get("title", "unknown"))
            summary = f"maximized window {title!r}"

        # --- Observation helpers ---
        elif action.action_type == ComputerActionType.SCREENSHOT:
            summary = "captured screenshot"

        elif action.action_type == ComputerActionType.EXTRACT_TEXT:
            summary = "extracted visible text"

        state["last_action"] = summary
        observation = self.get_observation(session, session.observation_request)
        artifact = ComputerArtifact(kind="trace", mime_type="text/plain", content=summary)
        return ComputerActionResult(
            action_id=action.action_id,
            success=True,
            summary=summary,
            observation=observation,
            artifacts=[artifact],
        )

    def close_session(self, session: ComputerSession) -> ComputerSession:
        observation = self.get_observation(session, session.observation_request)
        # Clean up state to avoid memory leaks in long-running test suites.
        self._state.pop(session.session_id, None)
        return session.model_copy(update={"status": "closed", "last_observation": observation})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _target_label(self, target: ComputerCoordinate | UIElementRef | None) -> str:
        """Return a human-readable label for a target (used in action summaries)."""
        if target is None:
            return "screen"
        if isinstance(target, ComputerCoordinate):
            return f"({target.x},{target.y})"
        # UIElementRef
        label = target.name or target.role or target.selector or "element"
        if target.bounds:
            return f"{label}@({target.bounds.x},{target.bounds.y})"
        return label
