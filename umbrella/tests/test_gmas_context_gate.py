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
