"""Unit tests for ``umbrella.skills.domain_detection``."""

from typing import Any

import pytest

from umbrella.skills.domain_detection import (
    Domain,
    _parse_classifier_json,
    classify_with_keywords,
    classify_with_llm,
    detect_task_domains,
    summarize_domains,
)


class _StubClient:
    """Imitates ``code_analyzer.SimpleLLMClient.chat``."""

    def __init__(self, payload_text: str, *, raise_on_call: bool = False) -> None:
        self.payload_text = payload_text
        self.raise_on_call = raise_on_call
        self.calls: list[list[dict[str, str]]] = []

    def chat(
        self, messages: list[dict[str, str]], model: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls.append(messages)
        if self.raise_on_call:
            raise RuntimeError("boom")
        return {"content": self.payload_text}, {}


def test_keyword_fallback_matches_project_specific_tokens() -> None:
    assert classify_with_keywords("Use gmas for the new graph") == {
        Domain.MULTI_AGENT_GMAS
    }
    assert classify_with_keywords("Wire RoleGraph into the runner") == {
        Domain.MULTI_AGENT_GMAS
    }
    assert classify_with_keywords("MACPRunner orchestrates everything") == {
        Domain.MULTI_AGENT_GMAS
    }


def test_keyword_fallback_matches_llm_implementation_phrases() -> None:
    """Offline fallback fires for real model/agent work, not meta labels."""
    assert classify_with_keywords("Build a multi-agent system") == {
        Domain.MULTI_AGENT_GMAS
    }
    assert classify_with_keywords("Сделай мультиагентную систему") == {
        Domain.MULTI_AGENT_GMAS
    }
    assert classify_with_keywords("LLM enrichment for each news card") == {
        Domain.MULTI_AGENT_GMAS
    }
    assert classify_with_keywords(
        "Build an LLM-powered civilization game with economy and diplomacy bots"
    ) == {Domain.MULTI_AGENT_GMAS}
    assert classify_with_keywords("Call an LLM to summarize incoming tickets") == {
        Domain.MULTI_AGENT_GMAS
    }
    assert classify_with_keywords(
        "Сделай игру, где экономика и дипломатия ботов строится через ллм"
    ) == {Domain.MULTI_AGENT_GMAS}


def test_keyword_fallback_ignores_pure_non_ai_phrases() -> None:
    """Bumps, schema work and similar non-AI tasks still match nothing."""
    assert classify_with_keywords("Build a database schema for users") == set()
    assert (
        classify_with_keywords("Bump httpx from 0.27 to 0.28 and fix imports") == set()
    )
    assert (
        classify_with_keywords(
            "LLM smoke verification: create smoke_result.txt with static text"
        )
        == set()
    )
    assert classify_with_keywords("") == set()


def test_parse_classifier_json_handles_fenced_response() -> None:
    response = (
        "Here is the verdict:\n"
        "```json\n"
        '{"domains": ["multi_agent_gmas"], "rationale": "graph of agents"}\n'
        "```"
    )
    assert _parse_classifier_json(response) == {Domain.MULTI_AGENT_GMAS}


def test_parse_classifier_json_filters_unknown_domain_ids() -> None:
    response = '{"domains": ["multi_agent_gmas", "made_up_skill"], "rationale": "x"}'
    assert _parse_classifier_json(response) == {Domain.MULTI_AGENT_GMAS}


def test_parse_classifier_json_returns_empty_on_garbage() -> None:
    assert _parse_classifier_json("not json at all") == set()
    assert _parse_classifier_json("") == set()


def test_classify_with_llm_uses_provided_client() -> None:
    client = _StubClient('{"domains": ["multi_agent_gmas"], "rationale": "x"}')
    result = classify_with_llm(
        "いくつかのエージェントを持つグラフを作って", client=client
    )
    assert result == {Domain.MULTI_AGENT_GMAS}
    assert len(client.calls) == 1
    assert "いくつかのエージェント" in client.calls[0][-1]["content"]


def test_classify_with_llm_returns_none_when_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client", lambda: None
    )
    assert classify_with_llm("Build a multi-agent system") is None


def test_classify_with_llm_returns_none_on_call_failure() -> None:
    client = _StubClient("", raise_on_call=True)
    assert classify_with_llm("anything", client=client) is None


def test_classify_with_llm_returns_empty_set_for_blank_input() -> None:
    assert classify_with_llm("   ", client=_StubClient("{}")) == set()


def test_detect_task_domains_unions_llm_and_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM may miss a literal `gmas` mention -- keyword pass should still catch it."""
    client = _StubClient('{"domains": [], "rationale": "looks like a CRUD task"}')
    result = detect_task_domains("Use gmas for the agent graph", client=client)
    assert result == {Domain.MULTI_AGENT_GMAS}


def test_detect_task_domains_suppresses_llm_false_positive_for_meta_label() -> None:
    client = _StubClient('{"domains": ["multi_agent_gmas"], "rationale": "mentions LLM"}')
    result = detect_task_domains(
        "LLM smoke verification: create smoke_result.txt with static text only",
        client=client,
    )
    assert result == set()


def test_detect_task_domains_falls_back_to_keywords_when_llm_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "umbrella.control_plane.code_analyzer.get_llm_client", lambda: None
    )
    # Pure plumbing -- no AI vocabulary at all, fallback fires nothing.
    assert detect_task_domains("Bump dependency httpx from 0.27 to 0.28") == set()
    # Project-specific names always fire.
    assert detect_task_domains("Wire RoleGraph and MACPRunner") == {
        Domain.MULTI_AGENT_GMAS
    }
    # Concrete LLM implementation vocabulary fires too (this is the
    # news_cards_ai case that previously fell through and made the agent
    # default to raw ``requests`` / FastAPI).
    assert detect_task_domains("LLM enrichment for each news card") == {
        Domain.MULTI_AGENT_GMAS
    }


def test_detect_task_domains_empty_returns_empty() -> None:
    assert detect_task_domains("", "  ", client=_StubClient("{}")) == set()


def test_summarize_domains_renders_banner() -> None:
    assert "No active skills" in summarize_domains(set())
    summary = summarize_domains({Domain.MULTI_AGENT_GMAS})
    assert "gmas_active_context.md" in summary
    assert "multi_agent_gmas" in summary
