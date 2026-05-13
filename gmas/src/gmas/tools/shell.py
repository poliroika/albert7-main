"""
Shell tool — shell command execution.

Allows agents to execute commands in the system shell.
Supports timeouts and output size limits.
"""

import contextlib
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from .base import BaseTool, ToolResult


class ShellTool(BaseTool):
    """
    Tool for executing shell commands.

    Security:
        - Commands are executed in a subprocess with a timeout
        - Output is size-limited to prevent overflow
        - shell=True is used on all platforms to support pipes and builtins;
          restrict untrusted input with the ``allowed_commands`` allowlist

    Example:
        tool = ShellTool(timeout=30, max_output_size=4096)
        result = tool.execute(command="ls -la")

        if result.success:
            print(result.output)
        else:
            print(f"Error: {result.error}")

    """

    def __init__(
        self,
        timeout: int = 30,
        max_output_size: int = 8192,
        working_dir: str | None = None,
        allowed_commands: list[str] | None = None,
        callback_manager: Any | None = None,
    ):
        """
        Create ShellTool.

        Args:
            timeout: Maximum command execution time in seconds.
            max_output_size: Maximum output size in bytes.
            working_dir: Working directory for commands.
            allowed_commands: Whitelist of allowed commands (None = all).
            callback_manager: CallbackManager for sending events.
                If ``None``, the context manager is used on each call.

        """
        self._timeout = timeout
        self._max_output_size = max_output_size
        self._working_dir = working_dir
        self._allowed_commands = set(allowed_commands) if allowed_commands else None
        self._callback_manager = callback_manager

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output. "
            "Use for system operations, file manipulation, or running scripts."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
            },
            "required": ["command"],
        }

    def _is_command_allowed(self, command: str) -> bool:
        """Check whether the command is allowed."""
        if self._allowed_commands is None:
            return True

        # Extract the first word (command name)
        cmd_name = command.strip().split()[0] if command.strip() else ""
        return cmd_name in self._allowed_commands

    # ------------------------------------------------------------------
    # Callback helpers
    # ------------------------------------------------------------------

    def _get_callback_manager(self) -> Any | None:
        """Return callback manager from constructor or from context."""
        if self._callback_manager is not None:
            return self._callback_manager
        with contextlib.suppress(Exception):
            from gmas.callbacks.context import get_callback_manager

            return get_callback_manager()
        return None

    def _emit_tool_start(self, arguments: dict[str, Any]) -> None:
        """Send tool-execution-start event."""
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_start(
                    uuid4(),
                    tool_name=self.name,
                    arguments=arguments,
                )

    def _emit_tool_end(
        self,
        *,
        success: bool = True,
        output_size: int = 0,
        duration_ms: float = 0.0,
        result_summary: str = "",
    ) -> None:
        """Send tool-execution-end event."""
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_end(
                    uuid4(),
                    tool_name=self.name,
                    success=success,
                    output_size=output_size,
                    duration_ms=duration_ms,
                    result_summary=result_summary,
                )

    def _emit_tool_error(self, error: Exception) -> None:
        """Send tool-execution-error event."""
        cb = self._get_callback_manager()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.on_tool_error(
                    uuid4(),
                    tool_name=self.name,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, command: str = "", **_kwargs: Any) -> ToolResult:
        """
        Execute a shell command.

        Args:
            command: Command to execute.

        Returns:
            ToolResult with the command output or error.

        """
        if not command:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="No command provided",
            )

        if not self._is_command_allowed(command):
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Command not allowed: {command.split(maxsplit=1)[0]}",
            )

        self._emit_tool_start({"command": command})
        _start = time.monotonic()
        try:
            # Determine shell based on OS.
            # On both platforms shell=True is required because the command is a
            # raw string that may contain pipes, redirection, or built-ins.
            # The security trade-off is documented in the class docstring.
            if sys.platform == "win32":
                # Windows: cmd.exe is used implicitly via shell=True.
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    cwd=self._working_dir,
                    check=False,
                )
            else:
                # On Unix use /bin/sh
                result = subprocess.run(
                    command,
                    shell=True,
                    executable="/bin/sh",
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    cwd=self._working_dir,
                    check=False,
                )

            # Merge stdout and stderr
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"

            # Limit output size
            if len(output) > self._max_output_size:
                output = output[: self._max_output_size] + "\n... (output truncated)"

            elapsed_ms = (time.monotonic() - _start) * 1000

            if result.returncode != 0:
                error_msg = f"Command exited with code {result.returncode}"
                self._emit_tool_end(
                    success=False,
                    output_size=len(output),
                    duration_ms=elapsed_ms,
                    result_summary=error_msg,
                )
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    output=output,
                    error=error_msg,
                )

            final_output = output.strip() if output else "(no output)"
            self._emit_tool_end(
                success=True,
                output_size=len(final_output),
                duration_ms=elapsed_ms,
            )
            return ToolResult(
                tool_name=self.name,
                success=True,
                output=final_output,
            )

        except subprocess.TimeoutExpired as e:
            self._emit_tool_error(e)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Command timed out after {self._timeout} seconds",
            )
        except FileNotFoundError as e:
            self._emit_tool_error(e)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="Command not found",
            )
        except (OSError, ValueError) as e:
            self._emit_tool_error(e)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Execution error: {e}",
            )
