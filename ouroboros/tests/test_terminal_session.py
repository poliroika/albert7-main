"""Tests for :mod:`ouroboros.tools.terminal_session`.

The tmux/bash backends require a POSIX shell that we don't have on the
Windows host where most CI runs ad-hoc. The TB container path is what we
actually care about, so each test gates itself to the backend it needs and
skips when that backend isn't available.
"""

import sys

import pytest

from ouroboros.tools.terminal_session import (
    BashSubprocessBackend,
    OneShotBackend,
    TerminalSession,
    TmuxBackend,
    _strip_shell_startup_noise,
    get_or_create_session,
    shutdown_all_sessions,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HAS_TMUX = TmuxBackend.is_available()
_HAS_BASH = BashSubprocessBackend.is_available()


def _persistent_backends() -> list[str]:
    out: list[str] = []
    if _HAS_TMUX:
        out.append("tmux")
    if _HAS_BASH:
        out.append("bash")
    return out


@pytest.fixture
def session(request) -> TerminalSession:
    backend_name = request.param
    if backend_name == "tmux" and not _HAS_TMUX:
        pytest.skip("tmux not installed")
    if backend_name == "bash" and not _HAS_BASH:
        pytest.skip("bash subprocess backend unavailable on this host")
    sess = TerminalSession.for_workspace(
        f"test-{backend_name}-{request.node.name}",
        prefer_backend=backend_name,
    )
    yield sess
    sess.kill()


# ---------------------------------------------------------------------------
# Backend-parameterized tests (run for tmux + bash where available)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _persistent_backends(), reason="no persistent shell backend available"
)
@pytest.mark.parametrize("session", _persistent_backends() or ["tmux"], indirect=True)
def test_session_persists_cwd(session: TerminalSession) -> None:
    session.run("cd /tmp", timeout=10)
    res = session.run("pwd", timeout=10)
    assert res.exit_code == 0
    assert "/tmp" in res.output


@pytest.mark.skipif(
    not _persistent_backends(), reason="no persistent shell backend available"
)
@pytest.mark.parametrize("session", _persistent_backends() or ["tmux"], indirect=True)
def test_session_persists_env(session: TerminalSession) -> None:
    session.run("export FOO=bar_persist", timeout=10)
    res = session.run("echo $FOO", timeout=10)
    assert res.exit_code == 0
    assert "bar_persist" in res.output


@pytest.mark.skipif(
    not _persistent_backends(), reason="no persistent shell backend available"
)
@pytest.mark.parametrize("session", _persistent_backends() or ["tmux"], indirect=True)
def test_marker_isolates_output(session: TerminalSession) -> None:
    first = session.run("echo first_marker_token", timeout=10)
    second = session.run("echo second_marker_token", timeout=10)
    assert "second_marker_token" in second.output
    # The second result must NOT include output of the first command.
    assert "first_marker_token" not in second.output
    assert first.marker != second.marker


@pytest.mark.skipif(
    not _persistent_backends(), reason="no persistent shell backend available"
)
@pytest.mark.parametrize("session", _persistent_backends() or ["tmux"], indirect=True)
def test_timeout_kills_command(session: TerminalSession) -> None:
    res = session.run("sleep 30", timeout=2)
    assert res.timed_out is True
    # exit code is either 124 (recovered) or 130 (SIGINT'd) depending on backend.
    assert res.exit_code in (124, 130, -1) or res.exit_code != 0
    # Session must still be usable afterwards.
    follow_up = session.run("echo still_alive", timeout=10)
    assert follow_up.exit_code == 0
    assert "still_alive" in follow_up.output


@pytest.mark.skipif(
    not _persistent_backends(), reason="no persistent shell backend available"
)
@pytest.mark.parametrize("session", _persistent_backends() or ["tmux"], indirect=True)
def test_view_returns_tail(session: TerminalSession) -> None:
    for i in range(6):
        session.run(f"echo line_{i}", timeout=10)
    tail = session.view(last_lines=20)
    assert "line_5" in tail


@pytest.mark.skipif(
    not _persistent_backends(), reason="no persistent shell backend available"
)
@pytest.mark.parametrize("session", _persistent_backends() or ["tmux"], indirect=True)
def test_reset_drops_state(session: TerminalSession) -> None:
    session.run("export RESET_PROBE=present", timeout=10)
    res_before = session.run("echo $RESET_PROBE", timeout=10)
    assert "present" in res_before.output
    session.reset()
    res_after = session.run('echo "[$RESET_PROBE]"', timeout=10)
    assert "[]" in res_after.output


# ---------------------------------------------------------------------------
# Backend-agnostic tests (run on every host, including Windows via OneShot)
# ---------------------------------------------------------------------------


def test_oneshot_backend_runs_basic_command() -> None:
    sess = TerminalSession(workspace_id="ws-oneshot", backend=OneShotBackend())
    res = sess.run([sys.executable, "-c", "print('hello-oneshot')"], timeout=20)
    assert res.exit_code == 0
    assert "hello-oneshot" in res.output


def test_oneshot_view_records_command() -> None:
    sess = TerminalSession(workspace_id="ws-oneshot-view", backend=OneShotBackend())
    sess.run([sys.executable, "-c", "print('viewable')"], timeout=20)
    tail = sess.view(last_lines=20)
    assert "viewable" in tail


def test_get_or_create_session_caches_per_workspace() -> None:
    class Ctx:
        pass

    ctx = Ctx()
    s1 = get_or_create_session(ctx, "alpha")
    s2 = get_or_create_session(ctx, "alpha")
    s3 = get_or_create_session(ctx, "beta")
    assert s1 is s2
    assert s1 is not s3
    shutdown_all_sessions(ctx)
    # After shutdown the cache must be empty so the next call re-creates.
    s4 = get_or_create_session(ctx, "alpha")
    assert s4 is not s1


def test_shutdown_all_sessions_is_idempotent() -> None:
    class Ctx:
        pass

    ctx = Ctx()
    get_or_create_session(ctx, "ws-x")
    shutdown_all_sessions(ctx)
    shutdown_all_sessions(ctx)


def test_oneshot_handles_command_not_found() -> None:
    sess = TerminalSession(workspace_id="ws-oneshot-missing", backend=OneShotBackend())
    res = sess.run(["this_binary_does_not_exist_xyz_12345"], timeout=10)
    assert res.exit_code != 0


def test_strip_shell_startup_noise_removes_ansi_and_macos_banner() -> None:
    raw = (
        "\x1b[?1034hThe default interactive shell is now zsh.\n"
        "To update your account to use zsh, please run `chsh -s /bin/zsh`.\n"
        "For more details, please visit https://support.apple.com/kb/HT208050.\n"
        "real output line\n"
    )
    cleaned = _strip_shell_startup_noise(raw)
    assert "interactive shell is now zsh" not in cleaned
    assert "chsh -s /bin/zsh" not in cleaned
    assert "\x1b" not in cleaned
    assert "real output line" in cleaned


def test_reset_is_reentrant_under_backend_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: timeout recovery in run() calls reset() while lock is held.

    With threading.Lock this deadlocks (same thread re-acquires lock in reset()).
    """
    tmux_backend = TmuxBackend("test-reentrant-reset")
    monkeypatch.setattr(tmux_backend, "kill", lambda: None)
    monkeypatch.setattr(tmux_backend, "start", lambda: None)
    assert tmux_backend._lock.acquire(timeout=0.1)
    try:
        tmux_backend.reset()
    finally:
        tmux_backend._lock.release()

    bash_backend = BashSubprocessBackend()
    monkeypatch.setattr(bash_backend, "kill", lambda: None)
    monkeypatch.setattr(bash_backend, "_spawn", lambda: None)
    assert bash_backend._lock.acquire(timeout=0.1)
    try:
        bash_backend.reset()
    finally:
        bash_backend._lock.release()
