"""Smoke tests for the Polymarket live betting simulator seed.

These tests act as acceptance criteria for the workspace. On the pristine
seed they fail on purpose (the web server does not exist yet); this is the
signal that Umbrella's runtime verification gate uses to tell Ouroboros the
task is not done.

When Ouroboros finishes building the simulator, every assertion here must
pass without modification — do NOT weaken these assertions to make the test
green. If the assertion truly needs to change, record the reason in memory
and update `workspace.toml` deliberately.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

WORKSPACE_DIR = Path(__file__).resolve().parent
WEB_SERVER = WORKSPACE_DIR / "web_server.py"
EXPECTED_PORT = 5050
HEALTH_URL = f"http://127.0.0.1:{EXPECTED_PORT}/api/health"
PRODUCTION_MODULE_HINTS = ("web_server.py", "sim_engine.py", "polymarket_client.py")


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def test_web_server_entrypoint_exists():
    assert WEB_SERVER.exists(), (
        "web_server.py must exist at the workspace root and start the simulator. "
        f"Expected at {WEB_SERVER}."
    )


def test_web_server_is_importable():
    if not WEB_SERVER.exists():
        pytest.skip(
            "web_server.py does not exist yet; see test_web_server_entrypoint_exists"
        )
    spec = importlib.util.spec_from_file_location("polymarket_web_server", WEB_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except SystemExit:
        pass  # some frameworks call sys.exit on import with __main__


def test_production_code_does_not_use_random():
    violations: list[str] = []
    for name in PRODUCTION_MODULE_HINTS:
        path = WORKSPACE_DIR / name
        if not path.exists():
            continue
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "import random" in stripped or "from random " in stripped:
                violations.append(f"{name}:{line_no}: {stripped}")
            if "random.random(" in stripped or "random.choice(" in stripped:
                violations.append(f"{name}:{line_no}: {stripped}")
    assert not violations, (
        "TASK_MAIN.md forbids random-driven outcomes; the following lines "
        "use the `random` module in production code:\n  " + "\n  ".join(violations)
    )


def test_llm_env_configuration_is_used():
    # web_server.py must consume LLM env vars (directly or via a helper module).
    # We search all .py files in the workspace for at least one mention.
    wanted = ("LLM_API_KEY", "LLM_MODEL")
    hits: dict[str, list[str]] = {key: [] for key in wanted}
    for path in WORKSPACE_DIR.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        text = _read_text(path)
        for key in wanted:
            if key in text:
                hits[key].append(path.name)

    missing = [key for key, files in hits.items() if not files]
    assert not missing, (
        f"Simulator must read LLM configuration from environment, but the "
        f"following keys are not referenced anywhere in workspace code: {missing}"
    )


def _wait_for_http(url: str, timeout: float) -> tuple[int | None, str]:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
                return resp.status, resp.read(4096).decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            last_error = f"URLError: {exc.reason}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    return None, last_error


def test_web_server_boots_and_health_responds():
    if not WEB_SERVER.exists():
        pytest.skip(
            "web_server.py does not exist yet; see test_web_server_entrypoint_exists"
        )

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    creationflags = 0
    preexec_fn = None
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        preexec_fn = os.setsid  # type: ignore[assignment]

    proc = subprocess.Popen(
        [sys.executable, str(WEB_SERVER)],
        cwd=str(WORKSPACE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        preexec_fn=preexec_fn,
    )
    try:
        status, body = _wait_for_http(HEALTH_URL, timeout=20)
        if status is None:
            proc.terminate()
            out, err = proc.communicate(timeout=5)
            pytest.fail(
                f"GET {HEALTH_URL} never returned 200. "
                f"last_error={body!r}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
            )
        assert 200 <= status < 300, f"Unexpected status {status}"
        payload: dict | None = None
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            assert any(key in payload for key in ("status", "ok", "service")), (
                f"/api/health should return a JSON object with a status-ish key, got {payload!r}"
            )
    finally:
        if proc.poll() is None:
            try:
                if sys.platform == "win32":
                    import signal as _signal

                    proc.send_signal(_signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    proc.terminate()
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
