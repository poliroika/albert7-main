"""Integration tests for the skill layer wired through ``ouroboros_bridge``."""

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from umbrella.integration.ouroboros_bridge import sync_umbrella_context_to_drive


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _seed_repo(repo_root: Path, *, task_text: str, opt_in_gmas: bool = False) -> None:
    """Build the minimum repo layout the bridge expects for a JKX-style run.

    ``opt_in_gmas=True`` still forces ``multi_agent_gmas`` on, but the
    normal detector now also activates it for LLM/model/agent tasks.
    """
    (repo_root / "ouroboros").mkdir(parents=True, exist_ok=True)
    _write(repo_root / "workspaces" / "JKX" / "TASK_MAIN.md", task_text)
    _write(repo_root / "gmas" / "README.md", "# gmas\nMulti-agent system framework.\n")
    if opt_in_gmas:
        _write(
            repo_root / "workspaces" / "JKX" / "workspace.toml",
            "[skills]\nmulti_agent_gmas = true\n",
        )


def _stub_gmas_payload(query: str) -> dict[str, Any]:
    return {
        "query": query,
        "recommended_pattern": "build_property_graph + MACPRunner",
        "confidence": 0.87,
        "key_symbols": ["AgentProfile", "build_property_graph"],
        "key_files": ["gmas/examples/basic_usage.py"],
        "results": [
            {
                "title": "basic_usage.py",
                "path": "gmas/examples/basic_usage.py",
                "score": 1.0,
                "content": "from gmas.core import AgentProfile\n# ... example body ...\n",
                "content_truncated": False,
            },
        ],
    }


def test_bridge_writes_gmas_artifact_when_llm_detects_multi_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client",
        lambda: _make_llm_stub(["multi_agent_gmas"]),
    )
    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        lambda repo, query, **kw: _stub_gmas_payload(query),
    )

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _seed_repo(
            repo_root,
            task_text=(
                "Build a system that processes citizen complaints end-to-end "
                "using a graph of cooperating agents."
            ),
        )
        drive_root = repo_root / ".umbrella" / "ouroboros_drive"

        sync_umbrella_context_to_drive(
            repo_root,
            drive_root,
            workspace_id="JKX",
            task_input="Build the citizen-complaints pipeline.",
            task_id="task_jkx_1",
        )

        knowledge_dir = drive_root / "memory" / "knowledge"
        artifact = (knowledge_dir / "gmas_active_context.md").read_text(
            encoding="utf-8"
        )
        banner = (knowledge_dir / "active_skills.md").read_text(encoding="utf-8")
        cache = json.loads(
            (drive_root / "state" / "active_skills.json").read_text(encoding="utf-8")
        )

        assert "GMAS Active Context" in artifact
        assert "basic_usage.py" in artifact
        assert "from gmas.core import AgentProfile" in artifact
        assert "multi_agent_gmas" in banner
        assert "memory/knowledge/gmas_active_context.md" in banner
        assert cache["entry"]["domains"] == ["multi_agent_gmas"]
        assert cache["entry"]["workspace_id"] == "JKX"
        workspace_toml = (
            repo_root / "workspaces" / "JKX" / "workspace.toml"
        ).read_text(encoding="utf-8")
        assert "[skills]" in workspace_toml
        assert "multi_agent_gmas = true" in workspace_toml
        assert "[skill_decisions.multi_agent_gmas]" in workspace_toml
        assert 'detected_by = "umbrella.skill_detector"' in workspace_toml


def test_bridge_current_llm_task_overrides_stale_gmas_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current task wins over a stale workspace opt-out.

    Workspaces can accumulate old ``multi_agent_gmas = false`` settings
    from earlier attempts. A new task that explicitly asks for LLM/model
    behavior should not silently degrade into rule-based code because of
    that stale setting.
    """
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client",
        lambda: _make_llm_stub(["multi_agent_gmas"]),
    )
    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        lambda repo, query, **kw: _stub_gmas_payload(query),
    )

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _seed_repo(
            repo_root,
            task_text=(
                "Build a system that processes citizen complaints end-to-end "
                "using a graph of cooperating agents."
            ),
            opt_in_gmas=False,
        )
        _write(
            repo_root / "workspaces" / "JKX" / "workspace.toml",
            "[skills]\nmulti_agent_gmas = false\n",
        )
        drive_root = repo_root / ".umbrella" / "ouroboros_drive"

        sync_umbrella_context_to_drive(
            repo_root,
            drive_root,
            workspace_id="JKX",
            task_input="Build the citizen-complaints pipeline.",
            task_id="task_jkx_no_optin",
        )

        knowledge_dir = drive_root / "memory" / "knowledge"
        assert (knowledge_dir / "gmas_active_context.md").exists()
        cache = json.loads(
            (drive_root / "state" / "active_skills.json").read_text(encoding="utf-8")
        )
        assert cache["entry"]["domains"] == ["multi_agent_gmas"]
        workspace_toml = (
            repo_root / "workspaces" / "JKX" / "workspace.toml"
        ).read_text(encoding="utf-8")
        assert "multi_agent_gmas = true" in workspace_toml
        assert "[skill_decisions.multi_agent_gmas]" in workspace_toml
        assert "overriding a stale workspace opt-out" in workspace_toml


def test_bridge_skips_skill_when_neither_llm_nor_keywords_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client",
        lambda: _make_llm_stub([]),
    )
    fetch_calls: list[str] = []
    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        lambda repo, query, **kw: (
            fetch_calls.append(query) or _stub_gmas_payload(query)
        ),
    )

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        # Pure plumbing task -- no LLM, no agent, no model vocabulary.
        # The broadened keyword fallback only fires on AI-related
        # phrases, so this must still produce no skill artifacts.
        _seed_repo(
            repo_root,
            task_text=(
                "Bump httpx from 0.27 to 0.28 and adjust the connection "
                "pool size in the database driver."
            ),
        )
        drive_root = repo_root / ".umbrella" / "ouroboros_drive"

        sync_umbrella_context_to_drive(
            repo_root,
            drive_root,
            workspace_id="JKX",
            task_input="Bump httpx 0.27 -> 0.28 and tune pool size.",
            task_id="task_crud_1",
        )

        knowledge_dir = drive_root / "memory" / "knowledge"
        assert not (knowledge_dir / "gmas_active_context.md").exists()
        assert not (knowledge_dir / "active_skills.md").exists()
        assert fetch_calls == []


def test_bridge_uses_keyword_fallback_when_llm_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client", lambda: None
    )
    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        lambda repo, query, **kw: _stub_gmas_payload(query),
    )

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _seed_repo(
            repo_root,
            task_text="Wire RoleGraph and MACPRunner into the workspace.",
        )
        drive_root = repo_root / ".umbrella" / "ouroboros_drive"

        sync_umbrella_context_to_drive(
            repo_root,
            drive_root,
            workspace_id="JKX",
            task_input=None,
            task_id="task_jkx_kw",
        )

        knowledge_dir = drive_root / "memory" / "knowledge"
        assert (knowledge_dir / "gmas_active_context.md").exists()
        banner = (knowledge_dir / "active_skills.md").read_text(encoding="utf-8")
        assert "multi_agent_gmas" in banner
        workspace_toml = (
            repo_root / "workspaces" / "JKX" / "workspace.toml"
        ).read_text(encoding="utf-8")
        assert "multi_agent_gmas = true" in workspace_toml


def test_bridge_does_not_force_gmas_from_workspace_imports_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``import gmas`` in workspace code is not enough on its own:
    activation follows task intent, not legacy imports lying around."""
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client",
        lambda: _make_llm_stub([]),
    )
    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        lambda repo, query, **kw: _stub_gmas_payload(query),
    )

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _seed_repo(repo_root, task_text="Bump httpx and tune connection pooling.")
        _write(
            repo_root / "workspaces" / "JKX" / "pipeline.py",
            "from gmas.core import AgentProfile\n",
        )
        drive_root = repo_root / ".umbrella" / "ouroboros_drive"

        sync_umbrella_context_to_drive(
            repo_root,
            drive_root,
            workspace_id="JKX",
            task_input="Bump httpx and tune connection pooling.",
            task_id="task_import_signal",
        )

        knowledge_dir = drive_root / "memory" / "knowledge"
        assert not (knowledge_dir / "gmas_active_context.md").exists()


def test_bridge_does_not_auto_gmas_for_llm_meta_smoke_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client",
        lambda: _make_llm_stub(["multi_agent_gmas"]),
    )
    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        lambda repo, query, **kw: _stub_gmas_payload(query),
    )

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _seed_repo(
            repo_root,
            task_text=(
                "LLM smoke verification: create smoke_result.txt with "
                "static text only. Do not implement an LLM pipeline."
            ),
        )
        drive_root = repo_root / ".umbrella" / "ouroboros_drive"

        sync_umbrella_context_to_drive(
            repo_root,
            drive_root,
            workspace_id="JKX",
            task_input="Create the smoke marker file.",
            task_id="task_llm_meta_smoke",
        )

        knowledge_dir = drive_root / "memory" / "knowledge"
        assert not (knowledge_dir / "gmas_active_context.md").exists()
        workspace_toml = repo_root / "workspaces" / "JKX" / "workspace.toml"
        assert not workspace_toml.exists()


def test_bridge_caches_verdict_and_does_not_recompute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second sync with same task text should not re-invoke build_gmas_context."""
    fetch_calls: list[str] = []
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client",
        lambda: _make_llm_stub(["multi_agent_gmas"]),
    )
    monkeypatch.setattr(
        "umbrella.retrieval.gmas_context.build_gmas_context",
        lambda repo, query, **kw: (
            fetch_calls.append(query) or _stub_gmas_payload(query)
        ),
    )

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _seed_repo(
            repo_root, task_text="Build a multi-agent dispatcher", opt_in_gmas=True
        )
        drive_root = repo_root / ".umbrella" / "ouroboros_drive"

        for _ in range(2):
            sync_umbrella_context_to_drive(
                repo_root,
                drive_root,
                workspace_id="JKX",
                task_input="Build a multi-agent dispatcher",
                task_id="task_cache_1",
            )

        assert len(fetch_calls) == 1, fetch_calls


def _make_llm_stub(domains: list[str]):
    """Factory: returns a chat-stub object the bridge can call."""
    payload = json.dumps({"domains": domains, "rationale": "stub"})

    class _Stub:
        def chat(self, messages, model=None):
            return {"content": payload}, {}

    return _Stub()
