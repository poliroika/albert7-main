"""Web search tool."""

import json
import logging
import os
import re
import html
import urllib.parse
import urllib.request
from typing import Any, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)


def _extract_openai_response_text(resp_dump: dict[str, Any]) -> str:
    text = ""
    for item in resp_dump.get("output", []) or []:
        if item.get("type") == "message":
            for block in item.get("content", []) or []:
                if block.get("type") in ("output_text", "text"):
                    text += block.get("text", "")
    return text.strip()


def _web_search_via_openai(query: str, max_results: int = 5) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    resp = client.responses.create(
        model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
        tools=[{"type": "web_search"}],
        tool_choice="auto",
        input=query,
    )
    answer = _extract_openai_response_text(resp.model_dump())
    return {
        "provider": "openai_responses_web_search",
        "answer": answer or "(no answer)",
        "sources": [],
        "max_results": max_results,
    }


def _fallback_answer_from_results(results: list[dict[str, str]]) -> str:
    lines = []
    for idx, item in enumerate(results[:5], start=1):
        title = (item.get("title") or "").strip() or f"Result {idx}"
        url = (item.get("url") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        parts = [f"{idx}. {title}"]
        if snippet:
            parts.append(snippet)
        if url:
            parts.append(url)
        lines.append("\n".join(parts))
    return "\n\n".join(lines) if lines else "(no answer)"


def _summarize_results_with_llm(query: str, results: list[dict[str, str]]) -> str:
    from ouroboros.llm import DEFAULT_LIGHT_MODEL, LLMClient

    if not results:
        return "(no results)"

    model = (
        os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "").strip()
        or os.environ.get("OUROBOROS_MODEL_LIGHT", "").strip()
        or os.environ.get("OUROBOROS_MODEL", "").strip()
        or DEFAULT_LIGHT_MODEL
    )

    rendered_results = []
    for idx, item in enumerate(results[:5], start=1):
        rendered_results.append(
            "\n".join(
                [
                    f"[{idx}] {item.get('title', '').strip()}",
                    f"URL: {item.get('url', '').strip()}",
                    f"Snippet: {item.get('snippet', '').strip()}",
                ]
            )
        )

    prompt = (
        "You are summarizing web search results for Ouroboros.\n"
        "Answer the query using only the evidence below.\n"
        "Be concise, factual, and mention uncertainty when results conflict or are incomplete.\n"
        "End with a short Sources section that cites result numbers like [1], [2].\n\n"
        f"Query: {query}\n\n"
        "Search results:\n"
        f"{chr(10).join(rendered_results)}"
    )

    llm = LLMClient()
    response, _usage = llm.chat(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        max_tokens=1200,
    )
    content = str(response.get("content") or "").strip()
    return content or _fallback_answer_from_results(results)


def _duckduckgo_search_results(
    query: str, max_results: int = 5
) -> list[dict[str, str]]:
    search_url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://html.duckduckgo.com/",
    }
    body = urllib.parse.urlencode({"q": query}).encode("utf-8")
    request = urllib.request.Request(
        search_url, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        html_body = response.read().decode("utf-8", errors="replace")

    title_pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    snippets = snippet_pattern.findall(html_body)
    results: list[dict[str, str]] = []
    for idx, (raw_href, raw_title) in enumerate(title_pattern.findall(html_body)):
        if len(results) >= max_results:
            break
        match = re.search(r"uddg=([^&]+)", raw_href)
        url = urllib.parse.unquote(match.group(1)) if match else raw_href
        if not str(url).startswith("http"):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", raw_title).strip())
        snippet = ""
        if idx < len(snippets):
            snippet = html.unescape(re.sub(r"<[^>]+>", "", snippets[idx]).strip())
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _web_search_via_duckduckgo(query: str, max_results: int = 5) -> dict[str, Any]:
    limit = max(1, min(int(max_results), 25))
    results = _duckduckgo_search_results(query, max_results=limit)
    sources = [
        {
            "title": (item.get("title") or "").strip(),
            "url": (item.get("url") or "").strip(),
            "snippet": (item.get("snippet") or "").strip(),
        }
        for item in results
        if item.get("url")
    ]
    answer = _fallback_answer_from_results(sources)
    try:
        answer = _summarize_results_with_llm(query, sources)
    except Exception:
        log.warning(
            "Failed to summarize DuckDuckGo search results with LLM", exc_info=True
        )
    return {
        "provider": "duckduckgo_plus_llm_summary",
        "answer": answer,
        "sources": sources,
        "max_results": limit,
    }


def _web_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    try:
        payload = (
            _web_search_via_openai(query, max_results=max_results)
            if os.environ.get("OPENAI_API_KEY", "").strip()
            else _web_search_via_duckduckgo(query, max_results=max_results)
        )
        payload["query"] = query
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": repr(e)}, ensure_ascii=False)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "web_search",
            {
                "name": "web_search",
                "description": (
                    "Search the public web. Uses OpenAI Responses web_search when "
                    "configured, otherwise DuckDuckGo plus LLM summarization. "
                    "Returns JSON with answer + sources. Pair with `web_fetch` to "
                    "read a specific result page in detail."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            _web_search,
        ),
    ]
