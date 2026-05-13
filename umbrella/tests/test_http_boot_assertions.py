"""Tests for http_boot JSON-body assertions (P1-1a).

We unit-test the pure assertion checker first, then drive the full
``_run_http_boot_step`` against an in-process Python ``http.server``
to make sure the wiring (probe-while-alive + status code + body
schema) is correct.
"""

import json
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

from umbrella.verification.models import (
    VerificationStatus,
    VerificationStep,
    VerificationStepKind,
)
from umbrella.verification.runner import (
    _check_json_assertions,
    _run_http_boot_step,
)


def _make_step(**overrides) -> VerificationStep:
    base = dict(
        kind=VerificationStepKind.HTTP_BOOT,
        name="t",
        command=["x"],
        timeout_seconds=10,
        health_url="http://127.0.0.1:0/",
        startup_timeout_seconds=5,
        expect_status=200,
    )
    base.update(overrides)
    return VerificationStep(**base)


# ---------- pure assertion logic -----------------------------------------------


class TestCheckJsonAssertions:
    def test_top_level_keys_required(self) -> None:
        step = _make_step(expect_json_keys=["items", "ok"])
        ok, err = _check_json_assertions(step, {"items": [], "ok": True})
        assert ok and err == ""
        ok, err = _check_json_assertions(step, {"items": []})
        assert not ok
        assert "ok" in err

    def test_json_path_exact_match(self) -> None:
        step = _make_step(expect_json_path={"meta.status": "live"})
        ok, _ = _check_json_assertions(step, {"meta": {"status": "live"}})
        assert ok
        ok, err = _check_json_assertions(step, {"meta": {"status": "stale"}})
        assert not ok
        assert "stale" in err

    def test_json_path_regex(self) -> None:
        step = _make_step(expect_json_path={"version": r"regex:^\d+\.\d+"})
        assert _check_json_assertions(step, {"version": "6.2.0"})[0]
        ok, err = _check_json_assertions(step, {"version": "alpha"})
        assert not ok and "did not match" in err

    def test_json_path_wildcard_just_existence(self) -> None:
        step = _make_step(expect_json_path={"items.0.title": "*"})
        ok, _ = _check_json_assertions(step, {"items": [{"title": "x"}]})
        assert ok
        ok, err = _check_json_assertions(step, {"items": []})
        assert not ok

    def test_min_items_enforced(self) -> None:
        step = _make_step(expect_min_items={"items": 5})
        assert _check_json_assertions(step, {"items": [1, 2, 3, 4, 5]})[0]
        ok, err = _check_json_assertions(step, {"items": [1, 2]})
        assert not ok
        assert "len 2" in err

    def test_min_items_wrong_type(self) -> None:
        step = _make_step(expect_min_items={"items": 1})
        ok, err = _check_json_assertions(step, {"items": "not a list"})
        assert not ok and "not a list" in err

    def test_top_level_must_be_object(self) -> None:
        step = _make_step(expect_json_keys=["x"])
        ok, err = _check_json_assertions(step, [1, 2])
        assert not ok and "object" in err


# ---------- end-to-end against an in-process HTTP server ----------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_server(tmp: Path, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload)
    src = textwrap.dedent(
        f"""
        import sys, time
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

        BODY = {body!r}.encode("utf-8")
        STATUS = {status}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw):
                pass
            def do_GET(self):
                self.send_response(STATUS)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)

        ThreadingHTTPServer.allow_reuse_address = True
        port = int(sys.argv[1])
        last = None
        for _ in range(40):
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", port), H)
                break
            except OSError as e:
                last = e
                time.sleep(0.25)
        else:
            print("bind_failed:", last, file=sys.stderr, flush=True)
            sys.exit(2)
        print("listening", flush=True)
        srv.serve_forever()
        """
    )
    (tmp / "srv.py").write_text(src, encoding="utf-8")


def _write_env_port_server(tmp: Path, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload)
    src = textwrap.dedent(
        f"""
        import os
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

        BODY = {body!r}.encode("utf-8")
        STATUS = {status}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw):
                pass
            def do_GET(self):
                self.send_response(STATUS)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)

        ThreadingHTTPServer.allow_reuse_address = True
        port = int(os.environ.get("PORT", "8765"))
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        print("listening", flush=True)
        srv.serve_forever()
        """
    )
    (tmp / "env_srv.py").write_text(src, encoding="utf-8")


@pytest.fixture()
def server_port(tmp_path: Path) -> int:
    return _free_port()


import os


def _server_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return env


class TestRunHttpBootStep:
    def test_passes_with_matching_body(self, tmp_path: Path, server_port: int) -> None:
        _write_server(tmp_path, {"items": [{"title": "a"}, {"title": "b"}], "ok": True})
        step = _make_step(
            command=[sys.executable, "-u", "srv.py", str(server_port)],
            health_url=f"http://127.0.0.1:{server_port}/",
            startup_timeout_seconds=20,
            expect_json_keys=["items", "ok"],
            expect_min_items={"items": 2},
            expect_json_path={"items.0.title": "a"},
        )
        result = _run_http_boot_step(step, tmp_path, env=_server_env())
        assert result.status == VerificationStatus.PASSED, (
            result.error or result.summary,
            result.stdout[-400:],
            result.stderr[-400:],
        )

    def test_fails_when_assertion_violated(
        self, tmp_path: Path, server_port: int
    ) -> None:
        _write_server(tmp_path, {"items": [], "ok": True})
        step = _make_step(
            command=[sys.executable, "-u", "srv.py", str(server_port)],
            health_url=f"http://127.0.0.1:{server_port}/",
            startup_timeout_seconds=20,
            expect_min_items={"items": 1},
        )
        result = _run_http_boot_step(step, tmp_path, env=_server_env())
        assert result.status == VerificationStatus.FAILED, (
            result.error,
            result.stderr[-400:],
        )
        assert "items" in (result.error or ""), (result.error, result.stderr[-400:])

    def test_fails_on_wrong_status(self, tmp_path: Path, server_port: int) -> None:
        _write_server(tmp_path, {"err": "boom"}, status=500)
        step = _make_step(
            command=[sys.executable, "-u", "srv.py", str(server_port)],
            health_url=f"http://127.0.0.1:{server_port}/",
            startup_timeout_seconds=10,
            expect_status=200,
        )
        result = _run_http_boot_step(step, tmp_path, env=_server_env())
        # Server only ever answers 500; health probe never sees 2xx, so the
        # boot loop times out — that's a failure too.
        assert result.status == VerificationStatus.FAILED

    def test_auto_remaps_busy_port_for_http_boot(
        self, tmp_path: Path, server_port: int
    ) -> None:
        # Occupy the originally requested health URL port with a stale listener.
        stale_proc = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "http.server",
                str(server_port),
                "--bind",
                "127.0.0.1",
            ],
            cwd="/",
            env=_server_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Ensure the stale listener is actually up before running verifier.
            stale_url = f"http://127.0.0.1:{server_port}/"
            for _ in range(20):
                try:
                    with urllib.request.urlopen(stale_url, timeout=0.3):  # noqa: S310
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                pytest.fail("stale listener failed to start")

            # Service under test binds from PORT env, so verifier can remap it.
            _write_env_port_server(tmp_path, {"ok": True})
            step = _make_step(
                command=[sys.executable, "-u", "env_srv.py"],
                health_url=f"http://127.0.0.1:{server_port}/",
                startup_timeout_seconds=20,
                expect_json_keys=["ok"],
            )
            result = _run_http_boot_step(step, tmp_path, env=_server_env())
            assert result.status == VerificationStatus.PASSED, (
                result.error,
                result.summary,
                result.stderr[-400:],
            )
            assert "auto-remapped port" in result.summary
        finally:
            stale_proc.terminate()
            try:
                stale_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stale_proc.kill()
                stale_proc.wait()
