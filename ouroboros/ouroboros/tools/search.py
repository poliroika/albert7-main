"""Web search tool."""

import json

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.web_search_adapter import (
    attempt_rows as _attempt_rows,
    create_gmas_web_search_tool as _create_gmas_web_search_tool,
    fallback_answer_from_results as _fallback_answer_from_results,
    source_rows as _source_rows,
    status_from_attempts as _status_from_attempts,
    web_search_via_gmas as _web_search_via_gmas,
)


def _web_search(
    ctx: ToolContext,
    query: str,
    max_results: int = 5,
    intent: str = "",
    provider: str = "",
    fetch_content: bool = False,
) -> str:
    try:
        payload = _web_search_via_gmas(
            query,
            max_results=max_results,
            intent=str(intent or ""),
            provider=str(provider or ""),
            fetch_content=bool(fetch_content),
        )
        payload["query"] = query
        if intent:
            payload["intent"] = str(intent)
        if provider:
            payload["requested_provider"] = str(provider)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps(
            {
                "status": "provider_error",
                "provider": "gmas_web_search",
                "query": query,
                "intent": str(intent or ""),
                "requested_provider": str(provider or ""),
                "error": repr(e),
                "retryable": True,
            },
            ensure_ascii=False,
        )


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "web_search",
            {
                "name": "web_search",
                "description": (
                    "Search the public web through the GMAS WebSearchTool "
                    "provider stack. DuckDuckGo is the default provider and "
                    "does not require an API key; optional providers such as "
                    "Brave, Serper, Tavily, Exa, SearXNG, Bocha, or Google can "
                    "be selected with `provider` when configured. Returns JSON "
                    "with status, answer, sources, and provider attempts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 5},
                        "provider": {
                            "type": "string",
                            "description": (
                                "Optional GMAS search provider override such as "
                                "duckduckgo, brave, serper, tavily, exa, "
                                "searxng, bocha, or google. If omitted, GMAS "
                                "routes/falls back to DuckDuckGo."
                            ),
                        },
                        "fetch_content": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "When true, ask the GMAS tool to fetch page "
                                "content for useful results."
                            ),
                        },
                        "intent": {
                            "type": "string",
                            "description": (
                                "Optional caller metadata such as planner_research "
                                "or subtask_evidence. GMAS can use it for "
                                "provider routing."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
            _web_search,
        ),
    ]
