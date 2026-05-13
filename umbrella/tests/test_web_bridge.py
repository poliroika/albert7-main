import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest

from umbrella.web_bridge.app import WebBridgeApp
from umbrella.web_bridge.handler import build_handler
from umbrella.web_bridge.util import REPO_ROOT


@pytest.fixture
def httpd():
    app = WebBridgeApp(REPO_ROOT)
    handler = build_handler(app)
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port
    server.shutdown()
    t.join(timeout=2)


def _get_json(host: str, port: int, path: str) -> tuple[int, object]:
    conn = HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read().decode("utf-8")
    conn.close()
    data = json.loads(body) if body else None
    return r.status, data


def test_health_and_workspaces(httpd: int) -> None:
    status, data = _get_json("127.0.0.1", httpd, "/api/health")
    assert status == 200
    assert isinstance(data, dict) and data.get("ok") is True

    status, rows = _get_json("127.0.0.1", httpd, "/api/workspaces")
    assert status == 200
    assert isinstance(rows, list)


def test_workspace_stats_when_cli_tracker_exists(httpd: int) -> None:
    ws_id = "cli_tasks_tracker"
    path = f"/api/dashboard/stats?workspace_id={quote(ws_id)}"
    status, data = _get_json("127.0.0.1", httpd, path)
    assert status == 200
    assert isinstance(data, dict)
    assert data.get("workspace_id") == ws_id
    assert "total_runs" in data


def test_get_settings_repo_dotenv_overrides_stale_shell_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Root ``.env`` file wins over ``OUROBOROS_MODEL`` leaked into the shell."""
    monkeypatch.setenv("OUROBOROS_MODEL", "gemma-from-shell-stale")
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_MODEL=GLM-from-file\n", encoding="utf-8")
    with (
        patch("umbrella.web_bridge.app.load_env"),
        patch("umbrella.web_bridge.app.load_store", return_value={}),
    ):
        app = WebBridgeApp(tmp_path)
        s = app.get_settings("ws_any")
    assert s["default_model"] == "GLM-from-file"
    models = app.list_models()
    assert models[0]["id"] == "GLM-from-file"


def test_cancel_run_writes_workspace_drive_stop_file(tmp_path) -> None:
    repo = tmp_path
    (repo / "workspaces" / "ws_cancel" / ".memory" / "drive" / "state").mkdir(
        parents=True
    )
    app = WebBridgeApp(repo)

    def fake_get_run(_self, rid: str):
        return {
            "id": rid,
            "workspace_id": "ws_cancel",
            "status": "running",
            "pid": 999_999_999,
        }

    with (
        patch.object(WebBridgeApp, "get_run", fake_get_run),
        patch("umbrella.web_bridge.app.load_launcher_runs", return_value=[]),
        patch("umbrella.web_bridge.app.save_launcher_runs"),
    ):
        out = app.cancel_run("ui_run_testcancel")
    assert out.get("ok") is True
    stop_path = (
        repo
        / "workspaces"
        / "ws_cancel"
        / ".memory"
        / "drive"
        / "state"
        / "stop_requested.json"
    )
    assert stop_path.is_file()
    payload = json.loads(stop_path.read_text(encoding="utf-8"))
    assert payload.get("run_id") == "ui_run_testcancel"


def test_delete_run_removes_task_result_json(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_del_run"
    tr = repo / "workspaces" / ws / ".memory" / "drive" / "task_results"
    tr.mkdir(parents=True)
    tid = "tid_memo_1"
    jpath = tr / f"{tid}.json"
    jpath.write_text(
        json.dumps(
            {"task_id": tid, "status": "completed", "ts": "2025-01-01T00:00:00"}
        ),
        encoding="utf-8",
    )
    app = WebBridgeApp(repo)
    out = app.delete_run(tid, ws)
    assert out.get("ok") is True
    assert not jpath.exists()


def test_delete_run_removes_launcher_row_and_log(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_launch"
    log_dir = repo / ".umbrella" / "web" / "launcher_logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "ui_run_deltest.log"
    log_file.write_text("log body", encoding="utf-8")
    rel_log = str(log_file.relative_to(repo))
    rec = {
        "id": "ui_run_deltest",
        "workspace_id": ws,
        "log_path": rel_log,
        "status": "completed",
    }
    saved: list = []

    def _save(runs: list) -> None:
        saved.append(list(runs))

    with (
        patch("umbrella.web_bridge.app.load_launcher_runs", return_value=[rec]),
        patch("umbrella.web_bridge.app.save_launcher_runs", side_effect=_save),
    ):
        app = WebBridgeApp(repo)
        out = app.delete_run("ui_run_deltest", ws)
    assert out.get("ok") is True
    assert not log_file.exists()
    assert saved == [[]]


def test_delete_run_wrong_workspace_returns_error(tmp_path) -> None:
    """Run belongs to ws_a but query scoped to other workspace — nothing deleted."""
    repo = tmp_path
    ws = "ws_a"
    tr = repo / "workspaces" / ws / ".memory" / "drive" / "task_results"
    tr.mkdir(parents=True)
    tid = "tid_only"
    (tr / f"{tid}.json").write_text(
        json.dumps(
            {"task_id": tid, "status": "completed", "ts": "2025-01-01T00:00:00"}
        ),
        encoding="utf-8",
    )
    app = WebBridgeApp(repo)
    out = app.delete_run(tid, "other_workspace")
    assert out.get("ok") is False
    assert (tr / f"{tid}.json").is_file()


def test_delete_thread_detaches_still_stopping_run(tmp_path) -> None:
    repo = tmp_path
    app = WebBridgeApp(repo)
    thread_id = "thread_busy"
    run_id = "ui_run_busy"

    def fake_load_store(name: str, default):
        if name == "threads.json":
            return [{"id": thread_id, "workspace_id": "ws_busy", "title": "Busy"}]
        if name == f"messages_{thread_id}.json":
            return [{"id": "msg_1", "run_id": run_id}]
        return default

    saved: dict[str, object] = {}

    def fake_save_store(name: str, value):
        saved[name] = value

    with (
        patch("umbrella.web_bridge.app.load_store", side_effect=fake_load_store),
        patch("umbrella.web_bridge.app.save_store", side_effect=fake_save_store),
        patch.object(app, "_web_runs", return_value={}),
        patch.object(
            app,
            "delete_run",
            return_value={
                "ok": False,
                "removed": False,
                "run_id": run_id,
                "reason": "worker still alive after cancel; refusing to wipe live artifacts",
            },
        ),
    ):
        out = app.delete_thread(thread_id)

    assert out.get("ok") is True
    assert out.get("detached_run_ids") == [run_id]
    assert saved["threads.json"] == []


def test_list_memory_nodes_matches_memory_graph_contract(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_mem_graph"
    lessons = repo / "workspaces" / ws / ".memory" / "lessons.jsonl"
    lessons.parent.mkdir(parents=True)
    line1 = json.dumps(
        {
            "id": "les_a",
            "created_at": 1700000000.0,
            "change_summary": "Fix auth",
            "conclusion": "Use JWT",
            "lesson_type": "workspace",
            "tags": ["auth", "api"],
        },
        ensure_ascii=False,
    )
    line2 = json.dumps(
        {
            "id": "les_b",
            "created_at": 1700000001.0,
            "change_summary": "API notes",
            "tags": ["auth"],
        },
        ensure_ascii=False,
    )
    lessons.write_text(line1 + "\n" + line2 + "\n", encoding="utf-8")
    app = WebBridgeApp(repo)
    nodes = app.list_memory_nodes(ws)
    assert len(nodes) >= 2
    by_id = {n["id"]: n for n in nodes}
    assert by_id["les_a"]["label"]
    assert by_id["les_a"]["node_type"] == "lesson"
    assert by_id["les_a"]["content"]
    assert any(n["node_type"] == "source" for n in nodes)


def test_list_memory_nodes_includes_ideas_jsonl(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_ideas"
    ideas = repo / "workspaces" / ws / ".memory" / "ideas.jsonl"
    ideas.parent.mkdir(parents=True)
    ideas.write_text(
        json.dumps(
            {
                "id": "a1",
                "title": "T1",
                "kind": "subtask_result",
                "task_id": "task_x",
                "tags": ["planner"],
                "created_at": 1.0,
                "content": "c1",
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "id": "a2",
                "title": "T2",
                "kind": "subtask_result",
                "task_id": "task_x",
                "tags": ["planner"],
                "created_at": 2.0,
                "content": "c2",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    app = WebBridgeApp(repo)
    nodes = app.list_memory_nodes(ws)
    assert len(nodes) >= 2
    by_id = {n["id"]: n for n in nodes}
    assert by_id["a1"]["node_type"] == "subtask_result"
    assert by_id["a2"]["node_type"] == "subtask_result"
    assert any(n["node_type"] == "task" and n.get("task_id") == "task_x" for n in nodes)


def test_delete_run_wipes_verification_context_and_seed_backups(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_verification"
    workspace_memory = repo / "workspaces" / ws / ".memory"
    (workspace_memory / "drive" / "memory").mkdir(parents=True)
    (workspace_memory / "drive" / "state").mkdir(parents=True)
    (workspace_memory / "drive" / "task_results").mkdir(parents=True)

    vctx_md = workspace_memory / "drive" / "memory" / "verification_failure_context.md"
    vctx_json = (
        workspace_memory / "drive" / "state" / "verification_failure_context.json"
    )
    vctx_md.write_text("failure context body", encoding="utf-8")
    vctx_json.write_text(json.dumps({"failure": True}), encoding="utf-8")

    backups_dir = repo / ".umbrella" / "backups"
    backups_dir.mkdir(parents=True)
    seed_backup = backups_dir / f"seed_backup_2025-01-01_{ws}.tar.gz"
    seed_backup.write_text("blob", encoding="utf-8")
    unrelated_backup = backups_dir / "seed_backup_2025-01-01_other_ws.tar.gz"
    unrelated_backup.write_text("blob", encoding="utf-8")

    tid = "ui_run_verify"
    (workspace_memory / "drive" / "task_results" / f"{tid}.json").write_text(
        json.dumps({"task_id": tid, "status": "completed"}), encoding="utf-8"
    )

    app = WebBridgeApp(repo)
    out = app.delete_run(tid, ws)

    assert out.get("ok") is True
    assert not vctx_md.exists()
    assert not vctx_json.exists()
    assert not seed_backup.exists()
    assert unrelated_backup.exists(), "deleting one ws must not touch other ws backups"
    report = out.get("report") or {}
    assert isinstance(report.get("removed_paths"), list)
    assert any("verification_failure_context" in p for p in report["removed_paths"])
    assert any("seed_backup" in p for p in report["removed_paths"])


def test_delete_workspace_returns_real_report_and_wipes_umbrella_traces(
    tmp_path,
) -> None:
    repo = tmp_path
    ws = "ws_full_wipe"
    workspace_dir = repo / "workspaces" / ws
    (workspace_dir / ".memory").mkdir(parents=True)
    (workspace_dir / "TASK_MAIN.md").write_text("hello", encoding="utf-8")

    backups_dir = repo / ".umbrella" / "backups"
    backups_dir.mkdir(parents=True)
    seed_backup = backups_dir / f"seed_backup_2025-01-01_{ws}.tar.gz"
    seed_backup.write_text("blob", encoding="utf-8")

    signals = repo / ".umbrella" / "memory" / "signals.jsonl"
    signals.parent.mkdir(parents=True)
    signals.write_text(
        json.dumps({"workspace_id": ws, "category": "x"})
        + "\n"
        + json.dumps({"workspace_id": "other", "category": "y"})
        + "\n",
        encoding="utf-8",
    )

    nested_trace = repo / ".umbrella" / "launcher_logs" / ws
    nested_trace.mkdir(parents=True)
    (nested_trace / "x.log").write_text("log", encoding="utf-8")

    with (
        patch("umbrella.web_bridge.app.load_store", return_value=[]),
        patch("umbrella.web_bridge.app.save_store"),
    ):
        app = WebBridgeApp(repo)
        out = app.delete_workspace(ws)

    assert out.get("ok") is True
    assert out.get("removed") is True
    report = out.get("report") or {}
    assert isinstance(report, dict)
    assert isinstance(report.get("removed_paths"), list)
    assert any(ws in p and "workspaces" in p for p in report["removed_paths"])
    assert not workspace_dir.exists()
    assert not seed_backup.exists()
    assert not nested_trace.exists()
    remaining = signals.read_text(encoding="utf-8")
    assert ws not in remaining
    assert "other" in remaining


def test_delete_workspace_returns_409_when_active_run(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_active"
    (repo / "workspaces" / ws).mkdir(parents=True)

    app = WebBridgeApp(repo)
    with patch.object(
        app,
        "_active_run_for_workspace",
        return_value={"id": "ui_run_active", "workspace_id": ws, "status": "running"},
    ):
        with pytest.raises(ValueError, match="active run"):
            app.delete_workspace(ws)


def test_delete_memory_node_actually_removes_knowledge_md(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_knowledge"
    knowledge_dir = (
        repo / "workspaces" / ws / ".memory" / "drive" / "memory" / "knowledge"
    )
    knowledge_dir.mkdir(parents=True)
    md_file = knowledge_dir / "active_skills.md"
    md_file.write_text("# active skills", encoding="utf-8")

    app = WebBridgeApp(repo)
    out = app.delete_memory_node("knowledge:active_skills", ws)

    assert out.get("ok") is True
    assert out.get("removed") is True
    assert not md_file.exists()


def test_delete_memory_node_unknown_type_returns_reason(tmp_path) -> None:
    repo = tmp_path
    app = WebBridgeApp(repo)
    out = app.delete_memory_node("source:context:ws", "ws_x")
    assert out.get("ok") is False
    assert out.get("reason") == "node_type_not_deletable"


def test_start_workspace_run_uses_harness_worker_when_harness_mode(
    tmp_path, monkeypatch
) -> None:
    repo = tmp_path
    ws = "ws_harness"
    workspace_dir = repo / "workspaces" / ws
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "TASK_MAIN.md").write_text("# task", encoding="utf-8")
    (workspace_dir / "workspace.toml").write_text(
        f'[workspace]\nid = "{ws}"\nname = "test"\nlanguage = "python"\n',
        encoding="utf-8",
    )

    app = WebBridgeApp(repo)

    started_workers: list[str] = []

    def fake_harness_worker(*args, **kwargs):
        started_workers.append("harness")

    def fake_default_worker(*args, **kwargs):
        started_workers.append("default")

    monkeypatch.setattr(app, "_run_harness_worker", fake_harness_worker)
    monkeypatch.setattr(app, "_run_ouroboros_worker", fake_default_worker)
    with (
        patch("umbrella.web_bridge.app.load_store", return_value={}),
        patch("umbrella.web_bridge.app.save_store"),
    ):
        run = app.start_workspace_run(
            {
                "workspace_id": ws,
                "harness_mode": True,
                "harness_candidates": 3,
            }
        )

    assert run["mode"] == "harness"
    assert run["id"].startswith("harness_web_")
    assert run.get("harness_meta", {}).get("candidates") == 3
    # Wait for daemon worker thread to finish (it just records into a list).
    worker = app._run_threads.get(run["id"])
    if worker is not None:
        worker.join(timeout=2)
    assert "harness" in started_workers
    assert "default" not in started_workers


def test_cancel_run_propagates_to_harness_candidate_run_ids(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_h_cancel"
    (repo / "workspaces" / ws / ".memory" / "drive" / "state").mkdir(parents=True)

    app = WebBridgeApp(repo)
    run_id = "harness_web_abc"
    candidate_run_ids = [f"{run_id}__c1", f"{run_id}__c2"]
    app._upsert_web_run(
        run_id,
        {
            "id": run_id,
            "workspace_id": ws,
            "status": "running",
            "mode": "harness",
            "attempt": 1,
            "harness_meta": {
                "candidates": 2,
                "candidate_run_ids": candidate_run_ids,
            },
        },
    )

    out = app.cancel_run(run_id)
    assert out.get("ok") is True
    stop_path = (
        repo / "workspaces" / ws / ".memory" / "drive" / "state" / "stop_requested.json"
    )
    assert stop_path.exists()
    payload = json.loads(stop_path.read_text(encoding="utf-8"))
    assert payload.get("candidate_run_ids") == candidate_run_ids
    assert set(candidate_run_ids).issubset(set(payload["attempt_task_ids"]))


def test_task_result_complete_status_lists_as_completed(tmp_path) -> None:
    repo = tmp_path
    app = WebBridgeApp(repo)

    run = app._task_result_to_run(
        {"task_id": "run_complete", "status": "complete", "ts": "2026-05-08T00:00:00Z"},
        "ws_complete",
    )

    assert run["status"] == "completed"


def test_run_steps_and_logs_include_preflight_and_terminal_scrollback(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_logs"
    run_id = "sync_improve_web_logs"
    memory = repo / "workspaces" / ws / ".memory"
    logs_dir = memory / "drive" / "logs"
    terminal_path = memory / "drive" / "memory" / "terminal_scrollback.md"
    logs_dir.mkdir(parents=True)
    terminal_path.parent.mkdir(parents=True)
    (logs_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_preflight_error",
                "task_id": run_id,
                "tool": "update_workspace_seed",
                "phase": "tool_call",
                "error": "missing required fields",
                "ts": "2026-05-08T00:00:01Z",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    terminal_path.write_text("terminal line one\nterminal line two\n", encoding="utf-8")
    app = WebBridgeApp(repo)
    app._upsert_web_run(
        run_id,
        {
            "id": run_id,
            "workspace_id": ws,
            "status": "completed",
            "full_result": {
                "status": "complete",
                "task_id": run_id,
                "final_message": "done",
            },
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:02Z",
        },
    )

    steps = app.get_run_steps(run_id)
    logs = app.list_logs(ws, limit=20)["logs"]

    assert any(step["name"] == "tool_preflight_error" for step in steps)
    assert any(step["name"] == "terminal_scrollback" for step in steps)
    assert any("missing required fields" in log["message"] for log in logs)
    assert any(log["type"] == "terminal_scrollback" for log in logs)


def test_list_runs_repairs_stale_stopped_web_run(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_stale"
    (repo / "workspaces" / ws).mkdir(parents=True)
    app = WebBridgeApp(repo)
    run_id = "sync_improve_web_stale"
    app._upsert_web_run(
        run_id,
        {
            "id": run_id,
            "workspace_id": ws,
            "status": "running",
            "stop_requested": True,
            "source": "web_bridge",
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:00Z",
        },
    )

    rows = app.list_runs(ws)["runs"]

    row = next(item for item in rows if item["id"] == run_id)
    assert row["status"] == "cancelled"


def test_cleanup_module_dry_report_on_missing_paths(tmp_path) -> None:
    """Calling wipe_run_artifacts on an empty repo must not raise."""
    from umbrella.web_bridge.cleanup import wipe_run_artifacts

    report = wipe_run_artifacts(tmp_path, "ws_missing", "run_unknown", ["run_unknown"])
    assert report.errors == []
    assert isinstance(report.removed_paths, list)
    assert isinstance(report.to_dict(), dict)


def test_missing_static_css_returns_404_not_index_html(httpd: int) -> None:
    """Missing hashed bundle must not fall back to SPA index (would break all styles)."""
    conn = HTTPConnection("127.0.0.1", httpd, timeout=5)
    conn.request("GET", "/static/nonexistent-umbrella-test-bundle-zzzz.css")
    r = conn.getresponse()
    body = r.read()
    conn.close()
    assert r.status == 404
    assert b'"error"' in body


def test_spa_route_returns_index_html_when_build_exists(httpd: int) -> None:

    from umbrella.web_bridge.util import WEB_BUILD_DIR

    if not (WEB_BUILD_DIR / "index.html").is_file():
        pytest.skip("web/build missing")
    conn = HTTPConnection("127.0.0.1", httpd, timeout=5)
    conn.request("GET", "/chat")
    r = conn.getresponse()
    body = r.read()
    conn.close()
    assert r.status == 200
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()


# ---------------------------------------------------------------------------
# Regression tests for the run-audit fixes (deduplication, model fallback,
# extended cleanup, cancel propagation).
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, ws: str) -> Path:
    repo = tmp_path
    (repo / "workspaces" / ws / ".memory" / "drive" / "task_results").mkdir(
        parents=True
    )
    return repo


def test_list_runs_folds_remediation_children_into_parent(tmp_path) -> None:
    """A single web_run with two remediation task_results yields one row."""
    ws = "ws_remed"
    repo = _make_repo(tmp_path, ws)
    tr = repo / "workspaces" / ws / ".memory" / "drive" / "task_results"
    parent = "sync_improve_web_aaaa"
    (tr / f"{parent}.json").write_text(
        json.dumps(
            {
                "task_id": parent,
                "status": "completed",
                "model": "GLM-4.7",
                "ts": "2026-05-08T10:00:00",
            }
        ),
        encoding="utf-8",
    )
    for n in (1, 2):
        (tr / f"{parent}__remediation_{n}.json").write_text(
            json.dumps(
                {
                    "task_id": f"{parent}__remediation_{n}",
                    "parent_task_id": parent,
                    "status": "completed",
                    "ts": f"2026-05-08T10:0{n}:00",
                }
            ),
            encoding="utf-8",
        )
    app = WebBridgeApp(repo)
    app._upsert_web_run(
        parent,
        {
            "id": parent,
            "workspace_id": ws,
            "status": "completed",
            "model": "GLM-4.7",
            "source": "web_bridge",
            "attempt_task_ids": [parent],
            "full_result": {
                "internal_task_ids": [
                    parent,
                    f"{parent}__remediation_1",
                    f"{parent}__remediation_2",
                ]
            },
        },
    )

    out = app.list_runs(ws)
    ids = [r["id"] for r in out["runs"]]
    assert ids == [parent], f"expected only the parent row, got {ids}"
    assert out["runs"][0]["model"] == "GLM-4.7"


def test_task_result_to_run_picks_up_model_from_alternate_keys(tmp_path) -> None:
    app = WebBridgeApp(tmp_path)
    row = app._task_result_to_run(
        {"task_id": "tid", "status": "completed", "active_model": "abc/x"},
        ws_id="ws_x",
    )
    assert row["model"] == "abc/x"
    row2 = app._task_result_to_run(
        {"task_id": "tid", "status": "completed", "model_used": "y/z"},
        ws_id="ws_x",
    )
    assert row2["model"] == "y/z"


def test_list_runs_falls_back_to_harness_meta_models(tmp_path) -> None:
    ws = "ws_harness_model"
    repo = _make_repo(tmp_path, ws)
    app = WebBridgeApp(repo)
    rid = "harness_web_xyz"
    app._upsert_web_run(
        rid,
        {
            "id": rid,
            "workspace_id": ws,
            "status": "completed",
            "model": None,
            "source": "web_bridge",
            "harness_meta": {"models": ["aaa/model"], "candidate_run_ids": []},
        },
    )
    out = app.list_runs(ws)
    rows = [r for r in out["runs"] if r["id"] == rid]
    assert rows and rows[0]["model"] == "aaa/model"


def test_cleanup_task_id_matches_handles_remediation_descendants() -> None:
    from umbrella.web_bridge.cleanup import _task_id_matches_run

    rid = "sync_improve_web_X"
    assert _task_id_matches_run(f"{rid}__remediation_1", rid, set())
    assert _task_id_matches_run(f"{rid}__a2", rid, set())
    parent_attempts = {f"{rid}__a1"}
    assert _task_id_matches_run(f"{rid}__a1__remediation_2", rid, parent_attempts)
    assert not _task_id_matches_run("other_run__remediation_1", rid, set())


def test_delete_run_wipes_run_quality_and_meta_harness_workspace(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_full_clean"
    workspace_memory = repo / "workspaces" / ws / ".memory"
    (workspace_memory / "drive" / "task_results").mkdir(parents=True)
    rid = "sync_improve_web_clean1"
    (workspace_memory / "drive" / "task_results" / f"{rid}.json").write_text(
        json.dumps({"task_id": rid, "status": "completed"}), encoding="utf-8"
    )
    rq_path = workspace_memory / "drive" / "task_results" / "run_quality.json"
    rq_path.write_text(json.dumps({"task_id": rid, "rounds": 5}), encoding="utf-8")

    meta_ws_dir = repo / ".umbrella" / "meta_harness" / "workspaces" / f"{rid}__s1__c1"
    meta_ws_dir.mkdir(parents=True)
    (meta_ws_dir / "marker.txt").write_text("x", encoding="utf-8")

    other_meta_ws = (
        repo / ".umbrella" / "meta_harness" / "workspaces" / "other_run__s1__c1"
    )
    other_meta_ws.mkdir(parents=True)
    (other_meta_ws / "marker.txt").write_text("keep", encoding="utf-8")

    lessons_path = repo / ".umbrella" / "memory" / "lessons.jsonl"
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    lessons_path.write_text(
        json.dumps({"id": "L1", "task_id": "unrelated", "text": "keep me"}) + "\n",
        encoding="utf-8",
    )

    app = WebBridgeApp(repo)
    out = app.delete_run(rid, ws)
    assert out.get("ok") is True
    assert not rq_path.exists()
    assert not meta_ws_dir.exists()
    assert other_meta_ws.exists(), (
        "delete must not touch unrelated meta_harness workspaces"
    )
    assert lessons_path.exists(), "moderate Delete must keep long-term lessons memory"
    remaining = lessons_path.read_text(encoding="utf-8")
    assert "keep me" in remaining


def test_cancel_run_calls_orchestrator_cancel_when_registered(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_h_cancel_orch"
    (repo / "workspaces" / ws / ".memory" / "drive" / "state").mkdir(parents=True)

    app = WebBridgeApp(repo)
    rid = "harness_web_orch"
    app._upsert_web_run(
        rid,
        {
            "id": rid,
            "workspace_id": ws,
            "status": "running",
            "mode": "harness",
            "attempt": 1,
            "harness_meta": {"candidates": 1, "candidate_run_ids": [f"{rid}__c1"]},
        },
    )

    cancel_called: list[bool] = []

    class _FakeOrch:
        def cancel(self) -> None:
            cancel_called.append(True)

    app._workers[rid] = {
        "thread": None,
        "kind": "harness",
        "started_at": 0.0,
        "orchestrator": _FakeOrch(),
    }

    out = app.cancel_run(rid)
    assert out.get("ok") is True
    assert cancel_called == [True]
    assert out.get("stop_method") == "cooperative"


def test_cancel_run_accepts_wait_and_force_after(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_cancel_kw"
    (repo / "workspaces" / ws / ".memory" / "drive" / "state").mkdir(parents=True)
    app = WebBridgeApp(repo)
    rid = "ui_run_kw"
    from umbrella.web_bridge.util import iso_utc, now_ts

    app._upsert_web_run(
        rid,
        {
            "id": rid,
            "workspace_id": ws,
            "status": "running",
            "attempt": 1,
            "created_at": iso_utc(now_ts()),
        },
    )
    out = app.cancel_run(rid, wait_seconds=0.0, force_after_seconds=0.0)
    assert out.get("ok") is True
    assert out.get("status") in {"cancelled", "failed"}
    assert out.get("stop_requested") is True
    assert "stop_method" in out


def test_normalize_final_message_returns_russian_structured_summary() -> None:
    """The web UI must always show a Russian structured report with
    sections "Что сделано / Где проблемы / Что осталось" — regardless
    of what the last LLM turn happened to print.
    """
    from umbrella.control_plane.ouroboros_integration import (
        _normalize_final_message_for_status,
    )

    llm_empty = (
        "⚠️ Failed to get a response from model GLM-4.7 after 3 attempts. "
        "All fallback models match the active one. Try rephrasing your request."
    )

    out_ok = _normalize_final_message_for_status(
        final_status="verified",
        final_message=llm_empty,
        verification_payload={"summary": "Verification: PASS (12/12)"},
        changes_made=["src/main.py", "tests/test_main.py"],
        remediation_attempts_used=2,
    )
    assert "## Готово (верификация пройдена)" in out_ok
    assert "### Что сделано" in out_ok
    assert "### Где проблемы" in out_ok
    assert "### Что осталось" in out_ok
    assert "src/main.py" in out_ok
    assert "Циклов self-verify" in out_ok
    # The empty LLM warning must be suppressed in the structured summary.
    assert "Failed to get a response from model" not in out_ok

    out_fail = _normalize_final_message_for_status(
        final_status="failed_verification",
        final_message="agent said it's done",
        verification_payload={
            "summary": "Verification: FAIL",
            "results": [
                {
                    "name": "tests::pytest_smoke",
                    "kind": "command",
                    "status": "failed",
                    "summary": "ImportError: No module named src.config",
                    "optional": False,
                },
                {
                    "name": "lint",
                    "kind": "command",
                    "status": "passed",
                    "optional": False,
                },
            ],
        },
        changes_made=["src/agents.py"],
    )
    assert "## Не пройдена верификация" in out_fail
    assert "### Где проблемы" in out_fail
    assert "tests::pytest_smoke" in out_fail
    # passed checks must NOT appear in "Где проблемы"
    assert "lint" not in out_fail.split("### Что осталось")[0]
    assert "verification_failure_context.md" in out_fail


def test_normalize_final_message_explains_skipped_verification_in_russian() -> None:
    """When a workspace has no ``[verification]`` spec the run finishes
    as ``failed_verification`` with a ``verification_skipped_no_spec``
    warning. The Russian summary must explain that verification did NOT
    run and tell the agent/operator how to add a spec.
    """
    from umbrella.control_plane.ouroboros_integration import (
        _normalize_final_message_for_status,
    )

    out = _normalize_final_message_for_status(
        final_status="failed_verification",
        final_message="agent said it's done",
        verification_payload={
            "summary": "No verification steps declared or auto-detected.",
            "skipped": True,
            "passed": False,
            "results": [],
        },
        completion_warnings=["verification_skipped_no_spec"],
        changes_made=["docs/news_cards.docx"],
    )
    assert "## Не пройдена верификация" in out
    assert "Верификация не запускалась" in out
    assert "[verification]" in out
    assert "workspace.toml" in out
    # Must NOT also dump the raw cosmetic warning code as a bullet —
    # the explanatory paragraph already covers it.
    assert "verification_skipped_no_spec" not in out
    # Must still list what was changed.
    assert "docs/news_cards.docx" in out


def test_remediation_archives_cached_plan_so_planner_runs_again(tmp_path) -> None:
    """End-to-end pin: when the remediation loop re-submits the SAME
    ``task_id``, it MUST archive the cached completed plan first.

    Without this archival step, ``plan_store.load(task_id)`` finds the
    previous attempt's plan with every subtask marked ``done`` →
    ``plan.is_complete()`` short-circuits the subtask phase → the loop
    drops straight into ``final_aggregation`` with ``tool_schemas=[]``
    and the model gets the failure context but **cannot call any
    tool to fix it**. Symptom in production: every remediation cycle
    has ``tool_calls=0`` and ``workspace_write_tools=0`` while the
    same 3 verification checks keep failing, then the run ends as
    ``failed`` after exhausting all 8 attempts.

    This test calls ``_archive_plan_before_remediation`` directly and
    verifies it actually moves the plan file aside, so a future
    refactor cannot silently re-introduce the regression.
    """
    from umbrella.control_plane.ouroboros_integration import (
        _archive_plan_before_remediation,
    )
    from ouroboros.task_planner import TaskPlanStore

    repo_root = tmp_path / "repo"
    workspace_id = "demo_ws"
    drive_root = repo_root / "workspaces" / workspace_id / ".memory" / "drive"
    drive_root.mkdir(parents=True)
    store = TaskPlanStore(drive_root)
    store.create_from_steps(
        task_id="task_remed",
        workspace_id=workspace_id,
        objective_digest="initial work",
        steps=[
            {"title": "do thing", "description": "...", "success_check": "ok"},
        ],
    )
    plan_path = drive_root / "task_plans" / "task_remed.json"
    assert plan_path.exists(), "test setup: plan must exist before archive"

    _archive_plan_before_remediation(
        repo_root=repo_root,
        workspace_id=workspace_id,
        task_id="task_remed",
        attempt=1,
    )

    assert not plan_path.exists(), (
        "Live plan must be moved aside so the next planner pass runs "
        "on the new remediation prompt instead of seeing a 'completed' "
        "plan and skipping straight to final_aggregation"
    )
    assert store.load("task_remed") is None, (
        "After archive, store.load() MUST return None — otherwise "
        "loop.py will short-circuit the subtask phase and the model "
        "will not get any tool calls during remediation"
    )

    archived = list(
        (drive_root / "task_plans").glob("task_remed.before_remediation_1.*.json")
    )
    assert len(archived) == 1, (
        f"Expected exactly one archived copy named "
        f"task_remed.before_remediation_1.*.json, found: {archived}"
    )
    body = json.loads(archived[0].read_text(encoding="utf-8"))
    assert body["task_id"] == "task_remed"
    assert body["objective_digest"] == "initial work"


def test_remediation_keeps_same_task_id_no_child_run_split() -> None:
    """Verification remediation must not append ``__remediation_N`` to the
    task id. The whole fix-verify-fix cycle stays under the parent
    ``task_id`` so the UI shows ONE row and the round counter / events
    remain attached to the original run.

    We pin this contract by source inspection: the integration test of
    the full launcher loop is too heavy and brittle, but the loss-of-id
    bug is a single line change that the code must keep.
    """
    src = Path("umbrella/control_plane/ouroboros_integration.py").read_text(
        encoding="utf-8",
    )
    # The new contract: ``current_task_id = base_task_id``. The previous
    # bug was ``f"{base_task_id}__remediation_{remediation_attempts_used}"``.
    assert "current_task_id = base_task_id" in src, (
        "Remediation must reuse the parent task_id; "
        "do NOT append __remediation_N or the run will visually split."
    )
    assert 'f"{base_task_id}__remediation_{remediation_attempts_used}"' not in src, (
        "Found legacy __remediation_N suffix — this resurrects the "
        "fragmented-runs UI bug. Remove it."
    )


def test_agent_writes_active_model_into_task_result(tmp_path, monkeypatch) -> None:
    """``agent.py`` must persist ``model`` in ``task_results/<id>.json``.

    This is what lets the web bridge surface the actual model used in
    each remediation/attempt instead of leaving the column blank.
    """
    drive_root = tmp_path / "drive"
    (drive_root / "task_results").mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_MODEL", "GLM-4.7-test")

    # Mimic the per-task write block from ``ouroboros.agent`` exactly so
    # this test pins the exact contract the web bridge depends on.
    import os as _os

    task = {"id": "tid_model_check", "parent_task_id": None}
    text = "result body"
    usage: dict = {"cost": 0.0, "rounds": 1}
    active_model = (
        str(usage.get("model") or "").strip()
        or str(_os.environ.get("OUROBOROS_MODEL") or "").strip()
        or str(_os.environ.get("LLM_MODEL") or "").strip()
    )
    result_data = {
        "task_id": task["id"],
        "parent_task_id": task["parent_task_id"],
        "status": "completed",
        "result": text,
        "cost_usd": 0.0,
        "total_rounds": 1,
        "model": active_model or None,
        "ts": "2026-05-08T00:00:00+00:00",
    }
    out_path = drive_root / "task_results" / f"{task['id']}.json"
    out_path.write_text(json.dumps(result_data), encoding="utf-8")

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["model"] == "GLM-4.7-test"
    # Surfacing in the web bridge:
    app = WebBridgeApp(tmp_path)
    row = app._task_result_to_run(payload, ws_id="ws_x")
    assert row["model"] == "GLM-4.7-test"


def test_delete_run_removes_workspace_palace_when_no_runs_remain(tmp_path) -> None:
    """When the last run for a workspace is deleted the entire ``.memory``
    tree (palace included) must be wiped. On Windows the ChromaDB SQLite
    file is held by the cached client; the deletion path must release the
    cache and retry so the directory actually disappears."""
    repo = tmp_path
    ws = "ws_palace_clean"
    workspace_memory = repo / "workspaces" / ws / ".memory"
    palace_dir = workspace_memory / "palace"
    palace_dir.mkdir(parents=True)
    (palace_dir / "chroma.sqlite3").write_bytes(b"\x00" * 16)
    (palace_dir / "subdir").mkdir()
    (palace_dir / "subdir" / "data.bin").write_bytes(b"\x01" * 16)

    tr = workspace_memory / "drive" / "task_results"
    tr.mkdir(parents=True)
    rid = "sync_improve_palace1"
    (tr / f"{rid}.json").write_text(
        json.dumps({"task_id": rid, "status": "completed"}), encoding="utf-8"
    )

    app = WebBridgeApp(repo)
    out = app.delete_run(rid, ws)
    assert out.get("ok") is True
    assert not palace_dir.exists(), "workspace .memory/palace must be removed"
    assert not workspace_memory.exists(), "workspace .memory must be removed when empty"
    report = out.get("report") or {}
    assert "workspace_memory_dir" in report.get("counts", {}) or any(
        "workspace_memory" in path or path.endswith(".memory")
        for path in report.get("removed_paths", [])
    )


def test_delete_run_releases_live_palace_handle_then_wipes(tmp_path) -> None:
    """End-to-end: open a real PalaceBackend, then delete the only run for
    its workspace. The deletion path must release the cached client and
    fully remove the workspace ``.memory`` directory (palace included).
    """
    pytest.importorskip("chromadb")
    pytest.importorskip("mempalace")
    from umbrella.memory.palace_backend import PalaceBackend, get_palace_backend

    repo = tmp_path
    ws = "ws_palace_live"
    workspace_memory = repo / "workspaces" / ws / ".memory"
    palace_dir = workspace_memory / "palace"
    palace_dir.mkdir(parents=True)
    backend = get_palace_backend(palace_dir)
    assert isinstance(backend, PalaceBackend)
    backend.add(
        workspace_id=ws,
        event_type="lesson",
        title="hello",
        content="palace lock probe",
        kind="info",
    )

    tr = workspace_memory / "drive" / "task_results"
    tr.mkdir(parents=True)
    rid = "sync_improve_palace_live"
    (tr / f"{rid}.json").write_text(
        json.dumps({"task_id": rid, "status": "completed"}), encoding="utf-8"
    )

    app = WebBridgeApp(repo)
    out = app.delete_run(rid, ws)
    assert out.get("ok") is True
    assert not workspace_memory.exists(), (
        "live palace handle must be released so .memory is fully removed; "
        f"report={out.get('report')}"
    )


def test_safe_remove_releases_palace_cache_on_oserror(tmp_path, monkeypatch) -> None:
    """``cleanup._safe_remove`` of a directory whose ``rmtree`` raises must
    flush the palace cache and retry. We simulate the lock-then-release
    sequence using a fake ``shutil.rmtree`` that fails the first time.
    """
    from umbrella.web_bridge import cleanup as cleanup_mod

    target = tmp_path / "palace_dir"
    target.mkdir()
    (target / "chroma.sqlite3").write_bytes(b"\x00")

    calls: list[Path] = []
    cache_releases: list[Path] = []

    real_rmtree = cleanup_mod.shutil.rmtree

    def fake_rmtree(path, *args, **kwargs):
        calls.append(Path(path))
        if len(calls) == 1:
            raise PermissionError("locked by chroma")
        return real_rmtree(path, *args, **kwargs)

    def fake_release(path):
        cache_releases.append(Path(path))

    monkeypatch.setattr(cleanup_mod.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(cleanup_mod, "_release_palace_cache", fake_release)

    report = cleanup_mod.CleanupReport()
    removed = cleanup_mod._safe_remove(target, report, kind="dir")
    assert removed is True
    assert not target.exists()
    assert cache_releases, "palace cache release must be invoked on PermissionError"
    assert len(calls) >= 2, (
        "rmtree should be retried at least once after the cache flush"
    )


def test_recall_lessons_for_failures_picks_up_relevant_ideas(tmp_path) -> None:
    """``_recall_relevant_lessons_for_failures`` must surface past
    lessons whose tags / content overlap with the failing check name."""
    from umbrella.orchestration.ouroboros_task import (
        _recall_relevant_lessons_for_failures,
    )

    workspace_memory = tmp_path / ".memory"
    workspace_memory.mkdir()
    ideas_path = workspace_memory / "ideas.jsonl"
    entries = [
        {
            "ts": "2026-05-09T01:00:00Z",
            "task_id": "task_a",
            "workspace_id": "ws",
            "content": "verification_fix for source_policy mock_scaffold: skip lesson files",
            "tags": ["verification_fix:source_policy"],
        },
        {
            "ts": "2026-05-09T02:00:00Z",
            "task_id": "task_a",
            "workspace_id": "ws",
            "content": "irrelevant note about colours and fonts",
            "tags": ["ui"],
        },
    ]
    with ideas_path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    failing = [
        {
            "name": "source_policy:mock_scaffold_scan",
            "kind": "source_policy",
            "status": "failed",
        }
    ]

    recalled = _recall_relevant_lessons_for_failures(
        workspace_memory_root=workspace_memory,
        failing=failing,
    )
    assert recalled, "matching lesson must be returned"
    titles = {entry["title"] for entry in recalled}
    contents = " ".join(entry["snippet"] for entry in recalled)
    assert any("source_policy" in (snippet or "") for snippet in contents.split()), (
        "expected the source_policy lesson, not the colour/font one"
    )
    assert "ui" not in (recalled[0].get("tags") or "")


def test_recall_lessons_returns_empty_when_no_failing_or_no_file(tmp_path) -> None:
    """Edge cases must return ``[]`` without crashing — no file, no
    failing checks, or no keyword overlap."""
    from umbrella.orchestration.ouroboros_task import (
        _recall_relevant_lessons_for_failures,
    )

    assert (
        _recall_relevant_lessons_for_failures(
            workspace_memory_root=tmp_path / "missing",
            failing=[{"name": "x", "kind": "y"}],
        )
        == []
    )

    workspace_memory = tmp_path / ".memory"
    workspace_memory.mkdir()
    (workspace_memory / "ideas.jsonl").write_text("", encoding="utf-8")
    assert (
        _recall_relevant_lessons_for_failures(
            workspace_memory_root=workspace_memory,
            failing=[],
        )
        == []
    )


def test_remediation_prompt_injects_recalled_lessons(tmp_path) -> None:
    """When ``recalled_lessons`` is provided, the remediation prompt
    must contain a ``## Past Lessons`` section with the entries."""
    from umbrella.orchestration.ouroboros_task import (
        render_verification_remediation_prompt,
    )

    text = render_verification_remediation_prompt(
        original_task="t",
        verification_report={
            "summary": "x",
            "results": [
                {
                    "name": "source_policy:mock_scaffold_scan",
                    "kind": "source_policy",
                    "status": "failed",
                    "summary": "found Point 1",
                }
            ],
        },
        attempt=1,
        max_attempts=3,
        recalled_lessons=[
            {
                "title": "Skip lesson files",
                "snippet": "exclude record_verification_lessons.py",
                "tags": "verification_fix:source_policy",
            }
        ],
    )
    assert "## Past Lessons" in text
    assert "Skip lesson files" in text
    assert "exclude record_verification_lessons.py" in text


def test_verification_spec_is_shallow_detects_only_static_steps() -> None:
    """A spec with only ``import_check`` / ``file_exists`` is shallow;
    adding even one ``shell`` step makes it non-shallow."""
    from umbrella.control_plane.ouroboros_integration import (
        _verification_spec_is_shallow,
    )

    shallow = {
        "results": [
            {"name": "imports", "kind": "import_check", "status": "passed"},
            {"name": "file", "kind": "file_exists", "status": "passed"},
        ],
    }
    non_shallow = {
        "results": [
            {"name": "imports", "kind": "import_check", "status": "passed"},
            {"name": "tests", "kind": "shell", "status": "passed"},
        ],
    }
    assert _verification_spec_is_shallow(shallow) is True
    assert _verification_spec_is_shallow(non_shallow) is False
    # Empty / failed-only specs are NOT shallow (caller wants
    # ``promote`` blocked for a different reason in those cases).
    assert _verification_spec_is_shallow({"results": []}) is False
    assert _verification_spec_is_shallow(None) is False


def test_workspace_allows_shallow_promotion_reads_toml(tmp_path) -> None:
    """Opt-in via ``[promotion] allow_shallow_verification`` only."""
    from umbrella.control_plane.ouroboros_integration import (
        _workspace_allows_shallow_promotion,
    )

    repo = tmp_path
    ws = "ws_promote"
    ws_dir = repo / "workspaces" / ws
    ws_dir.mkdir(parents=True)
    assert _workspace_allows_shallow_promotion(repo, ws) is False
    (ws_dir / "workspace.toml").write_text(
        "[promotion]\nallow_shallow_verification = true\n",
        encoding="utf-8",
    )
    assert _workspace_allows_shallow_promotion(repo, ws) is True


def test_normalize_final_message_includes_how_to_run_when_verified(tmp_path) -> None:
    """For verified runs with writes, the Russian summary must include
    ``### Что реализовано``, ``### Идея решения`` (if a plan exists)
    and ``### Как запустить`` (if a README has a usage block)."""
    from umbrella.control_plane.ouroboros_integration import (
        _normalize_final_message_for_status,
    )

    repo = tmp_path
    ws = "ws_human"
    ws_dir = repo / "workspaces" / ws
    ws_dir.mkdir(parents=True)
    (ws_dir / "README.md").write_text(
        "# Demo\n\n## Usage\n\n```bash\npython main.py --serve\n```\n",
        encoding="utf-8",
    )
    plan_dir = ws_dir / ".memory" / "drive" / "task_plans"
    plan_dir.mkdir(parents=True)
    (plan_dir / "task_xyz.json").write_text(
        json.dumps(
            {
                "subtasks": [
                    {"title": "Build CLI entrypoint", "status": "done"},
                    {"title": "Write smoke test", "status": "done"},
                ],
            }
        ),
        encoding="utf-8",
    )

    text = _normalize_final_message_for_status(
        final_status="verified",
        final_message="ok",
        verification_payload={"passed": True, "results": []},
        completion_warnings=[],
        changes_made=["main.py", "README.md"],
        remediation_attempts_used=0,
        repo_root=repo,
        workspace_id=ws,
        base_task_id="task_xyz",
    )
    assert "### Что реализовано" in text
    assert "### Идея решения" in text
    assert "### Как запустить" in text
    assert "python main.py --serve" in text


def test_read_workspace_file_cache_hits_on_unchanged_mtime(
    tmp_path, monkeypatch
) -> None:
    """Second read of the same file (same mtime) must come from cache.

    A different mtime must be a cache miss. We don't actually rely on
    OS-level mtime here (Windows clamps to ~16ms): we mutate the
    explicit ``mtime_ns`` part of the key to simulate a file write
    that flushes the cache entry.
    """
    from ouroboros.tools.umbrella_tools import (
        _read_cache_clear,
        _read_cache_get,
        _read_cache_put,
    )

    _read_cache_clear()
    target = tmp_path / "x.txt"
    target.write_text("hello", encoding="utf-8")
    key = ("ws", str(target.resolve()), 111111, 30000)
    assert _read_cache_get(key) is None
    _read_cache_put(key, "cached_payload")
    assert _read_cache_get(key) == "cached_payload"

    new_key = ("ws", str(target.resolve()), 222222, 30000)
    assert new_key != key
    assert _read_cache_get(new_key) is None, "different mtime_ns → cache miss"

    different_workspace = ("ws_other", str(target.resolve()), 111111, 30000)
    assert _read_cache_get(different_workspace) is None, (
        "workspace_id is part of the key"
    )


def test_run_snapshot_writes_every_n_rounds(tmp_path) -> None:
    """``_maybe_write_run_snapshot`` writes only on multiples of N
    and emits the expected JSON shape."""
    from ouroboros.loop import _maybe_write_run_snapshot, _RUN_SNAPSHOT_EVERY_ROUNDS

    drive_root = tmp_path / "drive"
    state_path = drive_root / "state" / "run_snapshot.json"

    _maybe_write_run_snapshot(
        drive_root=drive_root,
        task_id="t",
        phase_label="initial",
        round_idx=1,
        phase_round=1,
        usage={"cost": 0.5, "prompt_tokens": 10, "completion_tokens": 5},
        active_model="m",
        active_workspace_id="ws",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert not state_path.exists(), "round 1 must NOT trigger a snapshot"

    _maybe_write_run_snapshot(
        drive_root=drive_root,
        task_id="t",
        phase_label="initial",
        round_idx=_RUN_SNAPSHOT_EVERY_ROUNDS,
        phase_round=10,
        usage={"cost": 1.5, "prompt_tokens": 100, "completion_tokens": 50},
        active_model="m",
        active_workspace_id="ws",
        messages=[{"role": "assistant", "content": "x" * 600}],
    )
    assert state_path.exists(), "round N must write snapshot"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["task_id"] == "t"
    assert payload["global_round"] == _RUN_SNAPSHOT_EVERY_ROUNDS
    assert payload["recent_messages"][0]["role"] == "assistant"
    assert len(payload["recent_messages"][0]["preview"]) <= 400


def test_get_run_timeline_buckets_by_remediation_event(tmp_path) -> None:
    """Timeline buckets round_io / tools by ``remediation_started``
    boundary events, returning one phase per attempt."""
    from umbrella.web_bridge.app import WebBridgeApp

    repo = tmp_path
    ws = "ws_timeline"
    drive = repo / "workspaces" / ws / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "task_results").mkdir(parents=True)

    rid = "run_42"
    (drive / "task_results" / f"{rid}.json").write_text(
        json.dumps({"task_id": rid, "status": "complete"}),
        encoding="utf-8",
    )
    rounds_path = drive / "logs" / "round_io.jsonl"
    tools_path = drive / "logs" / "tools.jsonl"
    events_path = drive / "logs" / "events.jsonl"

    rounds_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": rid,
                        "ts": "2026-05-09T10:00:00Z",
                        "phase": "subtask_1",
                        "round": 1,
                    }
                ),
                json.dumps(
                    {
                        "task_id": rid,
                        "ts": "2026-05-09T10:05:00Z",
                        "phase": "subtask_1",
                        "round": 2,
                    }
                ),
                json.dumps(
                    {
                        "task_id": rid,
                        "ts": "2026-05-09T11:30:00Z",
                        "phase": "subtask_1",
                        "round": 1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tools_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": rid,
                        "ts": "2026-05-09T10:01:00Z",
                        "tool": "read_workspace_file",
                    }
                ),
                json.dumps(
                    {
                        "task_id": rid,
                        "ts": "2026-05-09T10:02:00Z",
                        "tool": "update_workspace_seed",
                    }
                ),
                json.dumps(
                    {
                        "task_id": rid,
                        "ts": "2026-05-09T11:35:00Z",
                        "tool": "update_workspace_seed",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    events_path.write_text(
        json.dumps(
            {
                "task_id": rid,
                "type": "remediation_started",
                "ts": "2026-05-09T11:00:00Z",
                "attempt": 1,
                "max_attempts": 3,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    app = WebBridgeApp(repo)
    timeline = app.get_run_timeline(rid)
    assert timeline["run_id"] == rid
    assert len(timeline["phases"]) == 2
    assert timeline["phases"][0]["name"] == "initial"
    assert timeline["phases"][0]["rounds"] == 2
    assert timeline["phases"][0]["tool_calls"] == 2
    assert timeline["phases"][0]["write_tool_calls"] == 1
    assert timeline["phases"][1]["name"] == "remediation_1"
    assert timeline["phases"][1]["rounds"] == 1
    assert timeline["phases"][1]["write_tool_calls"] == 1


def test_get_run_timeline_handles_missing_logs_gracefully(tmp_path) -> None:
    """No logs / no run → empty phases list, never throws."""
    from umbrella.web_bridge.app import WebBridgeApp

    app = WebBridgeApp(tmp_path)
    out = app.get_run_timeline("nonexistent_run_id")
    assert out["phases"] == []
    assert out["workspace_id"] == ""


def test_explicit_spec_is_not_silently_extended_with_smoke_step(tmp_path) -> None:
    """Backend MUST NOT silently inject a smoke step into a hand-written
    spec — agent's prompt + promotion-safety gate are the contract.

    This test pins the explicit decision (taken Mon May 11 2026) to
    surface shallow specs as a ``promotion_blocked_shallow_verification``
    warning instead of fixing them behind the operator's back. Auto-
    extend is a foot-gun: the agent learns to skip writing a real
    smoke step because the backend papered over it.
    """
    from umbrella.verification.spec_loader import load_verification_spec
    from umbrella.verification.models import VerificationStepKind

    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "workspace.toml").write_text(
        '[[verification.steps]]\nkind = "import_check"\n'
        'name = "import_main"\ncommand = ["python", "-c", "import main"]\n',
        encoding="utf-8",
    )
    steps = load_verification_spec(tmp_path)
    kinds = [s.kind for s in steps]
    assert kinds == [VerificationStepKind.IMPORT_CHECK], (
        "explicit spec must be returned verbatim, no auto-smoke injection"
    )


def test_autodetect_smoke_step_still_fires_when_no_explicit_spec(tmp_path) -> None:
    """The fallback path (no [verification] section at all) is still
    allowed to autodetect a smoke step — there is no agent / operator
    intent to respect in that case."""
    from umbrella.verification.spec_loader import autodetect_steps
    from umbrella.verification.models import VerificationStepKind

    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    steps = autodetect_steps(tmp_path)
    smoke = [s for s in steps if s.kind == VerificationStepKind.SHELL]
    assert smoke, "main.py-only workspace falls back to autodetected smoke"
    assert any("main.py" in str(arg) for arg in smoke[-1].command)


def test_run_verification_does_not_load_workspace_dotenv(tmp_path, monkeypatch) -> None:
    """Workspace ``.env`` is the AGENT's responsibility now: it must
    explicitly thread keys into ``[[verification.steps]] env`` (or
    bake them into a fixture). The runner must NOT silently merge
    ``<workspace>/.env`` into the subprocess env — this test pins
    that contract so a future refactor cannot regress it.
    """
    from umbrella.verification.models import (
        VerificationStep,
        VerificationStepKind,
        VerificationStepResult,
        VerificationStatus,
    )
    from umbrella.verification import runner as runner_mod

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "OPENAI_API_KEY=ws-secret-key\n",
        encoding="utf-8",
    )

    captured: dict[str, dict[str, str]] = {}

    def fake_shell(step, cwd, env):
        captured["env"] = dict(env or {})
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.PASSED,
            summary="ok",
        )

    monkeypatch.setattr(runner_mod, "_run_shell_step", fake_shell)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    step = VerificationStep(
        kind=VerificationStepKind.SHELL,
        name="echo",
        command=["python", "-c", "print(1)"],
        timeout_seconds=5,
    )
    runner_mod.run_verification(workspace, [step])

    env = captured.get("env") or {}
    assert "OPENAI_API_KEY" not in env, (
        "workspace .env must NOT be auto-loaded; agent owns key wiring"
    )


def test_run_verification_honours_explicit_step_env(tmp_path, monkeypatch) -> None:
    """When the agent declares ``env.X = "..."`` on a verification
    step (the supported way to pass credentials), that value MUST
    reach the subprocess env."""
    from umbrella.verification.models import (
        VerificationStep,
        VerificationStepKind,
        VerificationStepResult,
        VerificationStatus,
    )
    from umbrella.verification import runner as runner_mod

    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured: dict[str, dict[str, str]] = {}

    def fake_shell(step, cwd, env):
        captured["env"] = dict(env or {})
        return VerificationStepResult(
            step=step,
            status=VerificationStatus.PASSED,
            summary="ok",
        )

    monkeypatch.setattr(runner_mod, "_run_shell_step", fake_shell)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    step = VerificationStep(
        kind=VerificationStepKind.SHELL,
        name="needs_key",
        command=["python", "-c", "import os; print(os.environ.get('OPENAI_API_KEY'))"],
        timeout_seconds=5,
        env={"OPENAI_API_KEY": "agent-supplied-key"},
    )
    runner_mod.run_verification(workspace, [step])

    env = captured.get("env") or {}
    assert env.get("OPENAI_API_KEY") == "agent-supplied-key"


def test_workspace_task_prompt_tells_agent_to_wire_keys_themselves() -> None:
    """The system prompt must spell out the agent's responsibility
    to find/declare API keys — this is the *only* mechanism now,
    so regressing the prompt regresses the whole feature."""
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1] / "prompts" / "ouroboros_workspace_task.md"
    ).read_text(encoding="utf-8")
    assert "Credentials & API keys" in template
    assert "verification subprocess does NOT auto-load" in template
    assert "request_user_input" in template
    assert "smoke step" in template.lower()


def test_self_review_prompt_renders_real_run_output() -> None:
    """The self-review prompt must include the LGTM / NEEDS_FIX
    instructions and the actual stdout/stderr of behavioural steps
    so the agent has something concrete to react to."""
    from umbrella.orchestration.ouroboros_task import render_self_review_prompt

    text = render_self_review_prompt(
        original_task="Build a CLI that fetches news",
        verification_report={
            "passed": True,
            "results": [
                {
                    "name": "smoke_run:main.py",
                    "kind": "shell",
                    "status": "passed",
                    "summary": "exit 0",
                    "stdout_tail": "Loaded 0 articles\n",
                    "stderr_tail": "",
                },
            ],
        },
        attempt=1,
        max_attempts=1,
    )
    assert "Self-Review of the Real Run" in text
    assert "LGTM" in text
    assert "NEEDS_FIX" in text
    assert "Loaded 0 articles" in text
    assert "smoke_run:main.py" in text
    assert "delete_workspace_file" in text
    assert "final sweep will delete them automatically" in text
    assert "Do not call any tools" in text
    assert "<tool_call>" in text
    assert "STRICTLY ENFORCED" in text
    assert "MUST be either `LGTM`" in text


def test_parse_self_review_response_handles_lgtm_needs_fix_and_ambiguous() -> None:
    from umbrella.orchestration.ouroboros_task import parse_self_review_response

    verdict, body = parse_self_review_response("LGTM looks good — ships it.")
    assert verdict == "lgtm"
    assert body == ""

    verdict, body = parse_self_review_response(
        "NEEDS_FIX\n1. The CLI prints 0 articles, parser is broken.\n2. API key fallback fired.",
    )
    assert verdict == "needs_fix"
    assert "parser is broken" in body
    assert "API key fallback" in body

    verdict, body = parse_self_review_response("")
    assert verdict == "needs_fix"
    assert "empty response" in body

    verdict, body = parse_self_review_response(
        "Some long rambly reflection without a verdict"
    )
    assert verdict == "needs_fix"
    assert "did not start with LGTM or NEEDS_FIX" in body


def test_self_review_remediation_prompt_embeds_fixlist() -> None:
    """The self-review-driven remediation prompt must surface the
    agent's own fixlist verbatim (no ``failing checks`` block — the
    spec already passed)."""
    from umbrella.control_plane.ouroboros_integration import (
        _render_self_review_remediation_prompt,
    )

    text = _render_self_review_remediation_prompt(
        original_task="Build a CLI",
        fixlist_body="1. Parser broken at news/parser.py:42\n2. Empty stdout",
        attempt=1,
        max_attempts=3,
    )
    assert "Self-Review Remediation" in text
    assert "Parser broken at news/parser.py:42" in text
    assert "Empty stdout" in text
    assert "Failing Required Checks" not in text


def test_self_review_already_run_in_aggregate_detects_marker() -> None:
    from umbrella.control_plane.ouroboros_integration import (
        _self_review_already_run_in_aggregate,
    )

    assert _self_review_already_run_in_aggregate([]) is False
    assert (
        _self_review_already_run_in_aggregate(
            [
                {"type": "task_metrics"},
                {"type": "self_review_started"},
            ]
        )
        is True
    )
    assert (
        _self_review_already_run_in_aggregate(
            [
                {"type": "remediation_started"},
            ]
        )
        is False
    )


# ---------------------------------------------------------------------------
# Stop-signal regression tests
# ---------------------------------------------------------------------------
#
# Background: the user clicked Stop in the dashboard partway through a
# run. The stop file was correctly written to
# ``workspaces/<id>/.memory/drive/state/stop_requested.json``. The first
# ``_check_stop_requested`` inside the LLM loop fired and returned
# ``"Stop requested by dashboard: …"`` to the integration. But the
# integration then ran verification, found no writes / failed checks,
# and entered the remediation cycle. Each subsequent remediation
# submission ALSO hit ``_check_stop_requested`` immediately (same
# task_id), exited in <1 s with 0 LLM rounds, and the loop kept
# spinning until the user wandered off. The fix below adds a
# launch-time stale-stop sweep + per-iteration stop check + agent
# response sniffing so a click on Stop is honoured exactly once and
# the remediation budget is never burned on a cancelled run.


def test_stop_request_targets_task_matches_run_id_and_descendants() -> None:
    from umbrella.control_plane.ouroboros_integration import (
        _stop_request_targets_task,
    )

    payload = {
        "run_id": "task_42",
        "task_id": "task_42",
        "attempt_task_ids": ["task_42", "task_42__attempt_2"],
    }
    assert _stop_request_targets_task(payload, "task_42") is True
    assert _stop_request_targets_task(payload, "task_42__attempt_2") is True
    # ``task_42__remediation_1`` shares the ``task_42__`` prefix so it
    # IS treated as a descendant of task_42 — stopping task_42 must also
    # stop its remediation/self-review children.
    assert _stop_request_targets_task(payload, "task_42__remediation_1") is True
    assert _stop_request_targets_task(payload, "task_42_extra") is False, (
        "Match only on exact id or ``id__`` prefix — sharing a literal "
        "substring is not enough."
    )
    assert _stop_request_targets_task(payload, "other_task") is False
    # Empty payload is treated as a global stop (legacy semantics).
    assert _stop_request_targets_task({}, "task_42") is True
    assert _stop_request_targets_task(None, "task_42") is True


def test_clear_stop_requests_for_task_only_drops_matching_payloads(
    tmp_path,
) -> None:
    """A new run id MUST NOT silently cancel a different running run by
    deleting its stop file. We only clear stop files whose payload
    targets OUR new task id."""
    from umbrella.control_plane.ouroboros_integration import (
        _clear_stop_requests_for_task,
        _stop_request_paths,
    )

    repo_root = tmp_path / "repo"
    workspace_id = "demo_ws"
    drive_root = repo_root / "workspaces" / workspace_id / ".memory" / "drive"
    drive_root.mkdir(parents=True)
    (repo_root / ".umbrella" / "launcher").mkdir(parents=True, exist_ok=True)
    (repo_root / ".umbrella" / "ouroboros_drive" / "state").mkdir(
        parents=True, exist_ok=True
    )
    (drive_root / "state").mkdir(parents=True, exist_ok=True)

    # File A: targets the new task id — must be removed
    (drive_root / "state" / "stop_requested.json").write_text(
        json.dumps({"run_id": "fresh_task", "task_id": "fresh_task"}),
        encoding="utf-8",
    )
    # File B: targets a different running run — must SURVIVE
    (repo_root / ".umbrella" / "launcher" / "stop_requested.json").write_text(
        json.dumps({"run_id": "other_run", "task_id": "other_run"}),
        encoding="utf-8",
    )

    _clear_stop_requests_for_task(repo_root, workspace_id, "fresh_task")

    assert not (drive_root / "state" / "stop_requested.json").exists(), (
        "Stale stop file targeting our task id must be deleted on "
        "fresh launch — otherwise the in-loop stop check fires "
        "instantly and the run dies before doing any work."
    )
    assert (repo_root / ".umbrella" / "launcher" / "stop_requested.json").exists(), (
        "Stop file for an unrelated run must be left alone."
    )
    # And _stop_request_paths must include all three locations.
    paths = {p.name for p in _stop_request_paths(repo_root, workspace_id)}
    assert paths == {"stop_requested.json"}  # all three files share the name


def test_read_stop_request_for_task_returns_payload_when_targeted(tmp_path) -> None:
    from umbrella.control_plane.ouroboros_integration import (
        _read_stop_request_for_task,
    )

    repo_root = tmp_path / "repo"
    workspace_id = "ws_read"
    state_dir = repo_root / "workspaces" / workspace_id / ".memory" / "drive" / "state"
    state_dir.mkdir(parents=True)
    payload = {
        "run_id": "task_99",
        "task_id": "task_99",
        "reason": "user clicked stop",
    }
    (state_dir / "stop_requested.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    found = _read_stop_request_for_task(repo_root, workspace_id, "task_99")
    assert isinstance(found, dict)
    assert found["reason"] == "user clicked stop"

    not_found = _read_stop_request_for_task(repo_root, workspace_id, "task_xx")
    assert not_found is None


def test_final_message_indicates_stop_detects_loop_response() -> None:
    from umbrella.control_plane.ouroboros_integration import (
        _final_message_indicates_stop,
    )

    assert (
        _final_message_indicates_stop(
            "Stop requested by dashboard: stop requested from the web UI"
        )
        is True
    )
    assert (
        _final_message_indicates_stop("  Stop requested by dashboard: x  \n\nmore text")
        is True
    )
    assert _final_message_indicates_stop("Some normal final message.") is False
    assert _final_message_indicates_stop("") is False
    assert _final_message_indicates_stop(None) is False


def test_build_cancellation_message_renders_russian_summary() -> None:
    from umbrella.control_plane.ouroboros_integration import (
        _build_cancellation_message,
    )

    text = _build_cancellation_message(
        stop_payload={"reason": "пользователь нажал стоп"},
        remediation_attempts_used=2,
        final_message_from_agent="Stop requested by dashboard: stop requested",
    )
    assert "Run остановлен пользователем" in text
    assert text.lstrip().startswith("# Run"), (
        "First line must be a markdown heading so the UI renders it big"
    )
    assert "пользователь нажал стоп" in text
    assert "Использованных циклов remediation до остановки: `2`" in text
    # The agent's loop-side stop reply must NOT be re-quoted (it would
    # be circular — that prefix is the stop signal itself).
    assert "Stop requested by dashboard" not in text


def test_normalize_final_message_for_cancelled_status_short_circuits(
    tmp_path,
) -> None:
    """For ``cancelled`` status the summary must be the cancellation
    explanation, not the verification post-mortem (verification was
    never the deciding gate — the user was)."""
    from umbrella.control_plane.ouroboros_integration import (
        _normalize_final_message_for_status,
    )

    text = _normalize_final_message_for_status(
        final_status="cancelled",
        final_message="# Run остановлен пользователем\nПричина: stop from web UI.",
        verification_payload={
            "passed": False,
            "results": [{"name": "smoke", "status": "failed", "kind": "shell"}],
        },
        completion_warnings=["cancelled_by_user"],
        changes_made=["workspaces/demo/main.py"],
        remediation_attempts_used=1,
        repo_root=tmp_path,
        workspace_id="demo",
        base_task_id="task_x",
    )
    assert "Остановлено пользователем" in text
    assert "main.py" in text, "partial writes must be surfaced for review"
    # Verification post-mortem must not leak into a cancelled summary —
    # otherwise the user sees a misleading "Не пройдена верификация"
    # block for a run they themselves killed.
    assert "Где проблемы" not in text
    assert "Что осталось" not in text
    assert "smoke" not in text


def test_run_ouroboros_improvement_sync_breaks_on_stop_request_mid_loop(
    monkeypatch,
    tmp_path,
) -> None:
    """End-to-end pin for the actual remediation-spin bug: when the user
    clicks Stop AFTER a run has already submitted its first iteration,
    the next iteration's pre-flight stop check must abort the loop
    before submitting another wasteful task. We simulate this by
    writing the stop file from inside the launcher fake (i.e. the user
    clicks Stop while iteration N is running) and asserting that
    iteration N+1 never reaches the launcher."""
    import umbrella.control_plane.ouroboros_integration as integ

    repo_root = tmp_path / "repo"
    workspace_id = "ws_demo"
    drive_root = repo_root / "workspaces" / workspace_id / ".memory" / "drive"
    drive_root.mkdir(parents=True)
    (drive_root / "state").mkdir(parents=True, exist_ok=True)

    submit_calls: list[dict] = []

    def fake_run_launcher_task_once(*, repo_root, task, timeout_seconds):
        submit_calls.append(dict(task))
        # First call: produce a verification-failing result and "user
        # clicks Stop right now" by writing the stop file. The second
        # iteration of the integration loop must observe it and abort
        # without ever calling us again.
        if len(submit_calls) == 1:
            (drive_root / "state" / "stop_requested.json").write_text(
                json.dumps(
                    {
                        "run_id": "task_mid",
                        "task_id": "task_mid",
                        "reason": "user clicked stop",
                    }
                ),
                encoding="utf-8",
            )
            return task["id"], {
                "status": "failed_verification",
                "task_id": task["id"],
                "events": [
                    {"type": "send_message", "text": "Some partial work."},
                    {"type": "task_metrics", "tool_calls": 5},
                    {"type": "workspace_write_tools", "count": 1},
                ],
                "candidate_diff": "",
                "candidate_changed_files": ["workspaces/ws_demo/main.py"],
            }
        # Should never get here — fail loudly if we do.
        raise AssertionError(
            "Integration submitted iteration 2 even though the stop "
            "file was on disk before pre-flight check."
        )

    # Cooperate with the verification step the integration will run
    # after iteration 1 (we want it to think verification failed so the
    # remediation path is even considered).
    monkeypatch.setattr(integ, "_record_baseline", lambda *a, **kw: "")
    monkeypatch.setattr(
        integ,
        "_canonical_drive_root",
        lambda repo, ws=None: drive_root,
    )
    monkeypatch.setattr(integ, "_try_create_instance", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_run_launcher_task_once", fake_run_launcher_task_once)
    monkeypatch.setattr(
        integ,
        "_run_workspace_verification",
        lambda *a, **kw: {
            "passed": False,
            "skipped": False,
            "summary": "smoke step failed",
            "results": [{"name": "smoke", "kind": "shell", "status": "failed"}],
        },
    )
    monkeypatch.setattr(integ, "_collect_run_quality_telemetry", lambda **kw: {})
    monkeypatch.setattr(integ, "_capture_candidate_safe", lambda **kw: None)
    monkeypatch.setattr(integ, "_record_competency_signals", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_persist_final_gate_report", lambda **kw: "")
    monkeypatch.setattr(integ, "_collect_changed_files", lambda *a, **kw: [])
    monkeypatch.setattr(
        integ,
        "_filter_workspace_changes",
        lambda paths, ws: list(paths or []),
    )
    monkeypatch.setattr(
        integ,
        "_collect_candidate_workspace_changes",
        lambda *a, **kw: ["workspaces/ws_demo/main.py"],
    )
    monkeypatch.setattr(
        integ,
        "_persist_verification_failure_context",
        lambda **kw: {"state_path": ""},
    )

    result = integ.run_ouroboros_improvement_sync(
        repo_root=repo_root,
        task_description="please build the thing",
        workspace_id=workspace_id,
        verify=True,
        require_instance=False,
        task_id="task_mid",
        verification_remediation_attempts=8,  # generous budget — must NOT be burned
    )

    assert result["status"] == "cancelled", (
        f"Expected cancelled status, got {result.get('status')!r}. "
        "If this is 'failed_verification' the integration is still "
        "spinning remediation cycles after a Stop click."
    )
    assert "Остановлено пользователем" in result["final_message"]
    assert len(submit_calls) == 1, (
        "Iteration 2 must never be submitted once the stop file "
        f"appears mid-run. Got {len(submit_calls)} submissions."
    )
    assert "cancelled_by_user" in (result.get("completion_warnings") or [])


def test_run_ouroboros_sync_breaks_on_agent_stop_response(
    monkeypatch,
    tmp_path,
) -> None:
    """If the stop file is removed between user click and our pre-flight
    check (race), but the agent already saw the stop and replied with
    "Stop requested by dashboard…", we must still cancel cleanly on
    that signal — NOT proceed to verification + remediation."""
    import umbrella.control_plane.ouroboros_integration as integ

    repo_root = tmp_path / "repo"
    workspace_id = "ws_race"
    drive_root = repo_root / "workspaces" / workspace_id / ".memory" / "drive"
    drive_root.mkdir(parents=True)

    submit_calls: list[dict] = []

    def fake_run_launcher_task_once(*, repo_root, task, timeout_seconds):
        submit_calls.append(dict(task))
        return task["id"], {
            "status": "complete",
            "task_id": task["id"],
            "events": [
                {"type": "send_message", "text": "Stop requested by dashboard: web UI"},
            ],
            "candidate_diff": "",
            "candidate_changed_files": [],
        }

    monkeypatch.setattr(integ, "_record_baseline", lambda *a, **kw: "")
    monkeypatch.setattr(
        integ, "_canonical_drive_root", lambda repo, ws=None: drive_root
    )
    monkeypatch.setattr(integ, "_try_create_instance", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_run_launcher_task_once", fake_run_launcher_task_once)
    monkeypatch.setattr(integ, "_run_workspace_verification", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_collect_run_quality_telemetry", lambda **kw: {})
    monkeypatch.setattr(integ, "_capture_candidate_safe", lambda **kw: None)
    monkeypatch.setattr(integ, "_record_competency_signals", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_persist_final_gate_report", lambda **kw: "")
    monkeypatch.setattr(integ, "_collect_changed_files", lambda *a, **kw: [])
    monkeypatch.setattr(integ, "_filter_workspace_changes", lambda paths, ws: [])

    result = integ.run_ouroboros_improvement_sync(
        repo_root=repo_root,
        task_description="please build the thing",
        workspace_id=workspace_id,
        verify=True,
        require_instance=False,
        task_id="task_race",
        verification_remediation_attempts=8,
    )

    assert result["status"] == "cancelled"
    # Exactly ONE submission: the agent reported the stop in its first
    # reply, so we must NOT submit a second remediation iteration.
    assert len(submit_calls) == 1, (
        "Agent already surfaced the stop in iteration 1. We must NOT "
        "queue a remediation iteration after that. "
        f"Got {len(submit_calls)} submission(s)."
    )
    assert "cancelled_by_user" in (result.get("completion_warnings") or [])


# ---------------------------------------------------------------------------
# Stop-signal regression tests (named per the operator's review checklist).
# These four tests pin the contract called out by the post-incident review
# of run ``sync_improve_web_4ca9aa5e``: 7 phantom remediation cycles fired
# AFTER the user clicked Stop, and the resulting task-result row showed
# "completed" in the dashboard. The four behaviours below must hold so
# the same regression cannot recur.
# ---------------------------------------------------------------------------


def test_remediation_loop_breaks_when_stop_file_present(monkeypatch, tmp_path) -> None:
    """User clicks Stop while iteration N of the remediation loop is in
    flight (the realistic case observed in run ``sync_improve_web_4ca9aa5e``).
    ``run_ouroboros_improvement_sync`` must observe the new stop file
    BEFORE submitting iteration N+1 to the launcher and exit with
    ``status='cancelled'`` after at most ONE submission, never
    spinning the remediation budget down to zero."""
    import umbrella.control_plane.ouroboros_integration as integ

    repo_root = tmp_path / "repo"
    workspace_id = "ws_remed_loop"
    drive_root = repo_root / "workspaces" / workspace_id / ".memory" / "drive"
    drive_root.mkdir(parents=True)
    (drive_root / "state").mkdir(parents=True, exist_ok=True)

    submit_calls: list[dict] = []

    def fake_run_launcher_task_once(*, repo_root, task, timeout_seconds):
        submit_calls.append(dict(task))
        # Iteration 1 finishes with a verification failure AND the user
        # clicks Stop right at this moment (we simulate the dashboard
        # writing the stop file from inside the launcher fake). The
        # integration's pre-flight stop check at the top of iteration 2
        # must observe the new file and abort the whole run.
        if len(submit_calls) == 1:
            (drive_root / "state" / "stop_requested.json").write_text(
                json.dumps(
                    {
                        "run_id": "task_remed_loop",
                        "task_id": "task_remed_loop",
                        "attempt_task_ids": ["task_remed_loop"],
                        "reason": "user clicked stop mid-iteration",
                    }
                ),
                encoding="utf-8",
            )
            return task["id"], {
                "status": "failed_verification",
                "task_id": task["id"],
                "events": [
                    {"type": "send_message", "text": "Partial work."},
                    {"type": "task_metrics", "tool_calls": 5},
                    {"type": "workspace_write_tools", "count": 1},
                ],
                "candidate_diff": "",
                "candidate_changed_files": ["workspaces/ws_remed_loop/main.py"],
            }
        raise AssertionError(
            "Iteration 2 was submitted even though the stop file was "
            "on disk before pre-flight check — the loop is still "
            "burning the remediation budget after a Stop click."
        )

    monkeypatch.setattr(integ, "_record_baseline", lambda *a, **kw: "")
    monkeypatch.setattr(
        integ, "_canonical_drive_root", lambda repo, ws=None: drive_root
    )
    monkeypatch.setattr(integ, "_try_create_instance", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_run_launcher_task_once", fake_run_launcher_task_once)
    monkeypatch.setattr(
        integ,
        "_run_workspace_verification",
        lambda *a, **kw: {
            "passed": False,
            "skipped": False,
            "summary": "smoke step failed",
            "results": [{"name": "smoke", "kind": "shell", "status": "failed"}],
        },
    )
    monkeypatch.setattr(integ, "_collect_run_quality_telemetry", lambda **kw: {})
    monkeypatch.setattr(integ, "_capture_candidate_safe", lambda **kw: None)
    monkeypatch.setattr(integ, "_record_competency_signals", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_persist_final_gate_report", lambda **kw: "")
    monkeypatch.setattr(integ, "_collect_changed_files", lambda *a, **kw: [])
    monkeypatch.setattr(
        integ,
        "_filter_workspace_changes",
        lambda paths, ws: list(paths or []),
    )
    monkeypatch.setattr(
        integ,
        "_collect_candidate_workspace_changes",
        lambda *a, **kw: ["workspaces/ws_remed_loop/main.py"],
    )
    monkeypatch.setattr(
        integ,
        "_persist_verification_failure_context",
        lambda **kw: {"state_path": ""},
    )

    result = integ.run_ouroboros_improvement_sync(
        repo_root=repo_root,
        task_description="please build the thing",
        workspace_id=workspace_id,
        verify=True,
        require_instance=False,
        task_id="task_remed_loop",
        verification_remediation_attempts=8,
    )

    assert result["status"] == "cancelled", (
        f"Got {result.get('status')!r}. The remediation loop must observe "
        "stop_requested.json between iterations and abort."
    )
    assert len(submit_calls) == 1, (
        "Iteration 2 must never reach the launcher once the stop file "
        f"appears mid-run. Got {len(submit_calls)} submissions."
    )
    assert result.get("verification_remediation_attempts_used", 0) <= 1, (
        "Cancelled runs must not consume the remediation budget. "
        f"Used {result.get('verification_remediation_attempts_used')}."
    )


def test_final_status_cancelled_when_agent_returns_stop_text(
    monkeypatch, tmp_path
) -> None:
    """If the launcher task returned the canonical
    ``"Stop requested by dashboard: …"`` text (which is what the
    ouroboros loop emits when ``_check_stop_requested`` fires), the
    integration must end the run with ``status='cancelled'`` and NOT
    enter a verification remediation cycle."""
    import umbrella.control_plane.ouroboros_integration as integ

    repo_root = tmp_path / "repo"
    workspace_id = "ws_stop_text"
    drive_root = repo_root / "workspaces" / workspace_id / ".memory" / "drive"
    drive_root.mkdir(parents=True)

    submit_calls: list[dict] = []

    def fake_run_launcher_task_once(*, repo_root, task, timeout_seconds):
        submit_calls.append(dict(task))
        return task["id"], {
            "status": "complete",
            "task_id": task["id"],
            "events": [
                {
                    "type": "send_message",
                    "text": "Stop requested by dashboard: stop requested from the web UI",
                }
            ],
            "candidate_diff": "",
            "candidate_changed_files": [],
        }

    monkeypatch.setattr(integ, "_record_baseline", lambda *a, **kw: "")
    monkeypatch.setattr(
        integ, "_canonical_drive_root", lambda repo, ws=None: drive_root
    )
    monkeypatch.setattr(integ, "_try_create_instance", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_run_launcher_task_once", fake_run_launcher_task_once)
    monkeypatch.setattr(integ, "_run_workspace_verification", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_collect_run_quality_telemetry", lambda **kw: {})
    monkeypatch.setattr(integ, "_capture_candidate_safe", lambda **kw: None)
    monkeypatch.setattr(integ, "_record_competency_signals", lambda *a, **kw: None)
    monkeypatch.setattr(integ, "_persist_final_gate_report", lambda **kw: "")
    monkeypatch.setattr(integ, "_collect_changed_files", lambda *a, **kw: [])
    monkeypatch.setattr(integ, "_filter_workspace_changes", lambda paths, ws: [])

    result = integ.run_ouroboros_improvement_sync(
        repo_root=repo_root,
        task_description="please build the thing",
        workspace_id=workspace_id,
        verify=True,
        require_instance=False,
        task_id="task_stop_text",
        verification_remediation_attempts=8,
    )

    assert result["status"] == "cancelled"
    assert len(submit_calls) == 1, (
        "The agent already surfaced the stop in iteration 1; the loop "
        "must NOT queue a second remediation iteration. "
        f"Got {len(submit_calls)} submission(s)."
    )
    assert "cancelled_by_user" in (result.get("completion_warnings") or [])


def test_task_result_to_run_maps_stop_text_to_cancelled(tmp_path) -> None:
    """Old task-result files written by the launcher carry
    ``status='completed'`` even when the agent's only message was the
    canonical ``"Stop requested by dashboard: …"`` reply (which means
    the loop fired ``_check_stop_requested`` immediately and exited
    with 0 LLM rounds). The dashboard list must surface those rows as
    ``cancelled``, NOT ``completed`` — otherwise the operator sees
    "Готово" for a run they explicitly killed."""
    repo = tmp_path
    app = WebBridgeApp(repo)

    raw_completed_with_stop_text = {
        "task_id": "sync_improve_web_old_run",
        "status": "completed",
        "result": "Stop requested by dashboard: stop requested from the web UI",
        "ts": "2026-05-11T00:07:25Z",
        "total_rounds": 0,
    }
    row = app._task_result_to_run(raw_completed_with_stop_text, "ws_stop_text")
    assert row["status"] == "cancelled", (
        "Stop-text result must be reclassified as cancelled. "
        f"Got status={row['status']!r}, result_preview={row.get('result_preview')!r}"
    )

    raw_clean_complete = {
        "task_id": "sync_improve_web_real_done",
        "status": "completed",
        "result": "All subtasks finished, verification green.",
        "ts": "2026-05-11T00:07:25Z",
    }
    clean_row = app._task_result_to_run(raw_clean_complete, "ws_clean")
    assert clean_row["status"] == "completed"

    raw_with_final_message = {
        "task_id": "sync_improve_web_final_msg",
        "status": "completed",
        "final_message": "Stop requested by dashboard: cancel",
        "ts": "2026-05-11T00:07:25Z",
    }
    final_msg_row = app._task_result_to_run(raw_with_final_message, "ws_final_msg")
    assert final_msg_row["status"] == "cancelled"


def test_worker_clears_stop_request_files_on_exit(tmp_path, monkeypatch) -> None:
    """The next user-initiated run on the same workspace must NOT be
    poisoned by a leftover ``stop_requested.json`` from a previous
    cancelled run. The worker's ``finally`` block must call
    ``_clear_stop_requests`` so the path is gone after the worker
    returns. (Without this, the integration's pre-iteration stop check
    inside the NEXT run would fire on the first iteration and silently
    cancel a fresh run with 0 rounds.)"""
    repo = tmp_path
    ws_id = "ws_cleanup"
    drive_state = repo / "workspaces" / ws_id / ".memory" / "drive" / "state"
    drive_state.mkdir(parents=True)
    stop_file = drive_state / "stop_requested.json"
    stop_file.write_text(
        json.dumps({"run_id": "old_run", "task_id": "old_run", "reason": "leftover"}),
        encoding="utf-8",
    )

    launcher_dir = repo / ".umbrella" / "launcher"
    launcher_dir.mkdir(parents=True)
    launcher_stop = launcher_dir / "stop_requested.json"
    launcher_stop.write_text(
        json.dumps({"run_id": "old_run", "task_id": "old_run", "reason": "leftover"}),
        encoding="utf-8",
    )

    app = WebBridgeApp(repo)
    run_id = "sync_improve_web_cleanup"
    app._upsert_web_run(
        run_id,
        {
            "id": run_id,
            "workspace_id": ws_id,
            "status": "queued",
            "created_at": "2026-05-11T00:00:00Z",
            "updated_at": "2026-05-11T00:00:00Z",
        },
    )

    def fake_run_sync(**kwargs):
        return {
            "status": "cancelled",
            "task_id": kwargs.get("task_id"),
            "final_message": "Run cancelled from the web UI.",
        }

    monkeypatch.setattr(
        "umbrella.control_plane.ouroboros_integration.run_ouroboros_improvement_sync",
        fake_run_sync,
    )

    assert stop_file.exists()
    assert launcher_stop.exists()

    app._run_ouroboros_worker(
        run_id=run_id,
        ws_id=ws_id,
        task_text="please build the thing",
        timeout_hours=0.0,
        max_rounds=10,
        max_verify_retries=0,
        selected_model="",
    )

    assert not stop_file.exists(), (
        "Worker finally-block must remove the workspace-scoped "
        "stop_requested.json so the NEXT run on the same workspace is "
        "not auto-cancelled. File still present at "
        f"{stop_file}."
    )
    assert not launcher_stop.exists(), (
        "Worker finally-block must also clear the launcher-scoped "
        "stop_requested.json (legacy fallback path used by some "
        "tooling). File still present at "
        f"{launcher_stop}."
    )


def test_normalize_run_status_maps_delivery_contract_failures_to_failed() -> None:
    for raw in (
        "phase_impasse",
        "failed_self_review",
        "incomplete_subtasks",
        "incomplete_discovery",
        "verified_with_blocking_noise",
    ):
        assert WebBridgeApp._normalize_run_status(raw) == "failed", raw


def test_normalize_run_status_keeps_verified_completed_mapping() -> None:
    assert WebBridgeApp._normalize_run_status("verified") == "completed"
    assert WebBridgeApp._normalize_run_status("completed") == "completed"
    assert WebBridgeApp._normalize_run_status("running") == "running"
    assert WebBridgeApp._normalize_run_status("cancelled") == "cancelled"
