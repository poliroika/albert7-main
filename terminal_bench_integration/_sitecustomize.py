"""Reference copy of the sitecustomize patch installed into the venv.

The actual file lives at `.venv/Lib/site-packages/sitecustomize.py` and is
written there by `terminal_bench_integration.cli` on first run (or by
`terminal_bench_integration._install_sitecustomize` if invoked manually).
This module is kept inside the package as a single source of truth so
that the patch can be re-applied after a `uv sync`.

Why we patch
============

`terminal-bench` 0.2.x uses `pathlib.Path("/tmp/...")` and
`Path(DockerComposeManager.CONTAINER_SESSION_LOGS_PATH)` to construct
container-side paths. On Windows, `Path` resolves to `WindowsPath`,
which renders forward slashes as backslashes when stringified
(`str(Path("/tmp")) == "\\tmp"`). Those backslash-paths get sent to
the Docker Linux container as the target path of `put_archive` /
`mkdir -p`, and the Linux container then either fails outright (404
from the Docker daemon) or creates a literal `\tmp` file. We replace
those handful of usages with `pathlib.PurePosixPath`, which always
renders with forward slashes regardless of host OS.

This patch is a no-op on POSIX hosts.
"""



import sys


def _apply_terminal_bench_windows_patches() -> None:
    if sys.platform != "win32":
        return

    try:
        from pathlib import PurePosixPath

        from terminal_bench.terminal import (  # type: ignore[import-not-found]
            docker_compose_manager as _dcm,
        )
        from terminal_bench.terminal import (
            tmux_session as _ts,
        )
    except Exception:
        return

    # IMPORTANT: this attribute lives on the *class*, not the module.
    # Setting it on the module (`_ts._GET_ASCIINEMA_...`) does nothing
    # because `self._GET_ASCIINEMA_...` inside `TmuxSession` resolves
    # via the class MRO, never the importing module's namespace.
    _ts.TmuxSession._GET_ASCIINEMA_TIMESTAMP_SCRIPT_CONTAINER_PATH = PurePosixPath(
        "/tmp/get-asciinema-timestamp.sh"
    )

    _dcm.DockerComposeManager.CONTAINER_TEST_DIR = PurePosixPath("/tests")

    def _logging_path(self):
        return (
            PurePosixPath(_dcm.DockerComposeManager.CONTAINER_SESSION_LOGS_PATH)
            / f"{self._session_name}.log"
        )

    def _recording_path(self):
        if self._disable_recording:
            return None
        return (
            PurePosixPath(_dcm.DockerComposeManager.CONTAINER_SESSION_LOGS_PATH)
            / f"{self._session_name}.cast"
        )

    _ts.TmuxSession.logging_path = property(_logging_path)
    _ts.TmuxSession._recording_path = property(_recording_path)


_apply_terminal_bench_windows_patches()
