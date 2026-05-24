from types import SimpleNamespace

from umbrella.deep_agent_tools.workspace_gmas import (
    _gmas_context_query_specificity_issue,
)


def test_gmas_context_query_rejects_placeholder_for_active_agent_subtask() -> None:
    active = {
        "id": "gmas-bot-agents",
        "goal": "Implement GMAS-backed economy and diplomacy bots.",
        "files_to_create": ["src/demo/bots/economy_agent.py"],
    }

    issue = _gmas_context_query_specificity_issue("GMAS context please", active)
    assert issue


def test_gmas_context_query_accepts_symbol_or_specific_terms() -> None:
    active = {
        "id": "gmas-bot-agents",
        "goal": "Implement GMAS-backed economy and diplomacy bots.",
        "files_to_create": ["src/demo/bots/economy_agent.py"],
    }

    assert not _gmas_context_query_specificity_issue(
        "GraphBuilder LLMConfig economy diplomacy bot wiring",
        active,
    )
    assert not _gmas_context_query_specificity_issue(
        "economy diplomacy negotiation graph orchestration",
        active,
    )


def test_gmas_context_query_rejects_generic_workspace_research_query() -> None:
    ctx = SimpleNamespace(
        context_overlays={
            "gmas_prewrite_required": True,
            "detected_domains": ["multi_agent_gmas"],
        }
    )

    issue = _gmas_context_query_specificity_issue(
        "multi-agent game AI bot strategy turn-based",
        None,
        ctx=ctx,
    )

    assert issue


def test_gmas_context_query_accepts_concrete_workspace_research_query() -> None:
    ctx = SimpleNamespace(context_overlays={"gmas_prewrite_required": True})

    assert not _gmas_context_query_specificity_issue(
        "AgentProfile MACPRunner tools LLMCallerFactory",
        None,
        ctx=ctx,
    )
