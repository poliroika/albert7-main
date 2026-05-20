import logging
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from umbrella.web_bridge.app import WebBridgeApp
from umbrella.web_bridge.util import (
    WEB_BUILD_DIR,
    WEB_DIST_DIR,
    iso_utc,
    json_bytes,
    now_ts,
)

log = logging.getLogger(__name__)


def send_cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization, X-Requested-With",
    )
    handler.send_header("Access-Control-Max-Age", "86400")


class WebBridgeHandler(BaseHTTPRequestHandler):
    app: WebBridgeApp = WebBridgeApp()

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep default hook quiet; we emit explicit access logs in do_* handlers.
        return

    def _log_access(self, method: str) -> None:
        status = getattr(self, "_last_status", None)
        if status is None:
            return
        log.info('%s - "%s %s" %s', self.address_string(), method, self.path, status)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json_bytes(payload)
        self._last_status = int(status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        send_cors(self)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message, "status": status}, status=status)

    def _send_bytes(
        self, data: bytes, content_type: str, status: int = HTTPStatus.OK
    ) -> None:
        self._last_status = int(status)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        send_cors(self)
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        send_cors(self)
        self.end_headers()

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            import json

            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        return {
            k: v[0]
            for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
            if v
        }

    def _path(self) -> str:
        return unquote(urlparse(self.path).path)

    def do_GET(self) -> None:  # noqa: N802
        self._last_status = None
        try:
            self._dispatch_get()
        except FileNotFoundError as exc:
            self._send_error(f"not found: {exc}", HTTPStatus.NOT_FOUND)
        except Exception:
            log.exception("GET failed for %s", self.path)
            self._send_error("internal error", HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            self._log_access("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._last_status = None
        try:
            self._dispatch_post(self._read_json_body())
        except FileNotFoundError as exc:
            self._send_error(f"not found: {exc}", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception:
            log.exception("POST failed for %s", self.path)
            self._send_error("internal error", HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            self._log_access("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._last_status = None
        try:
            self._dispatch_patch(self._read_json_body())
        except FileNotFoundError as exc:
            self._send_error(f"not found: {exc}", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception:
            log.exception("PATCH failed for %s", self.path)
            self._send_error("internal error", HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            self._log_access("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._last_status = None
        try:
            self._dispatch_delete()
        except FileNotFoundError as exc:
            self._send_error(f"not found: {exc}", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            # Active-run guards and other "blocked but not server's fault"
            # cases surface as ValueError; map to 409 so the UI can show a
            # specific reason instead of a generic 500.
            self._send_json(
                {"ok": False, "removed": False, "reason": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
        except Exception:
            log.exception("DELETE failed for %s", self.path)
            self._send_error("internal error", HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            self._log_access("DELETE")

    def _dispatch_get(self) -> None:
        path = self._path()
        q = self._query()
        app = self.app

        if path == "/api/health":
            self._send_json(
                {"ok": True, "service": "umbrella-web-bridge", "ts": iso_utc(now_ts())}
            )
            return
        if path == "/api/workspaces":
            self._send_json(app.list_workspaces())
            return
        m = re.fullmatch(r"/api/workspaces/([^/]+)", path)
        if m:
            ws = app.get_workspace(m.group(1))
            if ws is None:
                raise FileNotFoundError(path)
            self._send_json(ws)
            return
        if path == "/api/threads":
            self._send_json(app.list_threads(q.get("workspace_id")))
            return
        m = re.fullmatch(r"/api/threads/([^/]+)", path)
        if m:
            thread = app.get_thread(m.group(1))
            if thread is None:
                raise FileNotFoundError(path)
            self._send_json(thread)
            return
        m = re.fullmatch(r"/api/threads/([^/]+)/messages", path)
        if m:
            self._send_json(app.list_messages(m.group(1)))
            return
        if path == "/api/runs":
            try:
                limit = int(q.get("limit") or 50)
            except ValueError:
                limit = 50
            try:
                offset = int(q.get("offset") or 0)
            except ValueError:
                offset = 0
            self._send_json(
                app.list_runs(q.get("workspace_id"), limit=limit, offset=offset)
            )
            return
        m = re.fullmatch(r"/api/runs/([^/]+)/steps", path)
        if m:
            self._send_json(app.get_run_steps(m.group(1)))
            return
        m = re.fullmatch(r"/api/runs/([^/]+)/timeline", path)
        if m:
            self._send_json(app.get_run_timeline(m.group(1)))
            return
        m = re.fullmatch(r"/api/runs/([^/]+)", path)
        if m:
            run = app.get_run(m.group(1))
            if run is None:
                raise FileNotFoundError(path)
            self._send_json(run)
            return
        if path == "/api/logs":
            try:
                limit = int(q.get("limit") or 200)
            except ValueError:
                limit = 200
            self._send_json(
                app.list_logs(
                    q.get("workspace_id"),
                    severity=q.get("severity"),
                    query=q.get("q"),
                    limit=limit,
                )
            )
            return
        if path == "/api/memory":
            result = app.list_memory_nodes(q.get("workspace_id"), q.get("run_id"))
            if q.get("include_palace") == "1":
                try:
                    import os
                    import pathlib
                    from umbrella.memory.palace.facade import MemPalace
                    repo_root = pathlib.Path(os.environ.get("UMBRELLA_REPO_ROOT", "."))
                    palace = MemPalace(repo_root, q.get("workspace_id") or "")
                    palace_nodes = palace.search("", n=100)
                    if isinstance(result, dict) and "nodes" in result:
                        result["palace_nodes"] = palace_nodes
                        result["palace_count"] = len(palace_nodes)
                    elif isinstance(result, list):
                        result = {"nodes": result, "palace_nodes": palace_nodes}
                except Exception:
                    pass
            self._send_json(result)
            return
        m = re.fullmatch(r"/api/memory/([^/]+)", path)
        if m:
            node = app.get_memory_node(m.group(1))
            if node is None:
                raise FileNotFoundError(path)
            self._send_json(node)
            return
        if path == "/api/settings":
            ws = q.get("workspace_id")
            if not ws:
                self._send_error("workspace_id is required", HTTPStatus.BAD_REQUEST)
                return
            self._send_json(app.get_settings(ws))
            return
        if path == "/api/dashboard/stats":
            ws = q.get("workspace_id")
            if not ws:
                self._send_error("workspace_id is required", HTTPStatus.BAD_REQUEST)
                return
            self._send_json(app.dashboard_stats(ws))
            return
        if path == "/api/models":
            self._send_json(app.list_models())
            return
        if path == "/api/tools":
            self._send_json(app.list_tools())
            return
        if path == "/api/user-input":
            self._send_json(
                app.list_user_input_requests(q.get("run_id"), q.get("status"))
            )
            return
        if path == "/api/permission-request":
            self._send_json(
                app.list_permission_requests(q.get("run_id"), q.get("status"))
            )
            return
        if path == "/api/mcp/servers":
            self._send_json(app.list_mcp_servers())
            return

        m = re.fullmatch(r"/api/runs/([^/]+)/phases", path)
        if m:
            self._send_json(self._get_phase_plan(m.group(1)))
            return

        m = re.fullmatch(r"/api/runs/([^/]+)/report", path)
        if m:
            self._send_json(self._get_run_report(m.group(1), q.get("workspace_id", "")))
            return

        if path == "/api/palace":
            self._send_json(
                self._list_palace_nodes(q.get("workspace_id"), q.get("query", ""), q.get("store"))
            )
            return

        self._serve_static(path)

    def _dispatch_post(self, payload: dict[str, Any]) -> None:
        path = self._path()
        app = self.app
        if path == "/api/workspaces":
            self._send_json(app.create_workspace(payload), status=HTTPStatus.CREATED)
            return
        if path == "/api/threads":
            self._send_json(app.create_thread(payload), status=HTTPStatus.CREATED)
            return
        if path == "/api/runs":
            run = app.start_workspace_run(payload)
            status = HTTPStatus.OK if run.get("already_running") else HTTPStatus.CREATED
            self._send_json(run, status=status)
            return
        m = re.fullmatch(r"/api/threads/([^/]+)/messages", path)
        if m:
            self._send_json(app.send_message(m.group(1), payload))
            return
        m = re.fullmatch(r"/api/runs/([^/]+)/cancel", path)
        if m:
            cancel_kwargs: dict[str, Any] = {}
            wait_value = payload.get("wait")
            if wait_value is None:
                wait_value = payload.get("wait_seconds")
            if wait_value is not None:
                try:
                    cancel_kwargs["wait_seconds"] = float(wait_value)
                except (TypeError, ValueError):
                    pass
            force_value = payload.get("force_after")
            if force_value is None:
                force_value = payload.get("force_after_seconds")
            if force_value is not None:
                try:
                    cancel_kwargs["force_after_seconds"] = float(force_value)
                except (TypeError, ValueError):
                    pass
            self._send_json(app.cancel_run(m.group(1), **cancel_kwargs))
            return
        m = re.fullmatch(r"/api/user-input/([^/]+)/answer", path)
        if m:
            answer = payload.get("answer", "")
            self._send_json(app.answer_user_input_request(m.group(1), str(answer)))
            return
        m = re.fullmatch(r"/api/permission-request/([^/]+)/resolve", path)
        if m:
            granted = bool(payload.get("granted", False))
            self._send_json(app.resolve_permission_request(m.group(1), granted))
            return
        if path == "/api/mcp/servers":
            self._send_json(app.add_mcp_server(payload), status=HTTPStatus.CREATED)
            return
        if path == "/api/mcp/discover":
            self._send_json(app.discover_mcp_servers(payload))
            return
        raise FileNotFoundError(path)

    def _dispatch_patch(self, payload: dict[str, Any]) -> None:
        path = self._path()
        app = self.app
        m = re.fullmatch(r"/api/workspaces/([^/]+)", path)
        if m:
            updated = app.update_workspace(m.group(1), payload)
            if updated is None:
                raise FileNotFoundError(path)
            self._send_json(updated)
            return
        m = re.fullmatch(r"/api/memory/([^/]+)", path)
        if m:
            updated = app.update_memory_node(m.group(1), payload)
            if updated is None:
                raise FileNotFoundError(path)
            self._send_json(updated)
            return
        if path == "/api/settings":
            ws = self._query().get("workspace_id")
            if not ws:
                self._send_error("workspace_id is required", HTTPStatus.BAD_REQUEST)
                return
            self._send_json(app.update_settings(ws, payload))
            return
        m = re.fullmatch(r"/api/mcp/servers/([^/]+)", path)
        if m:
            updated = app.update_mcp_server(m.group(1), payload)
            if updated is None:
                raise FileNotFoundError(path)
            self._send_json(updated)
            return
        raise FileNotFoundError(path)

    def _dispatch_delete(self) -> None:
        path = self._path()
        app = self.app
        m = re.fullmatch(r"/api/workspaces/([^/]+)", path)
        if m:
            result = app.delete_workspace(m.group(1))
            status = HTTPStatus.OK if result.get("ok", True) else HTTPStatus.CONFLICT
            self._send_json(result, status=status)
            return
        m = re.fullmatch(r"/api/threads/([^/]+)", path)
        if m:
            result = app.delete_thread(m.group(1))
            status = HTTPStatus.OK if result.get("ok", True) else HTTPStatus.CONFLICT
            self._send_json(result, status=status)
            return
        m = re.fullmatch(r"/api/runs/([^/]+)", path)
        if m:
            q = self._query()
            result = app.delete_run(m.group(1), q.get("workspace_id"))
            status = HTTPStatus.OK if result.get("ok", True) else HTTPStatus.CONFLICT
            self._send_json(result, status=status)
            return
        m = re.fullmatch(r"/api/memory/([^/]+)", path)
        if m:
            q = self._query()
            result = app.delete_memory_node(m.group(1), q.get("workspace_id"))
            status = HTTPStatus.OK if result.get("ok", True) else HTTPStatus.CONFLICT
            self._send_json(result, status=status)
            return
        m = re.fullmatch(r"/api/mcp/servers/([^/]+)", path)
        if m:
            result = app.delete_mcp_server(m.group(1))
            status = HTTPStatus.OK if result.get("ok", True) else HTTPStatus.CONFLICT
            self._send_json(result, status=status)
            return
        raise FileNotFoundError(path)

    def _get_phase_plan(self, run_id: str) -> dict:
        import json
        import os
        import pathlib
        repo_root = pathlib.Path(os.environ.get("UMBRELLA_REPO_ROOT", "."))
        drive_candidates = list(repo_root.glob("workspaces/*/.memory/drive/state/phase_plan.json"))
        for candidate in drive_candidates:
            try:
                data = json.loads(candidate.read_text())
                if data.get("run_id") == run_id or not run_id:
                    return {"ok": True, "data": data}
            except Exception:
                pass
        return {"ok": False, "error": "no phase plan found", "run_id": run_id}

    def _get_run_report(self, run_id: str, workspace_id: str) -> dict:
        import os
        import pathlib
        from umbrella.web_bridge.api.report_api import get_run_report
        repo_root = pathlib.Path(os.environ.get("UMBRELLA_REPO_ROOT", "."))
        if workspace_id:
            drive_root = repo_root / "workspaces" / workspace_id / ".memory" / "drive"
        else:
            drive_root = repo_root / ".umbrella"
        return get_run_report(run_id, workspace_id, drive_root=drive_root)

    def _list_palace_nodes(self, workspace_id: str | None, query: str, store: str | None) -> dict:
        try:
            import os
            import pathlib
            from umbrella.memory.palace.facade import MemPalace
            repo_root = pathlib.Path(os.environ.get("UMBRELLA_REPO_ROOT", "."))
            palace = MemPalace(repo_root, workspace_id or "")
            nodes = palace.search(
                query or "memory",
                stores=[store] if store else None,
                n=50,
            )
            return {"ok": True, "data": {"nodes": nodes, "total": len(nodes), "workspace_id": workspace_id}}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _serve_static(self, request_path: str) -> None:
        asset_like = request_path.startswith("/static/") or bool(
            Path(request_path).suffix
        )
        for root in (WEB_BUILD_DIR, WEB_DIST_DIR):
            if not root.exists():
                continue
            relative = (
                "index.html" if request_path in ("/", "") else request_path.lstrip("/")
            )
            try:
                candidate = (root / relative).resolve()
                candidate.relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            if candidate.is_file():
                content_type, _ = mimetypes.guess_type(candidate.name)
                self._send_bytes(
                    candidate.read_bytes(), content_type or "application/octet-stream"
                )
                return
            if asset_like:
                continue
            index = (root / "index.html").resolve()
            if index.exists():
                content_type, _ = mimetypes.guess_type(index.name)
                self._send_bytes(index.read_bytes(), content_type or "text/html")
                return
        raise FileNotFoundError(request_path)


def build_handler(app: WebBridgeApp | None = None) -> type[BaseHTTPRequestHandler]:
    instance = app or WebBridgeApp()

    class BoundHandler(WebBridgeHandler):
        pass

    BoundHandler.app = instance
    return BoundHandler
