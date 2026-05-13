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
    '- "multi_agent_gmas": fire this domain whenever the task involves '
    "**any** LLM / language-model usage, prompt orchestration, "
    "summarization, classification, generation, embeddings, RAG, agents, "
    "chatbots, autonomous workflows, planners, tool-using agents, "
    "evaluator/judge nodes, or any pipeline whose nodes call a model. "
    "In this repo, a single LLM call is still expressed as a one-node "
    '`gmas` graph -- so even seemingly trivial "call an LLM to '
    'summarize X" features count. Only skip this domain when the task '
    "has zero LLM/model involvement (pure plumbing, dependency bumps, "
    "non-AI web/data work, etc.). When in doubt and the task mentions "
    "an LLM, model, prompt, summarize, classify, generate, agent, or "
    "completion in any language, fire it.\n\n"
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


_KEYWORD_PATTERNS: dict[Domain, tuple[re.Pattern[str], ...]] = {
    Domain.MULTI_AGENT_GMAS: (
        # Project-specific names (kept verbatim from the original
        # narrow fallback so existing signals still fire).
        re.compile(r"\bgmas\b", re.IGNORECASE),
        re.compile(r"\brolegraph\b", re.IGNORECASE),
        re.compile(r"\bmacprunner\b", re.IGNORECASE),
        re.compile(r"\bagentprofile\b", re.IGNORECASE),
        re.compile(r"build_property_graph", re.IGNORECASE),
        # Generic LLM / agent signals. In this repo every LLM-touching
        # feature is expressed as a gmas graph (often single-node), so
        # any of these keywords means "use gmas, don't roll your own
        # request loop". The list intentionally covers EN + RU because
        # workspace TASK_MAIN files are routinely written in Russian.
        re.compile(r"\bllm[s]?\b", re.IGNORECASE),
        re.compile(r"\bllm[-_]?(?:api|model|key|base[-_]?url)\b", re.IGNORECASE),
        re.compile(r"\bai\b", re.IGNORECASE),
        re.compile(r"\blanguage\s+model\b", re.IGNORECASE),
        re.compile(r"\bgpt[-_ ]?\d", re.IGNORECASE),
        re.compile(r"\bopenai\b", re.IGNORECASE),
        re.compile(r"\banthropic\b", re.IGNORECASE),
        re.compile(r"\bopenrouter\b", re.IGNORECASE),
        re.compile(r"\bclaude\b", re.IGNORECASE),
        re.compile(r"\bprompt(?:ing|s)?\b", re.IGNORECASE),
        re.compile(r"\bcompletion(?:s)?\b", re.IGNORECASE),
        re.compile(r"\bembedding(?:s)?\b", re.IGNORECASE),
        re.compile(r"\bgenerat(?:e|es|ed|ing|ion|ive)\b", re.IGNORECASE),
        re.compile(r"\bsummar(?:y|ize|izes|ized|izing|ization)\b", re.IGNORECASE),
        re.compile(r"\bclassif(?:y|ies|ied|ying|ication)\b", re.IGNORECASE),
        re.compile(r"\bagent(?:s|ic)?\b", re.IGNORECASE),
        re.compile(r"\bmulti[-_\s]?agent\b", re.IGNORECASE),
        re.compile(r"\bchat[-_\s]?bot\b", re.IGNORECASE),
        # Russian / Cyrillic markers that show up in workspace seeds.
        re.compile(r"мульти[-\s]?агент", re.IGNORECASE),
        re.compile(r"агент[ыовами]*", re.IGNORECASE),
        re.compile(r"\bЛЛМ\b", re.IGNORECASE),
        re.compile(r"языков[аоые]*\s+модел", re.IGNORECASE),
        re.compile(r"промпт[аыовиеа]*", re.IGNORECASE),
        re.compile(r"генерац[ияиюей]+", re.IGNORECASE),
        re.compile(r"аннотац[ияиюей]+", re.IGNORECASE),
        re.compile(r"саммар(?:изац|и)", re.IGNORECASE),
        re.compile(r"эмбеддинг", re.IGNORECASE),
    ),
}


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
    The patterns above now also recognise generic LLM / agent
    vocabulary in English and Russian. The classifier is still bounded
    -- pure non-AI tasks (e.g. "bump dependency X", "fix a typo") will
    still match nothing.
    """
    if not task_text:
        return set()
    detected: set[Domain] = set()
    for domain, patterns in _KEYWORD_PATTERNS.items():
        if any(p.search(task_text) for p in patterns):
            detected.add(domain)
    return detected


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
    return keyword_hits | llm_hits


def summarize_domains(domains: set[Domain]) -> str:
    """Render a short human-readable summary for the drive banner."""
    if not domains:
        return "No active skills detected for this task."
    labels = {
        Domain.MULTI_AGENT_GMAS: (
            "multi_agent_gmas — task requires a multi-agent system; build "
            "on the in-repo gmas library and use get_gmas_context / "
            "search_gmas_knowledge for follow-up retrieval. The initial "
            "context dump for the task is in gmas_active_context.md."
        ),
    }
    lines = [labels.get(d, d.value) for d in sorted(domains, key=lambda x: x.value)]
    return "Detected skills:\n- " + "\n- ".join(lines)
