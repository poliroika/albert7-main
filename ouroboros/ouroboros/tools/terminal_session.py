"""Persistent per-workspace terminal sessions for Ouroboros.

Replaces the one-shot ``subprocess.Popen`` in
:func:`ouroboros.tools.umbrella_tools.run_workspace_command` with a long-lived
shell where ``cd``, ``export``, and background jobs survive between calls.

Three backends are provided, selected automatically at construction time:

1. ``TmuxBackend`` -- preferred. Used whenever the ``tmux`` binary is on
   ``PATH``. Drives a detached session and reads scrollback via
   ``capture-pane``. Works inside Terminal-Bench containers (which already
   speak tmux) and on any POSIX host with tmux installed.
2. ``BashSubprocessBackend`` -- fallback. Long-lived ``bash --login`` driven
   over stdin/stdout pipes. Used on POSIX hosts without tmux.
3. ``OneShotBackend`` -- last resort, used on Windows. Spawns a fresh process
   per call, exactly like the legacy implementation. State does **not**
   persist between calls in this mode, but the public contract is preserved
   so the rest of Ouroboros does not need a Windows-specific code path.

All backends return a :class:`RunResult` with the same shape so callers
never need to branch on backend type.
"""

import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from collections.abc import Sequence

log = logging.getLogger(__name__)


_END_MARKER_PREFIX = "__A7_END__"
_HARD_OUTPUT_LIMIT = (
    60000  # head/tail truncated for tool result; full slice goes to scrollback
)
_RECOVERY_GRACE_SECONDS = 5.0
_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\)|[@-_])"
)
_MACOS_ZSH_BANNER_LINES = {
    "The default interactive shell is now zsh.",
    "To update your account to use zsh, please run `chsh -s /bin/zsh`.",
    "For more details, please visit https://support.apple.com/kb/HT208050.",
}


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Kill ``proc`` and all of its descendants.

    Windows ``Popen.kill()`` only signals the immediate child, which leaves
    grandchildren (e.g. ``uv run python -m uvicorn`` -> ``python.exe`` ->
    bound socket) alive. This helper uses ``taskkill /F /T`` on Windows and
    ``killpg`` on POSIX so a timed-out shell call cannot leak a server.
    """
    if proc is None or proc.poll() is not None:
        return
    pid = proc.pid
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            log.debug("taskkill /T failed for pid=%s", pid, exc_info=True)
            try:
                proc.kill()
            except Exception:
                log.debug("Popen.kill fallback failed", exc_info=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
            time.sleep(0.5)
            if proc.poll() is None:
                os.killpg(pid, signal.SIGKILL)
        except Exception:
            log.debug("killpg failed for pid=%s", pid, exc_info=True)
            try:
                proc.kill()
            except Exception:
                log.debug("Popen.kill fallback failed", exc_info=True)


def _truncate_for_tool_result(
    output: str, *, limit: int = _HARD_OUTPUT_LIMIT
) -> tuple[str, bool, bool]:
    """Truncate ``output`` head+tail style, mirroring legacy behaviour.

    Returns ``(truncated_output, truncated_head, truncated_tail)``.
    """
    if len(output) <= limit:
        return output, False, False
    half = limit // 2
    head = output[:half]
    tail = output[-half:]
    return head + "\n...(truncated)...\n" + tail, True, True


def _strip_shell_startup_noise(output: str) -> str:
    """Remove terminal control escapes and known shell banner lines."""
    if not output:
        return output
    text = output.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_ESCAPE_RE.sub("", text)
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip() in _MACOS_ZSH_BANNER_LINES:
            continue
        lines.append(line)
    return "\n".join(lines)


@dataclass
class RunResult:
    exit_code: int
    output: str
    marker: str
    started_at: float
    finished_at: float
    truncated_head: bool = False
    truncated_tail: bool = False
    timed_out: bool = False
    session_recovered: bool = False
    raw_output: str = ""

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


def _bash_quote(s: str) -> str:
    return shlex.quote(s)


def _compose_command_line(
    cmd: Sequence[str] | str, *, cwd: str | None, marker: str
) -> str:
    """Build the bash one-liner sent to the shell.

    The trailing ``printf`` is the marker we look for to know the command
    finished and to recover its exit code.
    """
    if isinstance(cmd, (list, tuple)):
        cmd_str = " ".join(_bash_quote(part) for part in cmd)
    else:
        cmd_str = str(cmd)

    parts: list[str] = []
    if cwd:
        parts.append(f"cd {_bash_quote(cwd)}")
    parts.append(f"{{ {cmd_str}; }}")
    body = " && ".join(parts)
    end = f"printf '\\n{_END_MARKER_PREFIX}%s__%d__\\n' {marker} $?"
    return f"{body}; {end}"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _BackendBase:
    name = "base"

    def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def run(
        self, cmd: Sequence[str] | str, *, cwd: str | None, timeout: int
    ) -> RunResult:  # pragma: no cover
        raise NotImplementedError

    def view(
        self, *, last_lines: int = 200, grep: str | None = None
    ) -> str:  # pragma: no cover
        raise NotImplementedError

    def reset(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def kill(self) -> None:  # pragma: no cover
        raise NotImplementedError


class TmuxBackend(_BackendBase):
    """Preferred backend: drives a detached ``tmux`` session."""

    name = "tmux"

    def __init__(
        self, session_name: str, *, width: int = 200, height: int = 50
    ) -> None:
        self.session_name = session_name
        self.width = width
        self.height = height
        # Reentrant lock is required because `run()` may call `reset()`
        # from timeout-recovery paths while already holding the backend lock.
        # With a plain Lock this deadlocks and the outer watchdog fires only
        # after the global tool timeout (observed as "run_workspace_command hangs").
        self._lock = threading.RLock()
        self._scrollback_seen = ""

    @staticmethod
    def is_available() -> bool:
        return bool(shutil.which("tmux"))

    def _tmux(
        self,
        *args: str,
        check: bool = True,
        capture: bool = True,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", *args],
            check=check,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def start(self) -> None:
        try:
            self._tmux("has-session", "-t", self.session_name, check=True)
            return
        except subprocess.CalledProcessError:
            pass
        except subprocess.TimeoutExpired:
            log.warning(
                "tmux has-session timed out; trying to start fresh", exc_info=True
            )
        self._tmux(
            "new-session",
            "-d",
            "-s",
            self.session_name,
            "-x",
            str(self.width),
            "-y",
            str(self.height),
            "env",
            "PS1=",
            "TERM=dumb",
            "bash",
            "--noprofile",
            "--norc",
            "-i",
        )
        # Disable status bar / give shell a moment to print prompt.
        try:
            self._tmux(
                "set-option", "-t", self.session_name, "status", "off", check=False
            )
        except Exception:
            pass
        time.sleep(0.05)
        self._scrollback_seen = ""

    def _capture(self, *, lines: int = 5000) -> str:
        try:
            cp = self._tmux(
                "capture-pane",
                "-p",
                "-J",
                "-t",
                f"{self.session_name}:0",
                "-S",
                f"-{lines}",
                check=True,
            )
            return cp.stdout
        except subprocess.CalledProcessError as e:
            log.debug("tmux capture-pane failed: %s", e.stderr)
            return ""
        except subprocess.TimeoutExpired:
            log.warning("tmux capture-pane timed out")
            return ""

    def _send_line(self, line: str) -> None:
        # `-l` means literal: do not interpret as a key name. Then send Enter.
        self._tmux("send-keys", "-t", f"{self.session_name}:0", "-l", line, check=True)
        self._tmux("send-keys", "-t", f"{self.session_name}:0", "Enter", check=True)

    def _send_ctrl_c(self) -> None:
        try:
            self._tmux("send-keys", "-t", f"{self.session_name}:0", "C-c", check=False)
        except Exception:
            log.debug("tmux send-keys C-c failed", exc_info=True)

    def run(
        self, cmd: Sequence[str] | str, *, cwd: str | None, timeout: int
    ) -> RunResult:
        with self._lock:
            self.start()
            marker = secrets.token_hex(8)
            line = _compose_command_line(cmd, cwd=cwd, marker=marker)
            end_re = re.compile(
                rf"{re.escape(_END_MARKER_PREFIX)}{re.escape(marker)}__(-?\d+)__"
            )

            # Snapshot scrollback BEFORE sending: anything new after this
            # point is the output of our command.
            pre_capture = self._capture(lines=2000)
            pre_len = len(pre_capture)

            started = time.time()
            try:
                self._send_line(line)
            except Exception as e:
                finished = time.time()
                return RunResult(
                    exit_code=-1,
                    output=f"WARNING: failed to send command to tmux: {e}",
                    marker=marker,
                    started_at=started,
                    finished_at=finished,
                    timed_out=False,
                    raw_output="",
                )

            deadline = started + max(1, int(timeout))
            poll_interval = 0.1
            recovered = False
            captured = ""
            match = None

            while True:
                now = time.time()
                if now >= deadline:
                    self._send_ctrl_c()
                    time.sleep(0.2)
                    self._send_ctrl_c()
                    grace_deadline = time.time() + _RECOVERY_GRACE_SECONDS
                    while time.time() < grace_deadline:
                        captured = self._capture(lines=4000)
                        match = (
                            end_re.search(captured[pre_len:])
                            if len(captured) >= pre_len
                            else end_re.search(captured)
                        )
                        if match:
                            break
                        time.sleep(0.2)
                    if not match:
                        log.warning(
                            "tmux command did not respond to C-c; recreating session"
                        )
                        self.reset()
                        recovered = True
                        finished = time.time()
                        return RunResult(
                            exit_code=124,
                            output=(
                                f"WARNING: command exceeded the per-call timeout of {timeout}s "
                                "and the tmux session had to be recreated. State (cwd, env vars, "
                                "background jobs) was lost."
                            ),
                            marker=marker,
                            started_at=started,
                            finished_at=finished,
                            timed_out=True,
                            session_recovered=True,
                            raw_output="",
                        )
                    finished = time.time()
                    output_slice = captured[pre_len : match.start()].rstrip("\n")
                    output_slice = (
                        f"WARNING: command exceeded timeout of {timeout}s and was interrupted with SIGINT.\n"
                        + output_slice
                    )
                    truncated, th, tt = _truncate_for_tool_result(output_slice)
                    return RunResult(
                        exit_code=int(match.group(1)),
                        output=truncated,
                        marker=marker,
                        started_at=started,
                        finished_at=finished,
                        truncated_head=th,
                        truncated_tail=tt,
                        timed_out=True,
                        raw_output=output_slice,
                    )

                captured = self._capture(lines=4000)
                if len(captured) >= pre_len:
                    haystack = captured[pre_len:]
                else:
                    # tmux scrolled past our anchor (extremely chatty command);
                    # search the whole pane.
                    haystack = captured
                match = end_re.search(haystack)
                if match:
                    finished = time.time()
                    output_slice = haystack[: match.start()]
                    output_slice = self._strip_echoed_command(output_slice, line)
                    output_slice = _strip_shell_startup_noise(output_slice)
                    truncated, th, tt = _truncate_for_tool_result(output_slice)
                    return RunResult(
                        exit_code=int(match.group(1)),
                        output=truncated.rstrip("\n"),
                        marker=marker,
                        started_at=started,
                        finished_at=finished,
                        truncated_head=th,
                        truncated_tail=tt,
                        session_recovered=recovered,
                        raw_output=output_slice.rstrip("\n"),
                    )

                time.sleep(poll_interval)
                if poll_interval < 0.4:
                    poll_interval = min(0.4, poll_interval * 1.5)

    @staticmethod
    def _strip_echoed_command(output: str, sent_line: str) -> str:
        """Drop the leading prompt+echo of the command we just sent.

        tmux capture-pane includes the literal prompt plus the typed command
        before the actual command output. We try to slice that off so the
        tool result is just the program's stdout/stderr.
        """
        if not output:
            return output
        idx = output.find(sent_line)
        if idx < 0:
            return output
        end_of_line = output.find("\n", idx + len(sent_line))
        if end_of_line < 0:
            return output[idx + len(sent_line) :]
        return output[end_of_line + 1 :]

    def view(self, *, last_lines: int = 200, grep: str | None = None) -> str:
        with self._lock:
            try:
                self.start()
            except Exception:
                return ""
            captured = self._capture(lines=max(50, int(last_lines) + 50))
        lines = captured.splitlines()
        if last_lines and last_lines > 0:
            lines = lines[-int(last_lines) :]
        if grep:
            try:
                pat = re.compile(grep)
                lines = [ln for ln in lines if pat.search(ln)]
            except re.error as e:
                return f"WARNING: invalid grep regex: {e}"
        return "\n".join(lines)

    def reset(self) -> None:
        with self._lock:
            self.kill()
            self.start()

    def kill(self) -> None:
        try:
            self._tmux("kill-session", "-t", self.session_name, check=False)
        except Exception:
            log.debug("tmux kill-session failed", exc_info=True)


class BashSubprocessBackend(_BackendBase):
    """POSIX fallback when tmux is unavailable."""

    name = "bash"

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        # Reentrant lock is required because timeout-recovery in `run()`
        # calls `reset()`, which acquires the same lock again.
        self._lock = threading.RLock()
        self._buffer = ""
        self._reader_thread: threading.Thread | None = None
        self._buf_lock = threading.Lock()

    @staticmethod
    def is_available() -> bool:
        return os.name == "posix" and bool(shutil.which("bash"))

    def _spawn(self) -> None:
        env = os.environ.copy()
        env.setdefault("PS1", "")  # quiet prompt
        env.setdefault("TERM", "dumb")
        self._proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile", "-i"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        self._buffer = ""

        def _pump() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            try:
                # IMPORTANT: avoid fixed-size blocking reads like read(4096).
                # They can stall until enough bytes accumulate, which delays
                # marker delivery and makes run() appear "hung". Line-buffered
                # reads flush promptly because every command terminator marker
                # we emit ends with '\n'.
                for line in iter(self._proc.stdout.readline, ""):
                    if not line:
                        break
                    with self._buf_lock:
                        self._buffer += line
            except Exception:
                log.debug("bash backend reader thread crashed", exc_info=True)

        self._reader_thread = threading.Thread(
            target=_pump, name="umbrella-bash-reader", daemon=True
        )
        self._reader_thread.start()

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._spawn()

    def _read_buffer(self) -> str:
        with self._buf_lock:
            return self._buffer

    def _truncate_buffer_to(self, idx: int) -> None:
        with self._buf_lock:
            if idx > 0 and idx <= len(self._buffer):
                self._buffer = self._buffer[idx:]

    def run(
        self, cmd: Sequence[str] | str, *, cwd: str | None, timeout: int
    ) -> RunResult:
        with self._lock:
            self.start()
            assert self._proc is not None and self._proc.stdin is not None

            marker = secrets.token_hex(8)
            line = _compose_command_line(cmd, cwd=cwd, marker=marker)
            end_re = re.compile(
                rf"{re.escape(_END_MARKER_PREFIX)}{re.escape(marker)}__(-?\d+)__"
            )

            # Drain anything left in the buffer (prompt banner etc.) so we
            # only collect output produced by THIS command.
            with self._buf_lock:
                self._buffer = ""

            started = time.time()
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                finished = time.time()
                return RunResult(
                    exit_code=-1,
                    output=f"WARNING: failed to write to bash backend: {e}",
                    marker=marker,
                    started_at=started,
                    finished_at=finished,
                )

            deadline = started + max(1, int(timeout))
            poll_interval = 0.05
            while True:
                now = time.time()
                buf = self._read_buffer()
                match = end_re.search(buf)
                if match:
                    finished = time.time()
                    output_slice = buf[: match.start()]
                    output_slice = self._strip_echo(output_slice, line)
                    output_slice = _strip_shell_startup_noise(output_slice)
                    truncated, th, tt = _truncate_for_tool_result(output_slice)
                    return RunResult(
                        exit_code=int(match.group(1)),
                        output=truncated.rstrip("\n"),
                        marker=marker,
                        started_at=started,
                        finished_at=finished,
                        truncated_head=th,
                        truncated_tail=tt,
                        raw_output=output_slice.rstrip("\n"),
                    )

                if now >= deadline:
                    # Try SIGINT then SIGKILL to recover.
                    if self._proc.poll() is None:
                        try:
                            os.killpg(self._proc.pid, signal.SIGINT)
                        except Exception:
                            log.debug("SIGINT failed", exc_info=True)
                        time.sleep(0.3)
                        buf = self._read_buffer()
                        match = end_re.search(buf)
                        if match:
                            finished = time.time()
                            output_slice = buf[: match.start()]
                            output_slice = self._strip_echo(output_slice, line)
                            output_slice = _strip_shell_startup_noise(output_slice)
                            output_slice = (
                                f"WARNING: command exceeded timeout of {timeout}s and was interrupted with SIGINT.\n"
                                + output_slice
                            )
                            truncated, th, tt = _truncate_for_tool_result(output_slice)
                            return RunResult(
                                exit_code=int(match.group(1)),
                                output=truncated.rstrip("\n"),
                                marker=marker,
                                started_at=started,
                                finished_at=finished,
                                truncated_head=th,
                                truncated_tail=tt,
                                timed_out=True,
                                raw_output=output_slice.rstrip("\n"),
                            )
                    # Recovery: kill subprocess and respawn.
                    self.reset()
                    finished = time.time()
                    return RunResult(
                        exit_code=124,
                        output=(
                            f"WARNING: command exceeded the per-call timeout of {timeout}s "
                            "and the bash session had to be recreated. State was lost."
                        ),
                        marker=marker,
                        started_at=started,
                        finished_at=finished,
                        timed_out=True,
                        session_recovered=True,
                    )

                time.sleep(poll_interval)
                if poll_interval < 0.3:
                    poll_interval = min(0.3, poll_interval * 1.5)

    @staticmethod
    def _strip_echo(output: str, sent_line: str) -> str:
        if not output:
            return output
        idx = output.find(sent_line)
        if idx < 0:
            return output
        end_of_line = output.find("\n", idx + len(sent_line))
        if end_of_line < 0:
            return output[idx + len(sent_line) :]
        return output[end_of_line + 1 :]

    def view(self, *, last_lines: int = 200, grep: str | None = None) -> str:
        # No real "scrollback" for the bash backend. Best effort: return the
        # tail of the *current* unconsumed buffer, which is usually empty.
        text = self._read_buffer()
        lines = text.splitlines()
        if last_lines and last_lines > 0:
            lines = lines[-int(last_lines) :]
        if grep:
            try:
                pat = re.compile(grep)
                lines = [ln for ln in lines if pat.search(ln)]
            except re.error as e:
                return f"WARNING: invalid grep regex: {e}"
        return "\n".join(lines)

    def reset(self) -> None:
        with self._lock:
            self.kill()
            self._spawn()

    def kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        proc.kill()
        except Exception:
            log.debug("bash backend kill failed", exc_info=True)
        finally:
            self._proc = None


class OneShotBackend(_BackendBase):
    """Last-resort backend (Windows / no bash). Spawns a fresh process per call.

    State does NOT persist between calls. The contract is preserved so
    callers don't need a Windows-specific code path.
    """

    name = "oneshot"

    def __init__(self) -> None:
        self._scrollback: list[str] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        return

    def _record(self, line: str) -> None:
        self._scrollback.append(line)
        if len(self._scrollback) > 4000:
            self._scrollback = self._scrollback[-2000:]

    @staticmethod
    def _resolve_windows_executable(argv: list[str]) -> list[str]:
        if os.name != "nt" or not argv:
            return argv
        executable = argv[0]
        if not executable or any(sep in executable for sep in ("/", "\\")):
            return argv
        resolved = shutil.which(executable)
        if not resolved:
            return argv
        return [resolved, *argv[1:]]

    def run(
        self, cmd: Sequence[str] | str, *, cwd: str | None, timeout: int
    ) -> RunResult:
        with self._lock:
            marker = secrets.token_hex(8)
            started = time.time()
            popen_kwargs: dict = {}
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            else:
                # Windows: a fresh process group lets us reliably reap the entire
                # subtree (uv -> python -> uvicorn -> ...) on timeout via taskkill /T.
                popen_kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            argv: list[str] | str
            if isinstance(cmd, (list, tuple)):
                argv = self._resolve_windows_executable(list(cmd))
                shell = False
            else:
                argv = str(cmd)
                shell = True
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=shell,
                    **popen_kwargs,
                )
            except FileNotFoundError as e:
                finished = time.time()
                return RunResult(
                    exit_code=127,
                    output=f"WARNING: command not found: {e}",
                    marker=marker,
                    started_at=started,
                    finished_at=finished,
                )

            timed_out = False
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
            finished = time.time()
            output = stdout + ("\n--- STDERR ---\n" + stderr if stderr else "")
            if timed_out:
                output = (
                    f"WARNING: command exceeded the per-call timeout of {timeout}s and was killed.\n"
                    + output
                )
            truncated, th, tt = _truncate_for_tool_result(output)
            cmd_repr = (
                cmd if isinstance(cmd, str) else " ".join(_bash_quote(p) for p in cmd)
            )
            self._record(f"$ {cmd_repr}")
            for ln in output.splitlines()[-200:]:
                self._record(ln)
            return RunResult(
                exit_code=124 if timed_out else int(proc.returncode or 0),
                output=truncated.rstrip("\n"),
                marker=marker,
                started_at=started,
                finished_at=finished,
                truncated_head=th,
                truncated_tail=tt,
                timed_out=timed_out,
                raw_output=output.rstrip("\n"),
            )

    def view(self, *, last_lines: int = 200, grep: str | None = None) -> str:
        with self._lock:
            lines = list(self._scrollback)
        if last_lines and last_lines > 0:
            lines = lines[-int(last_lines) :]
        if grep:
            try:
                pat = re.compile(grep)
                lines = [ln for ln in lines if pat.search(ln)]
            except re.error as e:
                return f"WARNING: invalid grep regex: {e}"
        return "\n".join(lines)

    def reset(self) -> None:
        with self._lock:
            self._scrollback.clear()

    def kill(self) -> None:
        return


# ---------------------------------------------------------------------------
# Public TerminalSession
# ---------------------------------------------------------------------------


def _select_backend(workspace_id: str, *, prefer: str | None = None) -> _BackendBase:
    """Pick the best available backend for this host.

    ``prefer`` allows tests / env-var overrides: ``"tmux"``, ``"bash"`` or
    ``"oneshot"``.
    """
    forced = (
        (prefer or os.environ.get("OUROBOROS_TERMINAL_BACKEND") or "").strip().lower()
    )
    safe_id = (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", str(workspace_id) or "default")[:40]
        or "default"
    )
    session_name = f"umbrella-{safe_id}-{os.getpid()}"

    if forced == "oneshot":
        return OneShotBackend()
    if forced == "bash" and BashSubprocessBackend.is_available():
        return BashSubprocessBackend()
    if forced == "tmux":
        if TmuxBackend.is_available():
            return TmuxBackend(session_name)
        log.warning(
            "OUROBOROS_TERMINAL_BACKEND=tmux but tmux is not on PATH; falling back"
        )

    if TmuxBackend.is_available():
        return TmuxBackend(session_name)
    if BashSubprocessBackend.is_available():
        return BashSubprocessBackend()
    return OneShotBackend()


@dataclass
class TerminalSession:
    """Persistent shell session for a workspace.

    One instance per ``(ouroboros_run, workspace_id)``. Stored on
    ``ctx._terminal_sessions`` and torn down when Ouroboros exits.
    """

    workspace_id: str
    backend: _BackendBase = field(default_factory=lambda: OneShotBackend())
    _started: bool = False

    @classmethod
    def for_workspace(
        cls, workspace_id: str, *, prefer_backend: str | None = None
    ) -> "TerminalSession":
        backend = _select_backend(workspace_id, prefer=prefer_backend)
        return cls(workspace_id=workspace_id, backend=backend)

    def _ensure_started(self) -> None:
        if not self._started:
            try:
                self.backend.start()
            finally:
                self._started = True

    def run(
        self, cmd: Sequence[str] | str, *, cwd: str | None = None, timeout: int = 180
    ) -> RunResult:
        self._ensure_started()
        return self.backend.run(cmd, cwd=cwd, timeout=timeout)

    def view(self, *, last_lines: int = 200, grep: str | None = None) -> str:
        self._ensure_started()
        return self.backend.view(last_lines=last_lines, grep=grep)

    def reset(self) -> None:
        self.backend.reset()
        self._started = True

    def kill(self) -> None:
        try:
            self.backend.kill()
        finally:
            self._started = False

    @property
    def backend_name(self) -> str:
        return self.backend.name


# ---------------------------------------------------------------------------
# Context-side helpers (used from umbrella_tools.py and the loop)
# ---------------------------------------------------------------------------


_SESSIONS_ATTR = "_terminal_sessions"


def get_or_create_session(ctx: object, workspace_id: str) -> TerminalSession:
    """Return the persistent session for this workspace, creating if needed."""
    if not hasattr(ctx, _SESSIONS_ATTR):
        try:
            setattr(ctx, _SESSIONS_ATTR, {})
        except Exception:
            # ctx is e.g. a frozen dataclass; fall back to per-call session.
            return TerminalSession.for_workspace(workspace_id)
    sessions: dict = getattr(ctx, _SESSIONS_ATTR)
    sess = sessions.get(workspace_id)
    if sess is None:
        sess = TerminalSession.for_workspace(workspace_id)
        sessions[workspace_id] = sess
        log.info(
            "TerminalSession created for workspace=%s backend=%s",
            workspace_id,
            sess.backend_name,
        )
    return sess


def shutdown_all_sessions(ctx: object) -> None:
    """Best-effort teardown of every session attached to ``ctx``.

    Called from the Ouroboros loop's ``finally`` block so we don't leave
    orphan tmux servers / bash subprocesses running.
    """
    sessions = getattr(ctx, _SESSIONS_ATTR, None)
    if not sessions:
        return
    for ws_id, sess in list(sessions.items()):
        try:
            sess.kill()
        except Exception:
            log.debug(
                "shutdown_all_sessions: failed to kill session for %s",
                ws_id,
                exc_info=True,
            )
    try:
        sessions.clear()
    except Exception:
        pass


__all__ = [
    "RunResult",
    "TerminalSession",
    "TmuxBackend",
    "BashSubprocessBackend",
    "OneShotBackend",
    "get_or_create_session",
    "shutdown_all_sessions",
]
