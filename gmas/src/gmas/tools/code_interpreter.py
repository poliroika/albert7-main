"""
Code Interpreter tool — Python code execution.

Allows agents to execute Python code in an isolated subprocess.
Supports real timeouts (via subprocess kill) and output limits.
In safe_mode the subprocess restricts builtins and available imports.
"""

import contextlib
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolResult

_SAFE_BUILTINS_NAMES: list[str] = [
    "bool",
    "int",
    "float",
    "str",
    "list",
    "dict",
    "tuple",
    "set",
    "frozenset",
    "bytes",
    "bytearray",
    "abs",
    "all",
    "any",
    "bin",
    "chr",
    "divmod",
    "enumerate",
    "filter",
    "format",
    "hash",
    "hex",
    "len",
    "map",
    "max",
    "min",
    "oct",
    "ord",
    "pow",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "slice",
    "sorted",
    "sum",
    "zip",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "ZeroDivisionError",
    "StopIteration",
    "ArithmeticError",
    "LookupError",
    "RuntimeError",
    "OverflowError",
    "True",
    "False",
    "None",
    "isinstance",
    "issubclass",
    "type",
    "callable",
    "hasattr",
    "getattr",
    "setattr",
    "iter",
    "next",
    "id",
    "object",
    "property",
    "staticmethod",
    "classmethod",
    "super",
]

_SAFE_MODULES: list[str] = [
    "math",
    "statistics",
    "json",
    "re",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "random",
]

_SANDBOX_PREAMBLE = textwrap.dedent("""\
    import builtins as _builtins

    _ALLOWED_NAMES = {allowed_names!r}
    _ALLOWED_MODULES = set({allowed_modules!r})

    _safe = {{k: getattr(_builtins, k) for k in _ALLOWED_NAMES if hasattr(_builtins, k)}}
    _safe["input"] = lambda *_a, **_kw: ""

    _real_import = _builtins.__import__

    def _restricted_import(name, *args, **kwargs):
        if name not in _ALLOWED_MODULES:
            raise ImportError(f"Import of '{{name}}' is not allowed in safe mode")
        return _real_import(name, *args, **kwargs)

    _safe["__import__"] = _restricted_import

    _globals = {{"__builtins__": _safe}}

    _user_code = {user_code!r}

    try:
        _compiled = compile(_user_code, "<code>", "eval")
        _result = eval(_compiled, _globals)
        if _result is not None:
            print(_result)
    except SyntaxError:
        exec(compile(_user_code, "<code>", "exec"), _globals)
""")

_UNSAFE_PREAMBLE = textwrap.dedent("""\
    _user_code = {user_code!r}

    try:
        _compiled = compile(_user_code, "<code>", "eval")
        _result = eval(_compiled)
        if _result is not None:
            print(_result)
    except SyntaxError:
        exec(compile(_user_code, "<code>", "exec"))
""")


class CodeInterpreterTool(BaseTool):
    """
    Tool for executing Python code.

    Executes Python code in an isolated subprocess. Supports:
    - Execution time limit (enforced via subprocess kill)
    - Output size limit
    - Safe sandbox (restricted builtins, process-isolated)

    Example:
        tool = CodeInterpreterTool(timeout=10, max_output_size=4096)
        result = tool.execute(code="print(2 + 2)")

        if result.success:
            print(result.output)  # "4"
        else:
            print(f"Error: {result.error}")

    """

    def __init__(
        self,
        timeout: int = 30,
        max_output_size: int = 8192,
        *,
        safe_mode: bool = True,
    ):
        """
        Create CodeInterpreterTool.

        Args:
            timeout: Maximum execution time in seconds.
            max_output_size: Maximum output size in bytes.
            safe_mode: If True, restricts available builtins for safety.

        """
        self._timeout = timeout
        self._max_output_size = max_output_size
        self._safe_mode = safe_mode

        self._safe_builtins = {
            "bool": bool,
            "int": int,
            "float": float,
            "str": str,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "set": set,
            "frozenset": frozenset,
            "bytes": bytes,
            "bytearray": bytearray,
            "abs": abs,
            "all": all,
            "any": any,
            "bin": bin,
            "chr": chr,
            "divmod": divmod,
            "enumerate": enumerate,
            "filter": filter,
            "format": format,
            "hash": hash,
            "hex": hex,
            "len": len,
            "map": map,
            "max": max,
            "min": min,
            "oct": oct,
            "ord": ord,
            "pow": pow,
            "print": print,
            "range": range,
            "repr": repr,
            "reversed": reversed,
            "round": round,
            "slice": slice,
            "sorted": sorted,
            "sum": sum,
            "zip": zip,
            "Exception": Exception,
            "ValueError": ValueError,
            "TypeError": TypeError,
            "KeyError": KeyError,
            "IndexError": IndexError,
            "ZeroDivisionError": ZeroDivisionError,
            "True": True,
            "False": False,
            "None": None,
            "isinstance": isinstance,
            "issubclass": issubclass,
            "type": type,
            "callable": callable,
            "hasattr": hasattr,
            "getattr": getattr,
            "setattr": setattr,
            "iter": iter,
            "next": next,
            "input": lambda _: "",
        }

    @property
    def name(self) -> str:
        return "code_interpreter"

    @property
    def description(self) -> str:
        return (
            "Execute Python code and return the output. "
            "Use for calculations, data processing, and algorithmic tasks. "
            "The code runs in a sandboxed subprocess with a real timeout."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Can be multi-line.",
                },
            },
            "required": ["code"],
        }

    def _get_safe_globals(self) -> dict[str, Any]:
        """Get safe globals for in-process fallback (kept for API compat)."""
        import collections
        import datetime
        import functools
        import itertools
        import json
        import math
        import random
        import re
        import statistics

        return {
            "__builtins__": self._safe_builtins if self._safe_mode else __builtins__,
            "math": math,
            "statistics": statistics,
            "json": json,
            "re": re,
            "datetime": datetime,
            "collections": collections,
            "itertools": itertools,
            "functools": functools,
            "random": random,
        }

    def _build_script(self, code: str) -> str:
        """Build the full Python script to run in the subprocess."""
        if self._safe_mode:
            return _SANDBOX_PREAMBLE.format(
                allowed_names=_SAFE_BUILTINS_NAMES,
                allowed_modules=_SAFE_MODULES,
                user_code=code,
            )
        return _UNSAFE_PREAMBLE.format(user_code=code)

    def execute(self, code: str = "", **_kwargs: Any) -> ToolResult:
        """
        Execute Python code in an isolated subprocess.

        Args:
            code: Python code to execute.

        Returns:
            ToolResult with output or error.

        """
        if not code:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="No code provided",
            )

        script = self._build_script(code)
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                delete=False,
                encoding="utf-8",
            )
            tmp.write(script)
            tmp.flush()
            tmp.close()

            proc = subprocess.Popen(
                [sys.executable, "-u", tmp.name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error=f"Code execution timed out after {self._timeout} seconds",
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                error_msg = stderr.strip() or f"Process exited with code {proc.returncode}"
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error=self._truncate(error_msg),
                    output=self._truncate(stdout),
                )

            output = stdout
            if stderr.strip():
                output += f"\n[stderr]\n{stderr}"

            output = self._truncate(output)

            return ToolResult(
                tool_name=self.name,
                success=True,
                output=output.strip() if output else "(no output)",
            )

        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Subprocess launch error: {e}",
            )
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    Path(tmp.name).unlink()

    def _truncate(self, text: str) -> str:
        """Truncate text to max_output_size."""
        if len(text) > self._max_output_size:
            return text[: self._max_output_size] + "\n... (output truncated)"
        return text
