"""Detect task domains in a language-agnostic way.

Strategy (in order):

1. **LLM classifier** (preferred). One cheap chat call using whatever
   client Umbrella already has configured (``get_llm_client`` from
   ``code_analyzer``). Returns a small JSON verdict:
   ``{"domains": ["multi_agent_gmas"], "rationale": "..."}``. This works
   for any language the underlying model speaks (Russian, English,
   Japanese, ...).
2. **Keyword fallback** for offline / degraded / no-API-key runs. It
   matches project-specific GMAS names plus high-signal LLM/agent
   vocabulary in English and Russian, because in this repo any
   LLM-backed feature should be expressed through GMAS rather than a
   hand-rolled request loop.

The bridge caches the verdict per task in ``drive/state/active_skills.json``
so the LLM is queried at most once per task input.
"""

import json
import logging
import re
from enum import Enum
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Domain(str, Enum):
    """Identifier for a detected task domain."""

    MULTI_AGENT_GMAS = "multi_agent_gmas"


_DOMAIN_VALUES: frozenset[str] = frozenset(d.value for d in Domain)


class _ChatClient(Protocol):
    """Minimal interface ``get_llm_client`` returns (duck-typed)."""

    def chat(
        self, messages: list[dict[str, str]], model: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...


_CLASSIFIER_SYSTEM = (
    "You are a task-domain classifier for a coding agent that lives in a "
    "monorepo whose ONLY blessed framework for any code that touches an "
    "LLM is the in-repo `gmas` library (a graph-of-agents orchestration "
    "kit). Given a task description (in any human language), decide "
    "which engineering domains apply. Respond with strict JSON only, no "
    'prose. The schema is: {"domains": [<list of domain ids>], '
    '"rationale": <short string>}.\n\n'
    "Available domain ids:\n"
    '- "multi_agent_gmas": fire this domain whenever the deliverable '
    "requires implementing or running actual LLM / language-model usage, "
    "prompt orchestration, "
    "summarization, classification, generation, embeddings, RAG, agents, "
    "chatbots, autonomous workflows, planners, tool-using agents, "
    "evaluator/judge nodes, or any pipeline whose nodes call a model. "
    "In this repo, a single LLM call is still expressed as a one-node "
    '`gmas` graph -- so even seemingly trivial "call an LLM to '
    'summarize X" features count. Only skip this domain when the task '
    "has zero LLM/model involvement (pure plumbing, dependency bumps, "
    "non-AI web/data work, file creation, verification scaffolding, "
    "documentation, or labels that merely mention LLM/model/agent words). "
    "Do not fire for a task whose concrete output is just a static file, "
    "config edit, smoke marker, or report about LLMs unless the requested "
    "implementation itself calls or orchestrates a model.\n\n"
    'If no domain applies, return {"domains": [], "rationale": "..."}. '
    "Do not invent domain ids."
)


def _build_classifier_messages(task_text: str) -> list[dict[str, str]]:
    excerpt = task_text.strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[:4000] + "\n... (truncated)"
    return [
        {"role": "system", "content": _CLASSIFIER_SYSTEM},
        {"role": "user", "content": f"Task description:\n\n{excerpt}"},
    ]


def _extract_text(response: dict[str, Any]) -> str:
    """Pull the assistant text out of the heterogeneous chat() return shape.

    ``code_analyzer.SimpleLLMClient.chat`` returns either an OpenRouter
    ``message`` dict (``{"content": "..."}``) or an Anthropic top-level
    response (``{"content": [{"text": "..."}, ...]}``).
    """
    content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        if chunks:
            return "\n".join(chunks)
    if isinstance(response.get("message"), dict):
        nested = response["message"].get("content")
        if isinstance(nested, str):
            return nested
    return ""


def _iter_balanced_json_objects(text: str) -> list[str]:
    """Yield every balanced ``{...}`` substring in ``text``.

    Plain ``re`` cannot match balanced braces, so models that emit
    JSON with nested objects (``{"a": {"b": 1}}``) tripped the previous
    parser. This walks the string with a brace counter and naive string
    handling so we can still recover the JSON when it's wrapped in
    markdown fences or chatter.
    """
    out: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for idx, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    out.append(text[start : idx + 1])
                    start = -1
    return out


def _parse_classifier_json(text: str) -> set[Domain]:
    """Best-effort parse of the classifier's JSON answer.

    Tolerates models that wrap JSON in code fences or add a trailing
    comment by scanning for any balanced ``{...}`` block.
    """
    if not text.strip():
        return set()
    candidates = [text.strip(), *_iter_balanced_json_objects(text)]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw = payload.get("domains")
        if not isinstance(raw, list):
            continue
        out: set[Domain] = set()
        for value in raw:
            if isinstance(value, str) and value in _DOMAIN_VALUES:
                out.add(Domain(value))
        return out
    return set()


def classify_with_llm(
    task_text: str, *, client: _ChatClient | None = None
) -> set[Domain] | None:
    """Ask the configured LLM to classify ``task_text``.

    Returns ``None`` (not an empty set!) when no client is available or
    the call fails -- the caller uses ``None`` as a signal to fall back
    to keywords. An empty set means the model ran and explicitly found
    nothing relevant.
    """
    if not task_text.strip():
        return set()
    if client is None:
        try:
            from umbrella.control_plane.code_analyzer import get_llm_client

            client = get_llm_client()
        except Exception as exc:
            log.debug("Skill LLM classifier unavailable: %s", exc, exc_info=True)
            return None
    if client is None:
        return None
    try:
        response, _meta = client.chat(_build_classifier_messages(task_text))
    except Exception as exc:
        log.warning("Skill LLM classifier call failed: %s", exc)
        return None
    text = _extract_text(response if isinstance(response, dict) else {})
    return _parse_classifier_json(text)



_PROJECT_GMAS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgmas\b", re.IGNORECASE),
    re.compile(r"\brolegraph\b", re.IGNORECASE),
    re.compile(r"\bmacprunner\b", re.IGNORECASE),
    re.compile(r"\bagentprofile\b", re.IGNORECASE),
    re.compile(r"build_property_graph", re.IGNORECASE),
)

_AGENT_WORKFLOW_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmulti[-_\s]?agent\b", re.IGNORECASE),
    re.compile(r"\bgraph\s+of\s+(?:cooperating\s+)?agents\b", re.IGNORECASE),
    re.compile(r"\bagent(?:ic)?\s+(?:workflow|system|graph|orchestration)\b", re.IGNORECASE),
    re.compile(r"\btool[-_\s]?using\s+agents?\b", re.IGNORECASE),
    re.compile(r"\bautonomous\s+(?:agent|workflow|planner)\b", re.IGNORECASE),
    re.compile(r"мульти[-\s]?агент", re.IGNORECASE),
)

_MODEL_IMPLEMENTATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:call|invoke|query|route|orchestrate|integrate|wrap|use)\s+"
        r"(?:an?\s+)?(?:llm|language\s+model|model|openai|anthropic|claude|gpt)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:llm|language\s+model|model|openai|anthropic|claude|gpt)"
        r"[-_\s]*(?:call|pipeline|workflow|node|router|judge|evaluator|agent|"
        r"chatbot|rag|embedding|enrichment|summarization|classification|generation)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:summarize|summarise|classify|generate|embed|enrich|retrieve|rag)\b"
        r".{0,80}\b(?:with|using|via|through)\s+"
        r"(?:an?\s+)?(?:llm|language\s+model|model|openai|anthropic|claude|gpt)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:build|implement|create|wire)\b.{0,80}\b"
        r"(?:llm|language\s+model|openai|anthropic|claude|gpt)\b.{0,80}\b"
        r"(?:agent|chatbot|pipeline|workflow|summarizer|summariser|classifier|"
        r"generator|enricher|rag|embedding)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:llm|language\s+model|openai|anthropic|claude|gpt)"
        r"[-_\s]*(?:powered|backed|driven|based)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:через|с помощью|используя|на основе)\s+"
        r"(?:ллм|языков\w*\s+модел\w*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:ллм|языков\w*\s+модел\w*).{0,120}"
        r"(?:бот|агент|эконом|диплом|стратег|решени|генерац)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:бот|агент|эконом|диплом|стратег).{0,120}"
        r"(?:ллм|языков\w*\s+модел\w*)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\bprompt\s+orchestration\b", re.IGNORECASE),
)

_NEGATED_MODEL_IMPLEMENTATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:do\s+not|don't|no|without|not)\b.{0,80}\b"
        r"(?:llm|language\s+model|model|openai|anthropic|claude|gpt)\b.{0,80}\b"
        r"(?:call|pipeline|workflow|agent|chatbot|rag|embedding|enrichment|"
        r"summarization|classification|generation|implementation|implement)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:do\s+not|don't|no|without|not)\b.{0,80}\b"
        r"(?:implement|build|create|call|invoke|use|integrate|orchestrate)\b"
        r".{0,80}\b(?:llm|language\s+model|model|openai|anthropic|claude|gpt)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:без|не\s+использ\w+|не\s+нужн\w+).{0,80}"
        r"(?:ллм|языков\w*\s+модел\w*)",
        re.IGNORECASE | re.DOTALL,
    ),
)


def _looks_like_gmas_task(task_text: str) -> bool:
    if any(pattern.search(task_text) for pattern in _PROJECT_GMAS_PATTERNS):
        return True
    if any(pattern.search(task_text) for pattern in _AGENT_WORKFLOW_PATTERNS):
        return True
    if any(pattern.search(task_text) for pattern in _NEGATED_MODEL_IMPLEMENTATION_PATTERNS):
        return False
    return any(pattern.search(task_text) for pattern in _MODEL_IMPLEMENTATION_PATTERNS)


def classify_with_keywords(task_text: str) -> set[Domain]:
    """Project-aware safety-net classifier.

    Originally this matcher was deliberately *under*-tuned -- it only
    matched the literal name ``gmas`` and a handful of internal class
    names so that offline runs wouldn't pollute random workspaces with
    the GMAS skill. In practice that policy backfired: workspaces like
    ``news_cards_ai`` describe an obvious LLM pipeline (in Russian, no
    less) without ever saying the word "gmas", so detection silently
    returned an empty set and the agent fell back to ad-hoc
    ``requests`` / FastAPI code instead of using the in-repo framework.
    The fallback now recognises project-specific GMAS names, high-signal
    multi-agent workflow language, and model implementation phrases such
    as "call an LLM" or "LLM enrichment pipeline". It deliberately ignores
    meta labels like "LLM smoke verification" when the concrete deliverable
    is just a static file/config/doc.
    """
    if not task_text:
        return set()
    if _looks_like_gmas_task(task_text):
        return {Domain.MULTI_AGENT_GMAS}
    return set()


def detect_task_domains(*texts: str, client: _ChatClient | None = None) -> set[Domain]:
    """Top-level domain detection with LLM-first, keyword-fallback strategy.

    All ``texts`` are concatenated before classification (so a CLI
    ``--task`` arg + ``TASK_MAIN.md`` are inspected together). Returns the
    union of LLM and keyword verdicts when the LLM is available -- that
    way an LLM miss on a task that *does* literally name ``gmas`` is
    still caught by the keyword pass.
    """
    haystack = "\n\n".join(t for t in texts if t and t.strip())
    if not haystack:
        return set()

    keyword_hits = classify_with_keywords(haystack)
    llm_hits = classify_with_llm(haystack, client=client)

    if llm_hits is None:
        return keyword_hits
    detected = keyword_hits | llm_hits
    if (
        Domain.MULTI_AGENT_GMAS in detected
        and not _looks_like_gmas_task(haystack)
    ):
        detected.discard(Domain.MULTI_AGENT_GMAS)
    return detected


def summarize_domains(domains: set[Domain]) -> str:
    """Render a short human-readable summary for the drive banner."""
    if not domains:
        return "No active skills detected for this task."
    labels = {
        Domain.MULTI_AGENT_GMAS: (
            "multi_agent_gmas — task requires a multi-agent system; build "
            "on the in-repo gmas library and use get_gmas_context / "
            "search_gmas_knowledge with a concrete query before implementing "
            "GMAS/LLM agent code."
        ),
    }
    lines = [labels.get(d, d.value) for d in sorted(domains, key=lambda x: x.value)]
    return "Detected skills:\n- " + "\n- ".join(lines)
