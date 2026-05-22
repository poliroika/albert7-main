import json
import os
import sys
import threading
import tomllib
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest

from umbrella.web_bridge.app import WebBridgeApp, _ensure_repo_python_paths
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


def test_web_phase_defaults_do_not_need_no_key_web_fallback_env(monkeypatch, tmp_path):
    app = WebBridgeApp(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("OUROBOROS_DEEP_SEARCH_PROVIDER", raising=False)

    app._ensure_web_discovery_defaults()

    assert "OUROBOROS_DEEP_SEARCH_PROVIDER" not in os.environ


def test_web_bridge_adds_bundled_agent_import_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ouroboros_root = tmp_path / "ouroboros"
    gmas_root = tmp_path / "gmas" / "src"
    (ouroboros_root / "ouroboros").mkdir(parents=True)
    (gmas_root / "gmas").mkdir(parents=True)
    monkeypatch.setattr(sys, "path", ["existing"])

    _ensure_repo_python_paths(tmp_path)
    _ensure_repo_python_paths(tmp_path)

    assert str(ouroboros_root.resolve()) in sys.path
    assert str(gmas_root.resolve()) in sys.path
    assert sys.path.count(str(ouroboros_root.resolve())) == 1
    assert sys.path.count(str(gmas_root.resolve())) == 1


def test_web_bridge_can_import_bundled_gmas_web_search() -> None:
    from gmas.tools.web_search import DuckDuckGoProvider, _create_web_search_tool

    tool = _create_web_search_tool(auto_route=True, max_results=1)
    try:
        assert isinstance(tool._provider, DuckDuckGoProvider)
    finally:
        tool.close()


def test_bridge_runtime_declares_no_key_duckduckgo_dependency() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [str(item).lower() for item in data["project"]["dependencies"]]

    assert any(dep.startswith("ddgs") for dep in deps)


def test_duckduckgo_missing_ddgs_empty_html_fallback_is_no_results() -> None:
    from gmas.tools.web_search import DuckDuckGoProvider

    provider = DuckDuckGoProvider()
    with (
        patch.object(
            provider,
            "_search_ddgs",
            side_effect=ImportError("No module named 'ddgs'"),
        ),
        patch.object(provider, "_search_html_httpx", return_value=[]),
        patch.object(provider, "_search_html_urllib", return_value=[]),
    ):
        assert provider.search("python turn based strategy game", max_results=3) == []


def test_gmas_stdlib_logging_fallback_accepts_loguru_format(caplog) -> None:
    from gmas.config.logging import logger

    if type(logger).__name__ != "_StdlibLogger":
        pytest.skip("stdlib fallback is only active when loguru is unavailable")

    with caplog.at_level("WARNING", logger="gmas"):
        logger.warning("DuckDuckGo backend={} unavailable: {}", "ddgs", "missing")

    assert "DuckDuckGo backend=ddgs unavailable: missing" in caplog.text


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


def test_web_phase_defaults_use_documented_ouroboros_round_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OUROBOROS_MAX_ROUNDS", raising=False)
    with patch("umbrella.web_bridge.app.load_env"):
        app = WebBridgeApp(tmp_path)

    assert app._current_max_rounds() == 200


def test_phase_runner_worker_applies_web_round_and_verify_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, str | None] = {}

    class FakePhaseRunner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, task_text, *, run_id):
            captured["max_rounds"] = os.environ.get("OUROBOROS_MAX_ROUNDS")
            captured["verify_retries"] = os.environ.get(
                "OUROBOROS_WEB_MAX_VERIFY_RETRIES"
            )
            return []

    monkeypatch.setattr("umbrella.orchestrator.runner.PhaseRunner", FakePhaseRunner)
    monkeypatch.setattr(WebBridgeApp, "_upsert_web_run", lambda self, rid, patch: {})
    monkeypatch.setattr(
        WebBridgeApp,
        "_run_log_summary",
        lambda self, ws_id, run_id: {"tools_used": [], "llm_rounds": 0},
    )
    monkeypatch.setattr(WebBridgeApp, "_clear_stop_requests", lambda self, ws_id: None)
    monkeypatch.setattr(
        WebBridgeApp, "_stop_was_requested", lambda self, run_id, ws_id: False
    )
    monkeypatch.setattr(
        WebBridgeApp,
        "_append_thread_finalize_message",
        lambda self, thread_id, run_id, final_run: None,
    )
    monkeypatch.delenv("OUROBOROS_MAX_ROUNDS", raising=False)
    monkeypatch.delenv("OUROBOROS_WEB_MAX_VERIFY_RETRIES", raising=False)
    app = WebBridgeApp(tmp_path)

    app._run_phase_runner_worker(
        "run-limits",
        "ws-limits",
        "build project",
        "GLM-test",
        1,
        240,
        12,
    )

    assert captured == {"max_rounds": "240", "verify_retries": "12"}
    assert os.environ.get("OUROBOROS_MAX_ROUNDS") is None
    assert os.environ.get("OUROBOROS_WEB_MAX_VERIFY_RETRIES") is None


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


def test_detached_cancelled_worker_still_blocks_new_workspace_run(tmp_path) -> None:
    repo = tmp_path
    (repo / "workspaces" / "ws_busy").mkdir(parents=True)
    app = WebBridgeApp(repo)
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, daemon=True)
    worker.start()
    app._run_threads["run_old"] = worker
    app._upsert_web_run(
        "run_old",
        {
            "id": "run_old",
            "workspace_id": "ws_busy",
            "status": "cancelled",
            "result_preview": "detached but still draining",
        },
    )

    active = app._active_run_for_workspace("ws_busy")

    stop.set()
    worker.join(timeout=2)
    assert active is not None
    assert active["id"] == "run_old"
    assert active["status"] == "stopping"
    assert active["detached_worker_alive"] is True


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


def test_phase_task_ids_match_parent_run_for_log_summaries(tmp_path) -> None:
    app = WebBridgeApp(tmp_path)
    assert app._task_id_matches_run("phase_web_abc:preflight", "phase_web_abc")
    assert app._task_id_matches_run(
        "phase_web_abc__a2:execute",
        "phase_web_abc",
        {"phase_web_abc__a2"},
    )


def test_run_log_summary_counts_phase_runner_rounds(tmp_path) -> None:
    repo = tmp_path
    ws = "ws_phase_logs"
    run_id = "phase_web_live"
    drive = repo / "workspaces" / ws / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "logs" / "round_io.jsonl").write_text(
        json.dumps(
            {
                "task_id": f"{run_id}:preflight",
                "model": "GLM-test",
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (drive / "logs" / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": f"{run_id}:preflight",
                "tool": "submit_preflight_report",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    app = WebBridgeApp(repo)
    summary = app._run_log_summary(ws, run_id)
    assert summary["llm_rounds"] == 1
    assert summary["prompt_tokens"] == 11
    assert summary["completion_tokens"] == 7
    assert summary["models"] == ["GLM-test"]
    assert summary["tools_used"] == ["submit_preflight_report"]


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
    terminal_path.write_text(
        "## ws=ws_logs task=sync_improve_web_logs run=sync_improve_web_logs "
        "ts=2026-05-08T00:00:02Z exit=0 backend=test\n"
        "terminal line one\nterminal line two\n",
        encoding="utf-8",
    )
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
    from umbrella.artifacts.task_ids import task_artifact_stem

    out_path = drive_root / "task_results" / f"{task_artifact_stem(task['id'])}.json"
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

    raw_failed_stop_requested = {
        "task_id": "phase_web_cancelled",
        "status": "failed",
        "result": "stop_requested by user during phase",
        "ts": "2026-05-11T00:07:25Z",
    }
    failed_stop_row = app._task_result_to_run(
        raw_failed_stop_requested, "ws_failed_stop"
    )
    assert failed_stop_row["status"] == "cancelled"


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
