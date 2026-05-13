"""
Framework-compatible computer-use tool.

Wraps ComputerUseController as a BaseTool so it can be registered in the
global tool registry and used by agents through the standard tools interface.
"""

import asyncio
import contextlib
import json
import os
import sys
import time
from typing import Any, Self
from uuid import UUID, uuid4

from .client import _DESCRIPTION, build_computer_use_tool_schema
from .controller import ComputerUseController
from .mock import MockComputerRuntime
from .models import ComputerUseCommand, ComputerUseResponse

try:
    from .linux import LinuxComputerRuntime
except ImportError:
    LinuxComputerRuntime = None  # ty: ignore[invalid-assignment]

try:
    from .macos import MacOSComputerRuntime
except ImportError:
    MacOSComputerRuntime = None  # ty: ignore[invalid-assignment]

try:
    from .windows import WindowsComputerRuntime
except ImportError:
    WindowsComputerRuntime = None  # ty: ignore[invalid-assignment]

from ..base import BaseTool, ToolResult, register_tool_factory


class ComputerUseTool(BaseTool):
    """
    Framework-compatible wrapper around the stateful computer-use controller.

    Registered in the global tool registry under the name ``"computer_use"``.
    The runtime is selected at construction time:

    - ``"auto"``: first available runtime from configured order.
    - ``"mock"``: fully deterministic in-memory runtime (always available).
    - ``"linux_native"``: Linux desktop runtime (pyautogui + Pillow).
    - ``"macos_native"``: macOS desktop runtime (pyautogui + Pillow).
    - ``"windows_native"``: Win32-backed runtime (requires ``pywin32`` and
      ``Pillow`` on Windows; install with ``pip install "frontier-ai-gmas[computer-use]"``).

    Blocking operations (screenshots, waits, input injection) should use
    ``execute_async`` inside async agent loops to avoid blocking the event loop.

    Example:
        from gmas.tools import get_registry

        tool = get_registry().get("computer_use")
        result = tool.execute(operation="start", config={"max_steps": 10})
        payload = json.loads(result.output)
        session_id = payload["session"]["session_id"]

        tool.execute(operation="close", session_id=session_id)

    """

    def __init__(
        self,
        runtime_name: str = "auto",
        callback_manager: Any | None = None,
    ) -> None:
        """
        Create ComputerUseTool.

        Args:
            runtime_name: Backend to use (``"auto"``, ``"mock"``,
                ``"windows_native"``, ``"linux_native"`` or ``"macos_native"``).
            callback_manager: CallbackManager for sending events.
                If ``None``, the context manager is used on each call.

        """
        self._runtime_name = runtime_name
        self._controller = ComputerUseController(self._create_runtime(runtime_name))
        self._callback_manager = callback_manager

    # ------------------------------------------------------------------
    # BaseTool interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "computer_use"

    @property
    def description(self) -> str:
        return _DESCRIPTION

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return build_computer_use_tool_schema()["function"]["parameters"]

    def execute(self, **kwargs: Any) -> ToolResult:
        """
        Execute a computer-use command envelope (synchronous).

        A fresh ``run_id`` is generated once per call and forwarded to every
        ``on_tool_start`` / ``on_tool_end`` / ``on_tool_error`` callback, so
        callbacks can correlate events that belong to the same invocation.

        Args:
            **kwargs: Fields of ``ComputerUseCommand`` — ``operation``,
                ``session_id``, ``config``, ``action``, ``observation``.

        Returns:
            ToolResult whose ``output`` is a JSON-serialised
            ``ComputerUseResponse``.  Session history is stripped from the
            payload to avoid flooding the LLM context window.

        """
        operation = str(kwargs.get("operation", ""))
        run_id = uuid4()
        self._emit_tool_start(run_id, operation, kwargs)
        _start = time.monotonic()
        try:
            response = self._controller.handle(ComputerUseCommand(**kwargs))
            payload = self._serialize_response(response)
            elapsed_ms = (time.monotonic() - _start) * 1000
            if response.success:
                self._emit_tool_end(
                    run_id,
                    operation,
                    success=True,
                    output_size=len(payload),
                    duration_ms=elapsed_ms,
                )
                return ToolResult(tool_name=self.name, success=True, output=payload)
            error = response.error or "computer_use execution failed"
            self._emit_tool_end(
                run_id,
                operation,
                success=False,
                output_size=len(payload),
                duration_ms=elapsed_ms,
                result_summary=error,
            )
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=error,
                output=payload,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.monotonic() - _start) * 1000
            self._emit_tool_error(run_id, operation, exc)
            return ToolResult(tool_name=self.name, success=False, error=str(exc))

    async def execute_async(self, **kwargs: Any) -> ToolResult:
        """
        Async version of ``execute`` using ``asyncio.to_thread``.

        Delegates the blocking synchronous call to a thread pool so that
        long-running operations (screenshots, ``wait`` actions, input
        injection) do not block the event loop.

        Args:
            **kwargs: Same arguments as ``execute``.

        Returns:
            ToolResult — identical to what ``execute`` would return.

        Example:
            tool = ComputerUseTool(runtime_name="mock")
            result = await tool.execute_async(
                operation="start",
                config={"max_steps": 5},
            )

        """
        return await asyncio.to_thread(self.execute, **kwargs)

    # ------------------------------------------------------------------
    # Resource management
    # ------------------------------------------------------------------

    def close(self) -> None:
        """
        Close all open sessions and release resources.

        Calls ``close_all_sessions()`` on the underlying controller so that
        every active session is gracefully shut down (final screenshots taken,
        launched processes terminated).  Safe to call multiple times.
        """
        with contextlib.suppress(Exception):
            self._controller.close_all_sessions()

    def __del__(self) -> None:
        """Release resources on garbage collection."""
        with contextlib.suppress(Exception):
            self.close()

    def __enter__(self) -> Self:
        """Synchronous context manager entry."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close resources when exiting the synchronous context manager."""
        self.close()

    async def __aenter__(self) -> Self:
        """Asynchronous context manager entry."""
        return self

    async def __aexit__(self, *_args: object) -> None:
        """Close resources when exiting the asynchronous context manager."""
        await asyncio.to_thread(self.close)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_runtime(self, runtime_name: str):
        if runtime_name == "auto":
            runtime_name = self._resolve_auto_runtime_name()
        if runtime_name == "mock":
            return MockComputerRuntime()
        if runtime_name == "linux_native":
            if LinuxComputerRuntime is None:
                msg = (
                    "linux_native runtime requires Linux desktop dependencies. "
                    'Install with: pip install "frontier-ai-gmas[computer-use]"'
                )
                raise RuntimeError(msg)
            return LinuxComputerRuntime()
        if runtime_name == "macos_native":
            if MacOSComputerRuntime is None:
                msg = (
                    "macos_native runtime requires macOS desktop dependencies. "
                    'Install with: pip install "frontier-ai-gmas[computer-use]"'
                )
                raise RuntimeError(msg)
            return MacOSComputerRuntime()
        if runtime_name == "windows_native":
            if WindowsComputerRuntime is None:
                msg = (
                    "windows_native runtime requires pywin32 and Pillow on Windows. "
                    'Install with: pip install "frontier-ai-gmas[computer-use]"'
                )
                raise RuntimeError(msg)
            return WindowsComputerRuntime()
        msg = (
            f"unknown computer_use runtime: {runtime_name!r}. "
            f"Available runtimes: {', '.join(self._available_runtime_names())}"
        )
        raise ValueError(msg)

    @staticmethod
    def _available_runtime_names() -> tuple[str, ...]:
        names = ["auto", "mock"]
        if sys.platform == "win32" and WindowsComputerRuntime is not None:
            names.append("windows_native")
        if sys.platform.startswith("linux") and LinuxComputerRuntime is not None:
            _rt_available = getattr(LinuxComputerRuntime, "is_available", lambda: True)()
            if _rt_available:
                names.append("linux_native")
        if sys.platform == "darwin" and MacOSComputerRuntime is not None:
            names.append("macos_native")
        return tuple(names)

    @classmethod
    def _resolve_auto_runtime_name(cls) -> str:
        """
        Resolve runtime for ``runtime_name='auto'``.

        Priority:
        1) ``GMAS_COMPUTER_USE_RUNTIME_ORDER`` (comma-separated runtime names).
        2) Built-in fallback order for current OS.
        """
        configured_order = os.getenv("GMAS_COMPUTER_USE_RUNTIME_ORDER", "").strip()
        if configured_order:
            requested = [name.strip() for name in configured_order.split(",") if name.strip()]
            selected = cls._first_available_runtime(requested)
            if selected is not None:
                return selected
        if sys.platform == "win32":
            defaults = ("windows_native", "mock")
        elif sys.platform.startswith("linux"):
            defaults = ("linux_native", "mock")
        elif sys.platform == "darwin":
            defaults = ("macos_native", "mock")
        else:
            defaults = ("mock",)
        return cls._first_available_runtime(defaults) or "mock"

    @classmethod
    def _first_available_runtime(cls, names: tuple[str, ...] | list[str]) -> str | None:
        available = set(cls._available_runtime_names())
        for name in names:
            if name in available and name != "auto":
                return name
        return None

    @staticmethod
    def _serialize_response(response: ComputerUseResponse) -> str:
        """
        Serialise ``response`` to JSON, stripping the session action history.

        The history tuple can be very large — each entry contains a full
        ``ComputerObservation`` with a screenshot path and element list.
        Sending the entire history on every response would fill the LLM context
        window quickly; the LLM already received each result when the action
        was executed.
        """
        data = response.model_dump(mode="json")
        if data.get("session") and data["session"].get("history"):
            data["session"]["history"] = []
        return json.dumps(data, ensure_ascii=False)

    def _get_callback_manager(self) -> Any | None:
        """Return callback manager from constructor or from context."""
        if self._callback_manager is not None:
            return self._callback_manager
        with contextlib.suppress(Exception):
            from gmas.callbacks.context import get_callback_manager

            return get_callback_manager()
        return None

    def _emit_tool_start(self, run_id: UUID, operation: str, arguments: dict[str, Any]) -> None:
        """Send tool-execution-start event using the given ``run_id``."""
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_start(
                    run_id,
                    tool_name=self.name,
                    action=operation,
                    arguments=arguments,
                )

    def _emit_tool_end(
        self,
        run_id: UUID,
        operation: str,
        *,
        success: bool = True,
        output_size: int = 0,
        duration_ms: float = 0.0,
        result_summary: str = "",
    ) -> None:
        """Send tool-execution-end event using the same ``run_id`` as start."""
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_end(
                    run_id,
                    tool_name=self.name,
                    action=operation,
                    success=success,
                    output_size=output_size,
                    duration_ms=duration_ms,
                    result_summary=result_summary,
                )

    def _emit_tool_error(self, run_id: UUID, operation: str, error: Exception) -> None:
        """Send tool-execution-error event using the same ``run_id`` as start."""
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_error(
                    run_id,
                    tool_name=self.name,
                    action=operation,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )


# ------------------------------------------------------------------
# Global registration
# ------------------------------------------------------------------


def _computer_use_factory(**kwargs: Any) -> ComputerUseTool:
    """Factory that auto-selects the best available runtime."""
    kwargs.setdefault("runtime_name", "auto")
    return ComputerUseTool(**kwargs)


register_tool_factory("computer_use", _computer_use_factory)
