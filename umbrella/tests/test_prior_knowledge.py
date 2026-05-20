"""Tests for ``build_prior_knowledge_section`` (P1-2).

The helper has to:

* survive missing palace / retrieval gracefully (degraded run);
* render recent palace entries with the room tag;
* truncate over-long output;
* be invoked by ``render_workspace_prompt`` so the rendered prompt
  contains a ``{prior_knowledge}`` slot that is not the literal
  template marker.
"""

from pathlib import Path
from typing import Any

import pytest

from umbrella.orchestration.ouroboros_task import (
    build_prior_knowledge_section,
    render_workspace_prompt,
)


class _FakePalace:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def recent(self, *, workspace_id: str, limit: int = 8) -> list[dict[str, Any]]:
        return self._entries[:limit]


def _patch_palace(
    monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]
) -> None:
    fake = _FakePalace(entries)
    monkeypatch.setattr(
        "umbrella.memory.palace_backend.get_palace_backend",
        lambda *_a, **_kw: fake,
        raising=True,
    )
    monkeypatch.setattr(
        "umbrella.memory.paths.palace_path_for",
        lambda *_a, **_kw: Path("/tmp/fake-palace"),
        raising=True,
    )


def _stub_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force UmbrellaServices.retrieval = None so the retrieval branch is a no-op."""

    class _StubServices:
        def __init__(self, *_a, **_kw) -> None:
            self.retrieval = None

    monkeypatch.setattr(
        "umbrella.integration.services.UmbrellaServices",
        _StubServices,
        raising=True,
    )


class TestBuildPriorKnowledgeSection:
    def test_empty_palace_returns_cold_start_discovery_banner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cold-start workspace MUST get a discovery-required banner.

        Umbrella is the control plane: when there is no palace memory, no
        detected skills, and no retrieval hits, Umbrella's job is to tell
        the agent to research first. The Ouroboros planner-discovery
        gate enforces the same invariant at tool-call time; this banner
        is the prompt-level half of that contract.
        """
        _patch_palace(monkeypatch, [])
        _stub_services(monkeypatch)
        text = build_prior_knowledge_section(tmp_path, "wid")
        assert "[EMPTY PRIOR KNOWLEDGE" in text
        assert "EXTERNAL DISCOVERY REQUIRED" in text
        assert "deep_search" in text
        assert "github_project_search" in text
        assert "mcp_discover" in text
        assert "web_fetch" in text
        assert "propose_task_plan" in text
        assert "record_idea" in text
        assert "save_umbrella_lesson" in text

    def test_palace_entries_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(
            monkeypatch,
            [
                {"room": "lessons", "content": "Always ship a /api/health endpoint."},
                {"room": "decisions", "content": "Use httpx for outbound HTTP."},
                {"room": "lessons", "content": ""},  # filtered out
            ],
        )
        _stub_services(monkeypatch)
        text = build_prior_knowledge_section(tmp_path, "wid")
        assert "[lessons] Always ship a /api/health endpoint." in text
        assert "[decisions] Use httpx for outbound HTTP." in text
        # empty entry stripped
        assert text.count("- [") == 2

    def test_truncation_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        big = [{"room": "lessons", "content": "x" * 600} for _ in range(8)]
        _patch_palace(monkeypatch, big)
        _stub_services(monkeypatch)
        text = build_prior_knowledge_section(tmp_path, "wid", max_chars=1000)
        assert "[prior knowledge truncated]" in text
        assert len(text) <= 1100  # max_chars + truncation marker tail

    def test_palace_failure_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a, **_kw):
            raise RuntimeError("palace unavailable")

        monkeypatch.setattr(
            "umbrella.memory.palace_backend.get_palace_backend",
            _boom,
            raising=True,
        )
        monkeypatch.setattr(
            "umbrella.memory.paths.palace_path_for",
            lambda *_a, **_kw: Path("/tmp/x"),
            raising=True,
        )
        _stub_services(monkeypatch)
        text = build_prior_knowledge_section(tmp_path, "wid")
        # No palace + no retrieval → "no prior memory" fallback.
        assert isinstance(text, str)
        assert text.strip() != ""


class TestRenderWorkspacePrompt:
    def test_template_injects_prior_knowledge_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(
            monkeypatch,
            [{"room": "lessons", "content": "Auto-injected fact #42."}],
        )
        _stub_services(monkeypatch)
        # Skip the environment snapshot (it pulls a lot of repo state).
        rendered = render_workspace_prompt(
            repo_root=tmp_path,
            workspace_id="wid",
            task_text="Build a thing.",
            quality_threshold=0.8,
            include_environment_snapshot=False,
            include_prior_knowledge=True,
        )
        assert "## Prior Knowledge" in rendered
        assert "get_umbrella_memory" in rendered
        assert "{prior_knowledge}" not in rendered
        assert "Auto-injected fact #42." in rendered

    def test_can_disable_prior_knowledge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(monkeypatch, [{"room": "lessons", "content": "__PRIOR_SECRET__"}])
        _stub_services(monkeypatch)
        rendered = render_workspace_prompt(
            repo_root=tmp_path,
            workspace_id="wid",
            task_text="t",
            quality_threshold=0.8,
            include_environment_snapshot=False,
            include_prior_knowledge=False,
        )
        assert "__PRIOR_SECRET__" not in rendered
        assert "{prior_knowledge}" not in rendered  # still substituted (with "")


def _seed_skill_state(
    repo_root: Path,
    workspace_id: str,
    *,
    domains: list[str],
    gmas_context: str = "",
) -> None:
    drive = repo_root / "workspaces" / workspace_id / ".memory" / "drive"
    (drive / "state").mkdir(parents=True, exist_ok=True)
    (drive / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    payload = {
        "entry": {
            "text_hash": "abc",
            "workspace_id": workspace_id,
            "domains": list(domains),
            "computed_at": "2026-04-18T00:00:00+00:00",
        }
    }
    import json as _json

    (drive / "state" / "active_skills.json").write_text(
        _json.dumps(payload), encoding="utf-8"
    )
    if gmas_context:
        (drive / "memory" / "knowledge" / "gmas_active_context.md").write_text(
            gmas_context, encoding="utf-8"
        )


class TestSkillArtifactInjection:
    """F1: Detected skills + gmas_active_context.md must reach the prompt."""

    def test_detected_skills_banner_appears_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(monkeypatch, [])
        _stub_services(monkeypatch)
        _seed_skill_state(
            tmp_path,
            "wid",
            domains=["multi_agent_gmas"],
            gmas_context="# GMAS context\nUse RoleGraph + MACPRunner.\n",
        )

        text = build_prior_knowledge_section(tmp_path, "wid")

        assert "### Detected skills" in text
        assert "multi_agent_gmas" in text
        assert "GMAS active context" in text
        assert "RoleGraph + MACPRunner" in text
        assert "MUST call" in text and "get_gmas_context" in text
        assert "Before your first workspace write" in text
        assert "apply_workspace_patch" in text
        assert "execute-time retrieval" in text

    def test_no_skills_no_banner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(monkeypatch, [{"room": "lessons", "content": "boring"}])
        _stub_services(monkeypatch)
        text = build_prior_knowledge_section(tmp_path, "wid")
        assert "Detected skills" not in text
        assert "### Recent Umbrella memory" in text
        assert "boring" in text

    def test_gmas_context_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(monkeypatch, [])
        _stub_services(monkeypatch)
        _seed_skill_state(
            tmp_path,
            "wid",
            domains=["multi_agent_gmas"],
            gmas_context="X" * 20000,
        )
        text = build_prior_knowledge_section(
            tmp_path, "wid", max_chars=50000, token_budget=2000
        )
        assert "[...truncated" in text


class TestNonGmasMemoryFilter:
    """memory-filter: non-GMAS palace entries get demoted under multi_agent_gmas."""

    def test_fastapi_memory_demoted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(
            monkeypatch,
            [
                {
                    "room": "wing_news/observation",
                    "content": "Built web_server.py with FastAPI and httpx for fetching news.",
                },
                {
                    "room": "wing_news/observation",
                    "content": "Wired RoleGraph with two AgentProfile nodes via build_property_graph.",
                },
            ],
        )
        _stub_services(monkeypatch)
        _seed_skill_state(
            tmp_path,
            "wid",
            domains=["multi_agent_gmas"],
            gmas_context="# Stub gmas context\n",
        )
        text = build_prior_knowledge_section(tmp_path, "wid")

        assert "Previous attempts (review CRITICALLY" in text
        assert "FastAPI" in text and "httpx" in text
        assert "RoleGraph" in text
        # The GMAS-flavoured memory must NOT be in the demoted block.
        critical_block = text.split("### Previous attempts (review CRITICALLY")[1]
        assert "RoleGraph" not in critical_block

    def test_no_filter_without_gmas_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(
            monkeypatch,
            [
                {
                    "room": "wing/observation",
                    "content": "Built FastAPI service successfully.",
                }
            ],
        )
        _stub_services(monkeypatch)
        text = build_prior_knowledge_section(tmp_path, "wid")
        assert "Previous attempts" not in text
        assert "FastAPI" in text

    def test_low_signal_backup_chatter_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_palace(
            monkeypatch,
            [
                {
                    "room": "change",
                    "content": "Updated web_server.py\n\nBackup: C:/tmp/seed_backup_123",
                },
                {
                    "room": "observation",
                    "content": "Need GMAS runner wiring before writing code.",
                },
            ],
        )
        _stub_services(monkeypatch)
        _seed_skill_state(
            tmp_path,
            "wid",
            domains=["multi_agent_gmas"],
            gmas_context="# GMAS context\nUse RoleGraph + MACPRunner.\n",
        )

        text = build_prior_knowledge_section(tmp_path, "wid")

        assert "Updated web_server.py" not in text
        assert "seed_backup_123" not in text
        assert "Need GMAS runner wiring" in text
