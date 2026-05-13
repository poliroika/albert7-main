"""Tests for the standalone computer-use package."""

from pathlib import Path

import pytest

from gmas.tools.computer_use import (
    ComputerAction,
    ComputerActionType,
    ComputerCoordinate,
    ComputerSessionConfig,
    ComputerUseClient,
    ComputerUseCommand,
    ComputerUseController,
    ComputerUseOperation,
    MockComputerRuntime,
    ObservationMode,
    ObservationRequest,
    artifact_to_base64_url,
    build_computer_use_full_schema,
    build_computer_use_tool_schema,
    observation_to_openai_content,
)
from gmas.tools.computer_use.models import ComputerArtifact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_start_command() -> ComputerUseCommand:
    return ComputerUseCommand(
        operation=ComputerUseOperation.START,
        config=ComputerSessionConfig(
            start_url="https://example.com",
            observation=ObservationRequest(mode=ObservationMode.STANDARD),
        ),
    )


def build_type_command(session_id: str, text: str) -> ComputerUseCommand:
    return ComputerUseCommand(
        operation=ComputerUseOperation.ACT,
        session_id=session_id,
        action=ComputerAction(
            action_type=ComputerActionType.TYPE,
            text=text,
        ),
    )


def build_observe_command(session_id: str, mode: ObservationMode) -> ComputerUseCommand:
    return ComputerUseCommand(
        operation=ComputerUseOperation.OBSERVE,
        session_id=session_id,
        observation=ObservationRequest(mode=mode),
    )


def build_close_command(session_id: str) -> ComputerUseCommand:
    return ComputerUseCommand(
        operation=ComputerUseOperation.CLOSE,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Core lifecycle
# ---------------------------------------------------------------------------


def test_controller_start_act_observe_close():
    controller = ComputerUseController(MockComputerRuntime())

    start = controller.handle(build_start_command())
    assert start.success is True
    assert start.session is not None
    assert start.observation is not None
    assert start.observation.url == "https://example.com"
    assert start.observation.active_window is not None
    assert start.capabilities is not None
    assert start.capabilities.supports_screenshots is True

    act = controller.handle(build_type_command(start.session.session_id, "hello"))
    assert act.success is True
    assert act.session is not None
    assert act.session.step_count == 1
    assert act.observation is not None
    assert act.observation.text_excerpt == "hello"
    assert len(act.artifacts) >= 1

    observe = controller.handle(build_observe_command(start.session.session_id, ObservationMode.DETAILED))
    assert observe.success is True
    assert observe.observation is not None
    assert observe.observation.screenshot is not None
    assert len(observe.observation.elements) >= 1
    assert len(observe.observation.windows) >= 1

    close = controller.handle(build_close_command(start.session.session_id))
    assert close.success is True
    assert close.session is not None
    assert close.session.status == "closed"


def test_standalone_tool_adapter_round_trip():
    tool = ComputerUseClient(ComputerUseController(MockComputerRuntime()))
    start = tool.execute(**build_start_command().model_dump())
    assert start.success is True
    assert start.capabilities is not None
    assert tool.name == "computer_use"
    assert "stateful" in tool.description.lower()


def test_standalone_tool_execute_json_round_trip():
    tool = ComputerUseClient(ComputerUseController(MockComputerRuntime()))
    start = tool.execute_json(build_start_command().model_dump_json())
    assert start.success is True
    assert start.session is not None
    assert start.observation is not None


def test_default_artifact_root_is_based_on_current_working_directory():
    config = ComputerSessionConfig()
    expected_root = Path.cwd() / ".gmas" / "artifacts" / "computer_use"
    assert config.artifact_root == expected_root


# ---------------------------------------------------------------------------
# Error handling: missing / unknown session
# ---------------------------------------------------------------------------


def test_controller_rejects_unknown_session():
    tool = ComputerUseClient(ComputerUseController(MockComputerRuntime()))
    response = tool.execute(
        operation=ComputerUseOperation.OBSERVE,
        session_id="missing",
    )
    assert response.success is False
    assert response.error is not None
    assert "missing" in response.error  # error message should include the unknown session_id


def test_controller_rejects_missing_session_id():
    tool = ComputerUseClient(ComputerUseController(MockComputerRuntime()))
    response = tool.execute(operation=ComputerUseOperation.OBSERVE)
    assert response.success is False
    assert response.error is not None
    assert "session_id" in response.error


# ---------------------------------------------------------------------------
# Closed-session lifecycle (BUG FIX: closed sessions must be rejected)
# ---------------------------------------------------------------------------


def test_act_on_closed_session_is_rejected():
    """ACT on a closed session must return success=False."""
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    controller.handle(build_close_command(sid))

    act = controller.handle(build_type_command(sid, "after close"))
    assert act.success is False
    assert act.error is not None
    assert "closed" in act.error.lower()


def test_observe_on_closed_session_is_rejected():
    """OBSERVE on a closed session must return success=False."""
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    controller.handle(build_close_command(sid))

    obs = controller.handle(build_observe_command(sid, ObservationMode.STANDARD))
    assert obs.success is False
    assert obs.error is not None
    assert "closed" in obs.error.lower()


def test_close_is_idempotent():
    """Calling close twice must succeed both times without errors."""
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    close1 = controller.handle(build_close_command(sid))
    assert close1.success is True
    assert close1.session is not None
    assert close1.session.status == "closed"

    close2 = controller.handle(build_close_command(sid))
    assert close2.success is True
    assert close2.session is not None
    assert close2.session.status == "closed"


# ---------------------------------------------------------------------------
# Step budget
# ---------------------------------------------------------------------------


def test_step_budget_exhaustion_blocks_act():
    """The (max_steps+1)-th ACT must be rejected by the controller."""
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.START,
            config=ComputerSessionConfig(start_url="https://example.com", max_steps=3),
        )
    )
    assert start.session is not None
    sid = start.session.session_id

    for i in range(3):
        resp = controller.handle(build_type_command(sid, f"step{i}"))
        assert resp.success is True
        assert resp.session is not None
        assert resp.session.step_count == i + 1

    overflow = controller.handle(build_type_command(sid, "overflow"))
    assert overflow.success is False
    assert overflow.error is not None
    assert "budget" in overflow.error.lower() or "exceeded" in overflow.error.lower()


# ---------------------------------------------------------------------------
# Action-field validation (BUG FIX: validation in controller, not just runtime)
# ---------------------------------------------------------------------------


def test_click_without_target_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(action_type=ComputerActionType.CLICK),  # no target
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "target" in act.error.lower()


def test_drag_without_end_target_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(
                action_type=ComputerActionType.DRAG,
                target=ComputerCoordinate(x=10, y=20),
                # end_target missing
            ),
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "end_target" in act.error.lower()


def test_navigate_without_url_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(action_type=ComputerActionType.NAVIGATE),  # no url
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "url" in act.error.lower()


def test_type_without_text_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(action_type=ComputerActionType.TYPE),  # no text
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "text" in act.error.lower()


def test_hotkey_without_keys_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(action_type=ComputerActionType.HOTKEY),  # no keys
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "keys" in act.error.lower()


def test_act_without_action_envelope_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    resp = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
        )
    )
    assert resp.success is False
    assert resp.error is not None
    assert "action" in resp.error.lower()


def test_start_without_config_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    resp = controller.handle(ComputerUseCommand(operation=ComputerUseOperation.START))
    assert resp.success is False
    assert resp.error is not None
    assert "config" in resp.error.lower()


# ---------------------------------------------------------------------------
# Mock runtime: all action types succeed
# ---------------------------------------------------------------------------


def test_all_action_types_succeed_in_mock():
    """Every ComputerActionType must be handled by the mock without raising."""
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.START,
            config=ComputerSessionConfig(max_steps=50),
        )
    )
    assert start.session is not None
    sid = start.session.session_id

    actions = [
        ComputerAction(action_type=ComputerActionType.NAVIGATE, url="https://test.com"),
        ComputerAction(action_type=ComputerActionType.TYPE, text="hello"),
        ComputerAction(action_type=ComputerActionType.CLICK, target=ComputerCoordinate(x=100, y=200)),
        ComputerAction(action_type=ComputerActionType.DOUBLE_CLICK, target=ComputerCoordinate(x=100, y=200)),
        ComputerAction(action_type=ComputerActionType.RIGHT_CLICK, target=ComputerCoordinate(x=100, y=200)),
        ComputerAction(action_type=ComputerActionType.HOVER, target=ComputerCoordinate(x=100, y=200)),
        ComputerAction(
            action_type=ComputerActionType.DRAG,
            target=ComputerCoordinate(x=10, y=20),
            end_target=ComputerCoordinate(x=300, y=400),
        ),
        ComputerAction(action_type=ComputerActionType.SCROLL, delta_y=3),
        ComputerAction(action_type=ComputerActionType.HOTKEY, keys=["ctrl", "a"]),
        ComputerAction(action_type=ComputerActionType.KEY_PRESS, keys=["enter"]),
        ComputerAction(action_type=ComputerActionType.WAIT, wait_ms=0),
        ComputerAction(action_type=ComputerActionType.SCREENSHOT),
        ComputerAction(action_type=ComputerActionType.EXTRACT_TEXT),
        ComputerAction(action_type=ComputerActionType.OPEN_APP, path="notepad.exe"),
        ComputerAction(action_type=ComputerActionType.FOCUS_WINDOW, text="Mock"),
        ComputerAction(action_type=ComputerActionType.RESIZE_WINDOW, text="Mock", width=800, height=600),
        ComputerAction(action_type=ComputerActionType.MINIMIZE_WINDOW, text="Mock"),
        ComputerAction(action_type=ComputerActionType.MAXIMIZE_WINDOW, text="Mock"),
    ]

    for action in actions:
        resp = controller.handle(
            ComputerUseCommand(
                operation=ComputerUseOperation.ACT,
                session_id=sid,
                action=action,
            )
        )
        assert resp.success is True, f"{action.action_type.value} should succeed in mock, got: {resp.error}"


def test_mock_state_is_cleaned_up_after_close():
    """Mock runtime should not retain session state after close_session."""
    runtime = MockComputerRuntime()
    controller = ComputerUseController(runtime)

    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id
    assert sid in runtime._state

    controller.handle(build_close_command(sid))
    assert sid not in runtime._state


def test_mock_focus_window_updates_window_title():
    """focus_window should update the window_title in mock state."""
    runtime = MockComputerRuntime()
    controller = ComputerUseController(runtime)

    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(action_type=ComputerActionType.FOCUS_WINDOW, text="Notepad"),
        )
    )
    assert runtime._state[sid]["window_title"] == "Notepad"


def test_history_accumulates_as_immutable_tuple():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    for i in range(3):
        controller.handle(build_type_command(sid, f"step{i}"))

    with controller._lock:
        session = controller._sessions[sid]

    assert isinstance(session.history, tuple)
    assert len(session.history) == 3


def test_observe_without_explicit_request_uses_session_default():
    """OBSERVE with no observation field must fall back to the session's default."""
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.START,
            config=ComputerSessionConfig(
                start_url="https://example.com",
                observation=ObservationRequest(include_screenshot=False, include_text=True),
            ),
        )
    )
    assert start.session is not None
    sid = start.session.session_id

    obs = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.OBSERVE,
            session_id=sid,
        )
    )
    assert obs.success is True
    assert obs.observation is not None
    assert obs.observation.screenshot is None, "session default has include_screenshot=False"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_computer_use_tool_schema_exposes_command_envelope():
    schema = build_computer_use_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "computer_use"
    props = schema["function"]["parameters"]["properties"]
    assert "operation" in props
    assert "action" in props
    assert "observation" in props


def test_simplified_schema_has_descriptions_on_all_top_level_properties():
    schema = build_computer_use_tool_schema()
    props = schema["function"]["parameters"]["properties"]
    for name, prop in props.items():
        assert "description" in prop, f"property '{name}' is missing a description"


def test_simplified_schema_lists_all_action_types():
    schema = build_computer_use_tool_schema()
    action_props = schema["function"]["parameters"]["properties"]["action"]
    action_types = action_props["properties"]["action_type"]["enum"]
    expected = {at.value for at in ComputerActionType}
    assert set(action_types) == expected


def test_full_schema_uses_pydantic_model():
    full = build_computer_use_full_schema()
    assert full["type"] == "function"
    assert full["function"]["name"] == "computer_use"
    # Full schema contains $defs from Pydantic
    assert "$defs" in full["function"]["parameters"]


# ---------------------------------------------------------------------------
# base64 / multimodal helpers
# ---------------------------------------------------------------------------


def test_artifact_to_base64_url_with_in_memory_content():
    artifact = ComputerArtifact(kind="trace", mime_type="text/plain", content="hello")
    url = artifact_to_base64_url(artifact)
    assert url is not None
    assert url.startswith("data:text/plain;base64,")
    import base64

    decoded = base64.b64decode(url.split(",", 1)[1]).decode("utf-8")
    assert decoded == "hello"


def test_artifact_to_base64_url_returns_none_for_missing_file():
    artifact = ComputerArtifact(
        kind="screenshot",
        mime_type="image/png",
        path="/nonexistent/path/screenshot.png",
    )
    url = artifact_to_base64_url(artifact)
    assert url is None


def test_artifact_to_base64_url_returns_none_for_empty_artifact():
    artifact = ComputerArtifact(kind="trace")
    assert artifact_to_base64_url(artifact) is None


def test_observation_to_openai_content_includes_text_parts():
    """observation_to_openai_content must produce at least the text block."""
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    obs = start.observation
    assert obs is not None

    content = observation_to_openai_content(obs)
    assert len(content) >= 1

    # Screenshot artifact path doesn't exist in mock, so no image block.
    # But there should be a text block with URL / title info.
    text_blocks = [c for c in content if c.get("type") == "text"]
    assert len(text_blocks) >= 1
    combined_text = " ".join(b["text"] for b in text_blocks)
    assert "https://example.com" in combined_text


def test_observation_to_openai_content_includes_image_when_file_exists(tmp_path):
    """When the screenshot file exists, a base64 image block must be included."""
    png_path = tmp_path / "test.png"
    # Minimal 1×1 white PNG (89 bytes, no external deps needed)
    png_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
        b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    artifact = ComputerArtifact(kind="screenshot", mime_type="image/png", path=str(png_path))
    from gmas.tools.computer_use.models import ComputerObservation

    obs = ComputerObservation(url="https://test.com", title="Test", screenshot=artifact)
    content = observation_to_openai_content(obs)

    image_blocks = [c for c in content if c.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# Window management actions
# ---------------------------------------------------------------------------


def test_resize_window_without_dimensions_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(
                action_type=ComputerActionType.RESIZE_WINDOW,
                text="Mock",
                # width and height missing
            ),
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "width" in act.error.lower() or "height" in act.error.lower()


def test_resize_window_without_title_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(
                action_type=ComputerActionType.RESIZE_WINDOW,
                width=800,
                height=600,
                # text (title) missing
            ),
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "title" in act.error.lower()


def test_minimize_window_without_title_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(action_type=ComputerActionType.MINIMIZE_WINDOW),
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "title" in act.error.lower()


def test_maximize_window_without_title_is_rejected():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(action_type=ComputerActionType.MAXIMIZE_WINDOW),
        )
    )
    assert act.success is False
    assert act.error is not None
    assert "title" in act.error.lower()


def test_resize_window_succeeds_in_mock():
    controller = ComputerUseController(MockComputerRuntime())
    start = controller.handle(build_start_command())
    assert start.session is not None
    sid = start.session.session_id

    act = controller.handle(
        ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=sid,
            action=ComputerAction(
                action_type=ComputerActionType.RESIZE_WINDOW,
                text="Mock",
                width=1024,
                height=768,
            ),
        )
    )
    assert act.success is True
    assert act.action_result is not None
    assert "1024" in act.action_result.summary
    assert "768" in act.action_result.summary


# ---------------------------------------------------------------------------
# Schema: new action types must be present
# ---------------------------------------------------------------------------


def test_simplified_schema_includes_new_window_actions():
    schema = build_computer_use_tool_schema()
    action_types = schema["function"]["parameters"]["properties"]["action"]["properties"]["action_type"]["enum"]
    assert "resize_window" in action_types
    assert "minimize_window" in action_types
    assert "maximize_window" in action_types


def test_simplified_schema_includes_screenshot_format():
    schema = build_computer_use_tool_schema()
    obs_props = schema["function"]["parameters"]["properties"]["observation"]["properties"]
    assert "screenshot_format" in obs_props
    assert "screenshot_quality" in obs_props


def test_simplified_schema_includes_timeout_ms():
    schema = build_computer_use_tool_schema()
    action_props = schema["function"]["parameters"]["properties"]["action"]["properties"]
    assert "timeout_ms" in action_props
    assert "width" in action_props
    assert "height" in action_props


def test_simplified_schema_includes_ocr_in_metadata_description():
    schema = build_computer_use_tool_schema()
    action_props = schema["function"]["parameters"]["properties"]["action"]["properties"]
    desc = action_props["metadata"]["description"]
    assert "ocr" in desc.lower()


# ---------------------------------------------------------------------------
# Model: new fields
# ---------------------------------------------------------------------------


def test_observation_request_screenshot_format_default():
    from gmas.tools.computer_use.models import ObservationRequest

    req = ObservationRequest()
    assert req.screenshot_format == "png"
    assert req.screenshot_quality == 85


def test_action_timeout_default():
    action = ComputerAction(action_type=ComputerActionType.SCREENSHOT)
    assert action.timeout_ms == 30000


def test_action_width_height_fields():
    action = ComputerAction(
        action_type=ComputerActionType.RESIZE_WINDOW,
        text="test",
        width=1920,
        height=1080,
    )
    assert action.width == 1920
    assert action.height == 1080


# ---------------------------------------------------------------------------
# ComputerUseController — missed branches
# ---------------------------------------------------------------------------


class TestControllerMissedBranches:
    """Cover the few branches in controller.py not exercised elsewhere."""

    def _make_ctrl(self):
        return ComputerUseController(MockComputerRuntime())

    # line 100 — unreachable operation guard (monkey-patch handle)
    def test_handle_unknown_operation_returns_error(self):
        from unittest.mock import MagicMock, patch

        ctrl = self._make_ctrl()
        cmd = MagicMock()
        # Give the mock a realistic .value attribute for the log line
        fake_op = MagicMock()
        fake_op.value = "nonexistent_operation"
        cmd.operation = fake_op
        # Ensure that handle reaches the else-branch by making operation not
        # equal to any real ComputerUseOperation member.
        with patch.object(
            ComputerUseOperation,
            "__eq__",
            return_value=False,
        ):
            resp = ctrl.handle(cmd)
        assert resp.success is False

    # lines 359-361 — _cleanup_closed_sessions eviction
    def test_cleanup_evicts_excess_closed_sessions(self):
        from gmas.tools.computer_use.controller import _MAX_CLOSED_SESSIONS

        ctrl = self._make_ctrl()
        # Fill with more than _MAX_CLOSED_SESSIONS closed entries
        from gmas.tools.computer_use.models import ComputerSession

        overflow_count = _MAX_CLOSED_SESSIONS + 5
        for i in range(overflow_count):
            sid = f"closed-{i}"
            sess = ComputerSession(
                session_id=sid,
                runtime_name="mock",
                status="closed",
            )
            ctrl._sessions[sid] = sess
            ctrl._session_locks[sid] = __import__("threading").Lock()

        ctrl._cleanup_closed_sessions()
        remaining_closed = [sid for sid, s in ctrl._sessions.items() if s.status == "closed"]
        assert len(remaining_closed) <= _MAX_CLOSED_SESSIONS

    # line 344 — _get_session_lock returns None for empty session_id
    def test_get_session_lock_empty_session_id(self):
        ctrl = self._make_ctrl()
        lock = ctrl._get_session_lock("")
        assert lock is None

    # line 344 — _get_session_lock returns None for unknown session_id
    def test_get_session_lock_unknown_session_id(self):
        ctrl = self._make_ctrl()
        lock = ctrl._get_session_lock("session-does-not-exist")
        assert lock is None

    # lines 248 — act with failed action result logs warning (success=False)
    def test_act_failed_result_returns_failure(self):
        ctrl = self._make_ctrl()
        # Start a session first
        resp = ctrl.handle(build_start_command())
        assert resp.success
        session_id = resp.session.session_id

        # Navigate with an action that will succeed but craft one that fails via wrong args
        resp_act = ctrl.handle(
            ComputerUseCommand(
                operation=ComputerUseOperation.ACT,
                session_id=session_id,
                action=ComputerAction(action_type=ComputerActionType.CLICK, target=None),
            )
        )
        # CLICK without target should fail validation → success=False
        assert resp_act.success is False

    # lines 396-405 — _validate_action_fields: hotkey / key_press / navigate / open_app / focus_window
    def test_validate_action_hotkey_no_keys(self):
        ctrl = self._make_ctrl()
        resp = ctrl.handle(build_start_command())
        sid = resp.session.session_id

        res = ctrl.handle(
            ComputerUseCommand(
                operation=ComputerUseOperation.ACT,
                session_id=sid,
                action=ComputerAction(action_type=ComputerActionType.HOTKEY, keys=[]),
            )
        )
        assert res.success is False
        assert "hotkey requires keys" in (res.error or "")

    def test_validate_action_key_press_no_keys(self):
        ctrl = self._make_ctrl()
        resp = ctrl.handle(build_start_command())
        sid = resp.session.session_id

        res = ctrl.handle(
            ComputerUseCommand(
                operation=ComputerUseOperation.ACT,
                session_id=sid,
                action=ComputerAction(action_type=ComputerActionType.KEY_PRESS, keys=[]),
            )
        )
        assert res.success is False
        assert "key_press requires keys" in (res.error or "")

    def test_validate_action_navigate_no_url(self):
        ctrl = self._make_ctrl()
        resp = ctrl.handle(build_start_command())
        sid = resp.session.session_id

        res = ctrl.handle(
            ComputerUseCommand(
                operation=ComputerUseOperation.ACT,
                session_id=sid,
                action=ComputerAction(action_type=ComputerActionType.NAVIGATE, url=None),
            )
        )
        assert res.success is False
        assert "navigate requires url" in (res.error or "")

    def test_validate_action_open_app_no_path_no_text(self):
        ctrl = self._make_ctrl()
        resp = ctrl.handle(build_start_command())
        sid = resp.session.session_id

        res = ctrl.handle(
            ComputerUseCommand(
                operation=ComputerUseOperation.ACT,
                session_id=sid,
                action=ComputerAction(action_type=ComputerActionType.OPEN_APP),
            )
        )
        assert res.success is False
        assert "open_app requires" in (res.error or "")

    def test_validate_action_focus_window_no_title(self):
        ctrl = self._make_ctrl()
        resp = ctrl.handle(build_start_command())
        sid = resp.session.session_id

        res = ctrl.handle(
            ComputerUseCommand(
                operation=ComputerUseOperation.ACT,
                session_id=sid,
                action=ComputerAction(action_type=ComputerActionType.FOCUS_WINDOW),
            )
        )
        assert res.success is False
        assert "focus_window requires" in (res.error or "")

    def test_validate_action_resize_window_no_width_height(self):
        ctrl = self._make_ctrl()
        resp = ctrl.handle(build_start_command())
        sid = resp.session.session_id

        res = ctrl.handle(
            ComputerUseCommand(
                operation=ComputerUseOperation.ACT,
                session_id=sid,
                action=ComputerAction(action_type=ComputerActionType.RESIZE_WINDOW, text="title"),
            )
        )
        assert res.success is False
        assert "resize_window requires width and height" in (res.error or "")

    # lines 278, 280 — close unknown session
    def test_close_unknown_session_id_returns_error(self):
        ctrl = self._make_ctrl()
        res = ctrl.handle(ComputerUseCommand(operation=ComputerUseOperation.CLOSE, session_id="ghost-session"))
        assert res.success is False


# ---------------------------------------------------------------------------
# ComputerUseTool (framework.py) — missed branches
# ---------------------------------------------------------------------------


class TestComputerUseToolFramework:
    """Cover missed lines in framework.py."""

    def _make_tool(self):
        from gmas.tools.computer_use.framework import ComputerUseTool

        return ComputerUseTool(runtime_name="mock")

    # lines 126-144 — execute: failure and exception paths
    def test_execute_failure_path(self):
        """When controller returns success=False, ToolResult.success is False."""
        tool = self._make_tool()
        result = tool.execute(operation="act", action={"action_type": "click"})
        # No session_id → error from controller
        assert result.success is False

    def test_execute_exception_path(self):
        """When controller raises, execute catches and returns failure."""
        from unittest.mock import patch

        tool = self._make_tool()
        with patch.object(tool._controller, "handle", side_effect=RuntimeError("crash")):
            result = tool.execute(operation="start")
        assert result.success is False
        assert "crash" in result.error

    # lines 215-219 — _create_runtime unknown runtime name
    def test_create_runtime_unknown_raises(self):
        from gmas.tools.computer_use.framework import ComputerUseTool

        with pytest.raises(ValueError, match="unknown computer_use runtime"):
            ComputerUseTool(runtime_name="unknown_runtime")

    # lines 215-219 — _create_runtime windows_native when WindowsComputerRuntime is None
    def test_create_runtime_windows_native_not_available(self):
        from unittest.mock import patch

        from gmas.tools.computer_use import framework
        from gmas.tools.computer_use.framework import ComputerUseTool as _Tool

        with (
            patch.object(framework, "WindowsComputerRuntime", None),
            pytest.raises(RuntimeError, match="windows_native runtime requires"),
        ):
            _Tool(runtime_name="windows_native")

    # lines 254-255, 275-276 — callbacks fired when cb_manager present
    def test_execute_emits_callbacks(self):
        from unittest.mock import MagicMock

        from gmas.tools.computer_use.framework import ComputerUseTool

        mock_cb = MagicMock()
        tool = ComputerUseTool(runtime_name="mock", callback_manager=mock_cb)
        tool.execute(operation="start")
        mock_cb.on_tool_start.assert_called_once()
        mock_cb.on_tool_end.assert_called_once()

    def test_execute_emits_error_callback(self):
        from unittest.mock import MagicMock, patch

        from gmas.tools.computer_use.framework import ComputerUseTool

        mock_cb = MagicMock()
        tool = ComputerUseTool(runtime_name="mock", callback_manager=mock_cb)
        with patch.object(tool._controller, "handle", side_effect=RuntimeError("boom")):
            tool.execute(operation="start")
        mock_cb.on_tool_error.assert_called_once()

    def test_auto_runtime_falls_back_to_mock(self):
        """runtime_name='auto' must work cross-platform via mock fallback."""
        import json

        from gmas.tools.computer_use.framework import ComputerUseTool

        tool = ComputerUseTool(runtime_name="auto")
        result = tool.execute(operation="start", config={"runtime_name": "auto"})
        assert result.success is True
        payload = json.loads(result.output)
        assert payload["session"]["runtime_name"] in {
            "mock",
            "windows_native",
            "linux_native",
            "macos_native",
        }

    def test_auto_runtime_honors_env_runtime_order(self, monkeypatch):
        """Configured runtime order should be respected when available."""
        import json

        from gmas.tools.computer_use.framework import ComputerUseTool

        monkeypatch.setenv("GMAS_COMPUTER_USE_RUNTIME_ORDER", "mock,windows_native")
        tool = ComputerUseTool(runtime_name="auto")
        result = tool.execute(operation="start", config={"runtime_name": "auto"})
        assert result.success is True
        payload = json.loads(result.output)
        assert payload["session"]["runtime_name"] == "mock"

    def test_auto_runtime_prefers_linux_on_linux(self, monkeypatch):
        from gmas.tools.computer_use import framework
        from gmas.tools.computer_use.framework import ComputerUseTool

        monkeypatch.delenv("GMAS_COMPUTER_USE_RUNTIME_ORDER", raising=False)
        monkeypatch.setattr(framework.sys, "platform", "linux")
        monkeypatch.setattr(framework, "LinuxComputerRuntime", object)
        monkeypatch.setattr(framework, "MacOSComputerRuntime", None)
        monkeypatch.setattr(framework, "WindowsComputerRuntime", None)
        assert ComputerUseTool._resolve_auto_runtime_name() == "linux_native"

    def test_auto_runtime_prefers_macos_on_darwin(self, monkeypatch):
        from gmas.tools.computer_use import framework
        from gmas.tools.computer_use.framework import ComputerUseTool

        monkeypatch.delenv("GMAS_COMPUTER_USE_RUNTIME_ORDER", raising=False)
        monkeypatch.setattr(framework.sys, "platform", "darwin")
        monkeypatch.setattr(framework, "LinuxComputerRuntime", None)
        monkeypatch.setattr(framework, "MacOSComputerRuntime", object)
        monkeypatch.setattr(framework, "WindowsComputerRuntime", None)
        assert ComputerUseTool._resolve_auto_runtime_name() == "macos_native"

    # line 200 — close() is idempotent
    def test_close_idempotent(self):
        tool = self._make_tool()
        tool.close()
        tool.close()  # second call should not raise

    # async context manager — __aenter__ / __aexit__
    async def test_async_context_manager(self):
        tool = self._make_tool()
        async with tool as t:
            assert t is tool
