"""Bridge HTTP server: serves React build + `/api/*` for the web UI."""

import argparse
import errno
import logging
import sys
from pathlib import Path

from umbrella.web_bridge.app import WebBridgeApp
from umbrella.web_bridge.handler import build_handler
from umbrella.web_bridge.util import WEB_BUILD_DIR

log = logging.getLogger(__name__)


class ReusableThreadingHTTPServer:
    """ThreadingHTTPServer with allow_reuse_address (faster restart after Ctrl+C)."""

    @staticmethod
    def create(handler_cls: type, host: str, port: int):
        from http.server import ThreadingHTTPServer

        class _Srv(ThreadingHTTPServer):
            allow_reuse_address = True

        return _Srv((host, port), handler_cls)


def serve(
    host: str = "127.0.0.1", port: int = 8765, app: WebBridgeApp | None = None
) -> None:
    handler_cls = build_handler(app)
    try:
        httpd = ReusableThreadingHTTPServer.create(handler_cls, host, port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            log.error(
                "Port %s is already in use. Stop another process or run on a different port: "
                "`uv run bridge --port 8766` (or `uv run python -m umbrella.web_bridge --port 8766`)\n"
                "macOS/Linux: `lsof -nP -iTCP:%s -sTCP:LISTEN`",
                port,
                port,
            )
        raise
    log.info(
        "Web bridge: http://%s:%s - one process serves static UI (web/build or web/dist) "
        "and JSON API under `/api/*`.",
        host,
        port,
    )
    log.info("ACCESS log lines include `/`, `/static/...`, and `/api/...` routes.")
    log.info("Build directory: %s - exists: %s", WEB_BUILD_DIR, WEB_BUILD_DIR.exists())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopping")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Umbrella web bridge (API + static UI)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    try:
        serve(args.host, args.port, app=WebBridgeApp(args.repo_root))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
