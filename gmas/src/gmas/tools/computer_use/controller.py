"""
Stateful controller for computer-use command envelopes.

Routes start / observe / act / close commands to the active runtime and
manages per-session state as frozen ComputerSession snapshots.

Thread-safety
-------------
- ``_lock`` (RLock) guards the shared ``_sessions`` registry and the
  ``_session_locks`` map.
- ``_session_locks`` holds one ``threading.Lock`` per session.  The ACT
  handler acquires the per-session lock before executing, so concurrent ACT
  calls on the *same* session are serialised (preventing step-count races)
  while calls on *different* sessions proceed fully in parallel.
- Closed sessions are retained for idempotent ``close`` calls, but are evicted
  once the ``_MAX_CLOSED_SESSIONS`` limit is exceeded to bound memory usage.
"""

import contextlib
import threading

from gmas.config.logging import logger

from .models import (
    ComputerAction,
    ComputerActionType,
    ComputerSession,
    ComputerUseCommand,
    ComputerUseOperation,
    ComputerUseResponse,
)
from .runtime import ComputerRuntime

# Maximum number of closed-session snapshots retained before eviction.
_MAX_CLOSED_SESSIONS = 100


class ComputerUseController:
    """
    Thin orchestration layer for LLM-facing computer-use commands.

    Maintains a registry of open sessions keyed by session_id.  Every
    mutation produces a new frozen ComputerSession snapshot via
    ``model_copy(update=…)`` so the history is always append-only.

    Closed sessions stay in the registry so that ``close`` is idempotent and
    callers can still inspect the final snapshot; however, ``act`` and
    ``observe`` are rejected on a closed session.  Old closed sessions are
    evicted once ``_MAX_CLOSED_SESSIONS`` entries accumulate.

    Example:
        runtime = MockComputerRuntime()
        controller = ComputerUseController(runtime)

        start = controller.handle(ComputerUseCommand(
            operation=ComputerUseOperation.START,
            config=ComputerSessionConfig(),
        ))
        session_id = start.session.session_id

        act = controller.handle(ComputerUseCommand(
            operation=ComputerUseOperation.ACT,
            session_id=session_id,
            action=ComputerAction(action_type=ComputerActionType.SCREENSHOT),
        ))

    """

    def __init__(self, runtime: ComputerRuntime) -> None:
        self.runtime = runtime
        self._sessions: dict[str, ComputerSession] = {}
        self._lock = threading.RLock()
        # Per-session locks serialise concurrent ACT calls on the same session,
        # preventing step-count races without blocking unrelated sessions.
        self._session_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def handle(self, command: ComputerUseCommand) -> ComputerUseResponse:
        """Dispatch one high-level command and return a structured response."""
        logger.debug(
            "computer_use: dispatching {} (session={})",
            command.operation.value,
            command.session_id,
        )
        if command.operation == ComputerUseOperation.START:
            return self._handle_start(command)
        if command.operation == ComputerUseOperation.OBSERVE:
            return self._handle_observe(command)
        if command.operation == ComputerUseOperation.ACT:
            return self._handle_act(command)
        if command.operation == ComputerUseOperation.CLOSE:
            return self._handle_close(command)
        # Unreachable in practice because the Enum validator rejects unknown
        # values before handle() is called, but kept for exhaustiveness.
        return ComputerUseResponse(
            success=False,
            error=f"unsupported operation: {command.operation}",
        )

    def close_all_sessions(self) -> None:
        """
        Close every open session.

        Intended for shutdown / cleanup flows (e.g. ``ComputerUseTool.close``).
        Individual close errors are suppressed so that cleanup continues even
        if a single runtime call raises.
        """
        with self._lock:
            open_ids = [sid for sid, s in self._sessions.items() if s.status != "closed"]
        for sid in open_ids:
            with contextlib.suppress(Exception):
                self.handle(
                    ComputerUseCommand(
                        operation=ComputerUseOperation.CLOSE,
                        session_id=sid,
                    )
                )

    # ------------------------------------------------------------------
    # Operation handlers
    # ------------------------------------------------------------------

    def _handle_start(self, command: ComputerUseCommand) -> ComputerUseResponse:
        if command.config is None:
            return ComputerUseResponse(success=False, error="start requires config")
        session = self.runtime.start_session(command.config)
        observation = self.runtime.get_observation(session, command.config.observation)
        session = session.model_copy(update={"last_observation": observation})
        with self._lock:
            self._sessions[session.session_id] = session
            # Pre-create the per-session lock so _handle_act can find it.
            self._session_locks.setdefault(session.session_id, threading.Lock())
        logger.info(
            "computer_use: started session {} (runtime={}, max_steps={})",
            session.session_id,
            session.runtime_name,
            session.max_steps,
        )
        # Evict old closed sessions to bound memory usage.
        self._cleanup_closed_sessions()
        return ComputerUseResponse(
            success=True,
            session=session,
            observation=observation,
            capabilities=self.runtime.capabilities(),
            artifacts=[observation.screenshot] if observation.screenshot else [],
        )

    def _handle_observe(self, command: ComputerUseCommand) -> ComputerUseResponse:
        session, error = self._require_session(command.session_id)
        if error:
            return ComputerUseResponse(success=False, error=error)
        if session is None:  # type narrowing: guaranteed when error is None
            return ComputerUseResponse(success=False, error="session not found")

        request = command.observation or session.observation_request
        observation = self.runtime.get_observation(session, request)
        session = session.model_copy(update={"last_observation": observation, "observation_request": request})
        with self._lock:
            self._sessions[session.session_id] = session
        logger.debug(
            "computer_use: observation captured for session {}",
            session.session_id,
        )
        return ComputerUseResponse(
            success=True,
            session=session,
            observation=observation,
            capabilities=self.runtime.capabilities(),
            artifacts=[observation.screenshot] if observation.screenshot else [],
        )

    def _handle_act(self, command: ComputerUseCommand) -> ComputerUseResponse:
        # Quick check that requires no session lookup.
        if command.action is None:
            return ComputerUseResponse(success=False, error="act requires action")

        # Look up the per-session lock.  Returns None for unknown session_ids.
        session_lock = self._get_session_lock(command.session_id)
        if session_lock is None:
            # session_id is missing or not registered — delegate to
            # _require_session for the canonical error message.
            _, error = self._require_session(command.session_id)
            return ComputerUseResponse(
                success=False,
                error=error or "session not found",
            )

        # Serialise concurrent ACT calls on the same session so that the step
        # counter is always incremented atomically.
        with session_lock:
            # Re-read under lock for the freshest state.
            session, error = self._require_session(command.session_id)
            if error:
                return ComputerUseResponse(success=False, error=error)
            if session is None:  # type narrowing: guaranteed when error is None
                return ComputerUseResponse(success=False, error="session not found")

            # Validate action-specific required fields before dispatching.
            field_error = self._validate_action_fields(command.action)
            if field_error:
                return ComputerUseResponse(
                    success=False,
                    session=session,
                    capabilities=self.runtime.capabilities(),
                    error=field_error,
                )

            if session.step_count >= session.max_steps:
                logger.warning(
                    "computer_use: session {} step budget exceeded ({}/{})",
                    session.session_id,
                    session.step_count,
                    session.max_steps,
                )
                return ComputerUseResponse(
                    success=False,
                    session=session,
                    capabilities=self.runtime.capabilities(),
                    error="session step budget exceeded",
                )

            logger.debug(
                "computer_use: executing {} (step {}/{}) in session {}",
                command.action.action_type.value,
                session.step_count + 1,
                session.max_steps,
                session.session_id,
            )
            result = self.runtime.execute(session, command.action)
            observation = result.observation or self.runtime.get_observation(session, session.observation_request)
            updated_session = session.model_copy(
                update={
                    "step_count": session.step_count + 1,
                    "last_observation": observation,
                    "history": (*session.history, result),
                }
            )
            with self._lock:
                self._sessions[updated_session.session_id] = updated_session

        if not result.success:
            logger.warning(
                "computer_use: action {} failed in session {}: {}",
                command.action.action_type.value,
                session.session_id,
                result.error,
            )

        artifacts = list(result.artifacts)
        # Append the observation screenshot only if it wasn't already
        # included in the action result's own artifacts (avoids duplicates
        # when the runtime embeds the post-action screenshot in both places).
        if observation.screenshot is not None:
            existing_ids = {a.artifact_id for a in artifacts}
            if observation.screenshot.artifact_id not in existing_ids:
                artifacts.append(observation.screenshot)
        return ComputerUseResponse(
            success=result.success,
            session=updated_session,
            observation=observation,
            action_result=result,
            capabilities=self.runtime.capabilities(),
            artifacts=artifacts,
            error=result.error,
        )

    def _handle_close(self, command: ComputerUseCommand) -> ComputerUseResponse:
        # require_open=False: close is idempotent — calling it on an already-closed
        # session returns success rather than an error.
        session, error = self._require_session(command.session_id, require_open=False)
        if error:
            return ComputerUseResponse(success=False, error=error)
        if session is None:  # type narrowing: guaranteed when error is None
            return ComputerUseResponse(success=False, error="session not found")

        if session.status == "closed":
            logger.debug(
                "computer_use: close on already-closed session {} (no-op)",
                session.session_id,
            )
            artifacts = []
            if session.last_observation and session.last_observation.screenshot:
                artifacts.append(session.last_observation.screenshot)
            return ComputerUseResponse(
                success=True,
                session=session,
                observation=session.last_observation,
                capabilities=self.runtime.capabilities(),
                artifacts=artifacts,
            )

        closed = self.runtime.close_session(session)
        closed = closed.model_copy(update={"status": "closed"})
        with self._lock:
            self._sessions[closed.session_id] = closed
        logger.info(
            "computer_use: closed session {} after {} steps",
            closed.session_id,
            closed.step_count,
        )
        # Evict old closed sessions to bound memory usage.
        self._cleanup_closed_sessions()
        artifacts = []
        if closed.last_observation and closed.last_observation.screenshot:
            artifacts.append(closed.last_observation.screenshot)
        return ComputerUseResponse(
            success=True,
            session=closed,
            observation=closed.last_observation,
            capabilities=self.runtime.capabilities(),
            artifacts=artifacts,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_session(
        self,
        session_id: str | None,
        *,
        require_open: bool = True,
    ) -> tuple[ComputerSession | None, str | None]:
        """Return ``(session, None)`` on success or ``(None, error_message)``."""
        if not session_id:
            return None, "session_id is required"
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return None, f"session not found: {session_id!r}"
        if require_open and session.status == "closed":
            return None, f"session is already closed: {session_id!r}"
        return session, None

    def _get_session_lock(self, session_id: str | None) -> "threading.Lock | None":
        """Return the per-session lock, or ``None`` if the session is unknown."""
        if not session_id:
            return None
        with self._lock:
            return self._session_locks.get(session_id)

    def _cleanup_closed_sessions(self) -> None:
        """
        Evict the oldest closed sessions beyond ``_MAX_CLOSED_SESSIONS``.

        Called after every START and CLOSE so the registry doesn't grow
        without bound in long-running services.
        """
        with self._lock:
            closed_ids = [sid for sid, s in self._sessions.items() if s.status == "closed"]
            overflow = len(closed_ids) - _MAX_CLOSED_SESSIONS
            if overflow > 0:
                for sid in closed_ids[:overflow]:
                    del self._sessions[sid]
                    self._session_locks.pop(sid, None)

    def _validate_action_fields(self, action: ComputerAction | None) -> str | None:
        """
        Validate action-specific required fields before dispatching to the runtime.

        Centralising validation in the controller means consistent error messages
        regardless of which runtime backend is active.

        Returns an error string on failure, or ``None`` when the action is valid.
        """
        if action is None:
            return None

        atype = action.action_type

        click_types = {
            ComputerActionType.CLICK,
            ComputerActionType.DOUBLE_CLICK,
            ComputerActionType.RIGHT_CLICK,
            ComputerActionType.HOVER,
        }
        if atype in click_types and action.target is None:
            return f"{atype.value} requires a target (screen coordinate or UI element)"

        if atype == ComputerActionType.DRAG and (action.target is None or action.end_target is None):
            return "drag requires both target and end_target"

        if atype == ComputerActionType.TYPE and action.text is None:
            return "type requires text"

        if atype == ComputerActionType.HOTKEY and not action.keys:
            return "hotkey requires keys"

        if atype == ComputerActionType.KEY_PRESS and not action.keys:
            return "key_press requires keys"

        if atype == ComputerActionType.NAVIGATE and not action.url:
            return "navigate requires url"

        if atype == ComputerActionType.OPEN_APP and not (action.path or action.text):
            return "open_app requires path or text"

        if atype == ComputerActionType.FOCUS_WINDOW and not (action.text or action.metadata.get("title")):
            return "focus_window requires title in text or metadata.title"

        window_actions = {
            ComputerActionType.RESIZE_WINDOW,
            ComputerActionType.MINIMIZE_WINDOW,
            ComputerActionType.MAXIMIZE_WINDOW,
        }
        if atype in window_actions and not (action.text or action.metadata.get("title")):
            return f"{atype.value} requires title in text or metadata.title"

        if atype == ComputerActionType.RESIZE_WINDOW and (action.width is None or action.height is None):
            return "resize_window requires width and height"

        return None
