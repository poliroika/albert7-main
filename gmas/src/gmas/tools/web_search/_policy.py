"""Configurable retrieval/excerpt policy for :class:`WebSearchTool`."""

import dataclasses
import re
from typing import Any

from ._fetchers import BrowserFetcher

_DEFAULT_QUERY_TERM_PATTERN = r"(?u)[^\W_]{3,}"
_LONG_SECTION_MIN_CHARS = 80
_MEDIUM_CONTENT_LENGTH = 600
_HIGH_CONTENT_LENGTH = 1400
_MEDIUM_SNIPPET_LENGTH = 120
_LONG_SNIPPET_LENGTH = 220
_BEST_SNIPPET_SCORE_THRESHOLD = 0.30
_AVERAGE_SNIPPET_SCORE_THRESHOLD = 0.18


def _default_stopwords() -> frozenset[str]:
    return frozenset(
        {
            "what",
            "when",
            "where",
            "which",
            "who",
            "whom",
            "whose",
            "why",
            "how",
            "the",
            "and",
            "for",
            "with",
            "from",
            "that",
            "this",
            "into",
            "your",
            "about",
            "have",
            "has",
            "had",
            "are",
            "was",
            "were",
            "can",
            "could",
            "should",
            "would",
            "\u043a\u0430\u043a\u043e\u0435",
            "\u043a\u0430\u043a\u0430\u044f",
            "\u043a\u0430\u043a\u043e\u0439",
            "\u043a\u0430\u043a\u0438\u0435",
            "\u043a\u043e\u0433\u0434\u0430",
            "\u0433\u0434\u0435",
            "\u043a\u0442\u043e",
            "\u043a\u0430\u043a",
            "\u0434\u043b\u044f",
            "\u0447\u0442\u043e",
            "\u044d\u0442\u043e",
            "\u0438\u043b\u0438",
        }
    )


def _default_boilerplate_patterns() -> tuple[str, ...]:
    return (
        "enable javascript",
        "cookie policy",
        "privacy policy",
        "sign in",
        "log in",
        "subscribe",
        "accept cookies",
    )


def _default_query_pattern() -> str:
    return _DEFAULT_QUERY_TERM_PATTERN


@dataclasses.dataclass(slots=True)
class WebSearchPolicy:
    """Scoring and enrichment policy used by :class:`WebSearchTool`."""

    bulk_fetch_timeout: int = 5
    http_enrich_concurrency: int = 5
    query_term_limit: int = 8
    content_quality_threshold: float = 0.45
    top_result_quality_bonus: tuple[float, float] = (0.10, 0.05)
    full_browser_rescue_threshold: float = 0.30
    full_browser_rescue_pages: int = 1
    max_output_content_budget: int = 4500
    min_output_content_budget: int = 1500
    min_page_content_budget: int = 600
    min_excerpt_remainder: int = 120
    default_browser_fetch_pages: int = 3
    default_http_fetch_pages: int = 5
    min_fallback_content: int = BrowserFetcher.MIN_FALLBACK_CONTENT
    boilerplate_patterns: tuple[str, ...] = dataclasses.field(default_factory=_default_boilerplate_patterns)
    stopwords: frozenset[str] = dataclasses.field(default_factory=_default_stopwords)
    query_token_pattern: str = dataclasses.field(default_factory=_default_query_pattern)
    _query_token_re: re.Pattern[str] = dataclasses.field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.bulk_fetch_timeout = max(1, self.bulk_fetch_timeout)
        self.http_enrich_concurrency = max(1, self.http_enrich_concurrency)
        self.query_term_limit = max(1, self.query_term_limit)
        self.full_browser_rescue_pages = max(0, self.full_browser_rescue_pages)
        self.max_output_content_budget = max(1, self.max_output_content_budget)
        self.min_output_content_budget = max(1, self.min_output_content_budget)
        self.min_page_content_budget = max(1, self.min_page_content_budget)
        self.min_excerpt_remainder = max(1, self.min_excerpt_remainder)
        self.default_browser_fetch_pages = max(1, self.default_browser_fetch_pages)
        self.default_http_fetch_pages = max(1, self.default_http_fetch_pages)
        self.min_fallback_content = max(1, self.min_fallback_content)
        self._query_token_re = re.compile(self.query_token_pattern)

    @staticmethod
    def split_content_sections(content: str) -> list[str]:
        sections = [part.strip() for part in re.split(r"\n\s*\n+", content) if part.strip()]
        if sections:
            return sections
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if lines:
            return lines
        stripped_content = content.strip()
        return [stripped_content] if stripped_content else []

    @staticmethod
    def _score_excerpt_section(section: str, query_terms: list[str]) -> int:
        lowered = section.lower()
        score = sum(3 for term in query_terms if term in lowered)
        if any(ch.isdigit() for ch in section):
            score += 1
        if "http" in lowered:
            score += 1
        if len(section) > _LONG_SECTION_MIN_CHARS:
            score += 1
        return score

    def extract_query_terms(self, query: str) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for token in self._query_token_re.findall((query or "").lower()):
            if token in self.stopwords or token in seen:
                continue
            seen.add(token)
            terms.append(token)
            if len(terms) >= self.query_term_limit:
                break
        return terms

    def extract_query_focused_excerpt(self, content: str, query_terms: list[str], max_chars: int) -> str:
        text = (content or "").strip()
        if not text or max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text

        sections = self.split_content_sections(text)
        if not sections:
            return text[:max_chars]

        scored_sections = [
            (self._score_excerpt_section(section, query_terms), index, section)
            for index, section in enumerate(sections)
        ]

        scored_sections.sort(key=lambda item: (item[0], -item[1], len(item[2])), reverse=True)
        selected: list[tuple[int, str]] = []
        total = 0
        for score, index, section in scored_sections:
            if total >= max_chars and selected:
                break
            if score <= 0 and selected:
                break
            chunk = section if len(section) <= max_chars else section[:max_chars]
            if total + len(chunk) > max_chars and selected:
                remaining = max_chars - total
                if remaining < self.min_excerpt_remainder:
                    break
                chunk = chunk[:remaining]
            selected.append((index, chunk))
            total += len(chunk) + 2

        if not selected:
            return text[:max_chars]

        selected.sort(key=lambda item: item[0])
        excerpt = "\n\n".join(chunk for _, chunk in selected).strip()
        return excerpt[:max_chars]

    def prepare_results_for_output(
        self,
        results: list[dict[str, str]],
        *,
        query: str,
        with_content: bool,
        max_content_length: int,
    ) -> list[dict[str, str]]:
        if not with_content:
            return results

        query_terms = self.extract_query_terms(query)
        content_indexes = [idx for idx, result in enumerate(results) if result.get("content")]
        if not content_indexes:
            return results

        display_results = [dict(result) for result in results]
        weights = [max(1.0, 3.0 - idx * 0.6) for idx in content_indexes]
        total_weight = sum(weights) or float(len(content_indexes))
        total_budget = max(
            self.min_output_content_budget,
            min(max_content_length, self.max_output_content_budget),
        )

        for idx, weight in zip(content_indexes, weights, strict=False):
            page_budget = max(self.min_page_content_budget, int(total_budget * (weight / total_weight)))
            excerpt = self.extract_query_focused_excerpt(
                display_results[idx].get("content", ""),
                query_terms,
                page_budget,
            )
            display_results[idx]["content"] = excerpt

        return display_results

    def content_quality_score(
        self,
        query_terms: list[str],
        result: dict[str, Any],
        fetched: dict[str, Any] | None,
    ) -> float:
        if not fetched or not fetched.get("success"):
            return 0.0

        content = str(fetched.get("content", "") or "")
        if not content:
            return 0.0

        content_lower = content.lower()
        title = str(fetched.get("title") or result.get("title") or "").lower()
        snippet = str(result.get("snippet", "") or "").lower()
        score = 0.0

        content_len = len(content)
        if content_len >= self.min_fallback_content:
            score += 0.15
        if content_len >= _MEDIUM_CONTENT_LENGTH:
            score += 0.15
        if content_len >= _HIGH_CONTENT_LENGTH:
            score += 0.10

        if any(ch.isdigit() for ch in content):
            score += 0.05
        if "\n" in content:
            score += 0.05

        for term in query_terms:
            if term in title:
                score += 0.06
            elif term in snippet:
                score += 0.04
            elif term in content_lower:
                score += 0.03

        boilerplate_hits = sum(pattern in content_lower[:2000] for pattern in self.boilerplate_patterns)
        score -= min(0.25, boilerplate_hits * 0.08)

        return max(0.0, min(1.0, score))

    def snippet_quality_score(self, query_terms: list[str], result: dict[str, Any]) -> float:
        title = str(result.get("title", "") or "").lower()
        snippet = str(result.get("snippet", "") or "").lower()
        text = f"{title}\n{snippet}".strip()
        if not text:
            return 0.0

        score = 0.0
        if len(snippet) >= _MEDIUM_SNIPPET_LENGTH:
            score += 0.12
        if len(snippet) >= _LONG_SNIPPET_LENGTH:
            score += 0.08
        if any(ch.isdigit() for ch in text):
            score += 0.10

        for term in query_terms:
            if term in title:
                score += 0.10
            elif term in snippet:
                score += 0.06

        return max(0.0, min(1.0, score))

    def results_need_content_fetch(self, query: str, results: list[dict[str, str]]) -> bool:
        query_terms = self.extract_query_terms(query)
        if not query_terms:
            return True

        top_results = results[: min(3, len(results))]
        if not top_results:
            return True

        scores = [self.snippet_quality_score(query_terms, result) for result in top_results]
        best = max(scores, default=0.0)
        avg = sum(scores) / len(scores)
        return not (best >= _BEST_SNIPPET_SCORE_THRESHOLD and avg >= _AVERAGE_SNIPPET_SCORE_THRESHOLD)

    def should_browser_enrich_candidate(
        self,
        query_terms: list[str],
        idx: int,
        result: dict[str, Any],
        fetched: dict[str, Any] | None,
    ) -> bool:
        if not fetched or not fetched.get("success") or len(fetched.get("content", "")) < self.min_fallback_content:
            return True

        threshold = self.content_quality_threshold
        if idx == 0:
            threshold += self.top_result_quality_bonus[0]
        elif idx == 1:
            threshold += self.top_result_quality_bonus[1]
        return self.content_quality_score(query_terms, result, fetched) < threshold
