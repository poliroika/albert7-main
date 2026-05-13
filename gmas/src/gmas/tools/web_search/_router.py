"""Intent-based search router for automatic provider selection."""

import dataclasses
import re
from abc import ABC, abstractmethod
from typing import ClassVar

from gmas.config.logging import logger

from ._providers import SearchProvider


class IntentClassifier(ABC):
    """Pluggable intent classifier for ML/LLM-based detection."""

    @abstractmethod
    def classify(self, query: str) -> dict[str, float]:
        """Return ``{intent_name: confidence}`` for *query* (values in [0, 1])."""


class SearchRouter:
    """
    Select the best search provider for a query based on intent scoring.

    Uses weighted keyword/phrase/regex signals per intent, with optional
    external classifier blending.
    """

    CONFIDENCE_THRESHOLD: ClassVar[float] = 0.15

    @dataclasses.dataclass(frozen=True, slots=True)
    class _Signal:
        pattern: re.Pattern[str]
        weight: float
        is_phrase: bool = False

    _signal_cache: ClassVar[dict[str, list["SearchRouter._Signal"]] | None] = None

    INTENT_SIGNALS: ClassVar[dict[str, list[tuple[str, float]]]] = {
        "news": [
            ("breaking news", 1.0),
            ("latest news", 1.0),
            ("news today", 0.9),
            ("current events", 0.8),
            ("press release", 0.7),
            ("news about", 0.8),
            ("what happened", 0.6),
            ("breaking", 0.5),
            ("headlines", 0.7),
            ("announce", 0.5),
            ("announced", 0.5),
            ("announcement", 0.6),
            ("reporting", 0.4),
            ("journalist", 0.5),
            ("latest", 0.25),
            ("today", 0.15),
            ("update", 0.15),
            ("recent", 0.20),
            ("новост", 0.7),
            ("последн", 0.25),
            ("последние новост", 1.0),
            ("сегодня", 0.15),
            ("свеж", 0.3),
            ("обновлен", 0.15),
            ("documentation", -0.4),
            ("docs", -0.3),
            ("tutorial", -0.4),
            ("how to", -0.3),
            ("install", -0.3),
            ("error", -0.3),
            ("bug", -0.3),
            ("документаци", -0.3),
        ],
        "technical": [
            ("stack trace", 1.0),
            ("how to implement", 0.9),
            ("how to fix", 0.8),
            ("code example", 0.9),
            ("api reference", 1.0),
            ("api docs", 1.0),
            ("python docs", 0.9),
            ("official docs", 0.9),
            ("type error", 0.8),
            ("runtime error", 0.8),
            ("syntax error", 0.8),
            ("import error", 0.8),
            ("how to use", 0.6),
            ("stackoverflow", 0.9),
            ("github", 0.7),
            ("documentation", 0.7),
            ("docs", 0.5),
            ("tutorial", 0.6),
            ("library", 0.5),
            ("framework", 0.5),
            ("implement", 0.5),
            ("function", 0.4),
            ("module", 0.4),
            ("package", 0.4),
            ("error", 0.4),
            ("bug", 0.4),
            ("debug", 0.5),
            ("traceback", 0.8),
            ("exception", 0.5),
            ("deprecat", 0.5),
            ("api", 0.4),
            ("sdk", 0.6),
            ("cli", 0.5),
            ("npm", 0.6),
            ("pip", 0.6),
            ("cargo", 0.6),
            ("maven", 0.6),
            ("gradle", 0.5),
            ("webpack", 0.6),
            ("docker", 0.5),
            ("kubernetes", 0.5),
            ("ошибк", 0.5),
            ("баг", 0.5),
            ("библиотек", 0.5),
            ("документаци", 0.7),
            ("реализ", 0.4),
            ("как исправить", 0.8),
            ("как использовать", 0.6),
            ("как сделать", 0.5),
            ("пример кода", 0.8),
            ("price", -0.3),
            ("buy", -0.3),
            ("cheap", -0.4),
            ("discount", -0.4),
            ("news", -0.2),
            ("цен", -0.3),
            ("купить", -0.3),
        ],
        "shopping": [
            ("best price", 1.0),
            ("where to buy", 1.0),
            ("how much does", 0.8),
            ("for sale", 0.9),
            ("free shipping", 0.9),
            ("coupon code", 0.9),
            ("promo code", 0.9),
            ("price comparison", 1.0),
            ("black friday", 0.8),
            ("price", 0.6),
            ("buy", 0.5),
            ("purchase", 0.6),
            ("cheap", 0.6),
            ("cheapest", 0.7),
            ("deal", 0.5),
            ("deals", 0.6),
            ("discount", 0.6),
            ("store", 0.4),
            ("shop", 0.4),
            ("cost", 0.4),
            ("affordable", 0.5),
            ("shipping", 0.5),
            ("coupon", 0.6),
            ("promo", 0.5),
            ("amazon", 0.5),
            ("ebay", 0.5),
            ("walmart", 0.5),
            ("цен", 0.6),
            ("купить", 0.6),
            ("стоимост", 0.5),
            ("дешев", 0.5),
            ("скидк", 0.5),
            ("магазин", 0.4),
            ("где купить", 1.0),
            ("сколько стоит", 0.9),
            ("documentation", -0.4),
            ("tutorial", -0.4),
            ("how to implement", -0.5),
            ("github", -0.4),
            ("error", -0.3),
            ("документаци", -0.4),
        ],
        "research": [
            ("research paper", 1.0),
            ("scientific study", 1.0),
            ("literature review", 1.0),
            ("peer reviewed", 1.0),
            ("systematic review", 1.0),
            ("meta analysis", 1.0),
            ("case study", 0.8),
            ("white paper", 0.8),
            ("academic paper", 1.0),
            ("research", 0.5),
            ("paper", 0.4),
            ("study", 0.4),
            ("survey", 0.4),
            ("thesis", 0.7),
            ("dissertation", 0.7),
            ("journal", 0.6),
            ("arxiv", 0.9),
            ("pubmed", 0.9),
            ("scholar", 0.7),
            ("doi", 0.8),
            ("abstract", 0.4),
            ("hypothesis", 0.5),
            ("methodology", 0.6),
            ("findings", 0.4),
            ("conclusion", 0.3),
            ("analysis", 0.3),
            ("исследовани", 0.5),
            ("стать", 0.4),
            ("обзор", 0.3),
            ("анализ", 0.3),
            ("научн", 0.6),
            ("диссертаци", 0.7),
            ("buy", -0.3),
            ("price", -0.3),
            ("news today", -0.3),
            ("купить", -0.3),
        ],
        "semantic": [
            ("similar to", 1.0),
            ("companies like", 1.0),
            ("alternatives to", 1.0),
            ("sites like", 0.9),
            ("apps like", 0.9),
            ("tools like", 0.8),
            ("products like", 0.8),
            ("services like", 0.8),
            ("compared to", 0.6),
            ("alternative", 0.6),
            ("alternatives", 0.7),
            ("competitor", 0.6),
            ("competitors", 0.7),
            ("versus", 0.4),
            (" vs ", 0.4),
            ("comparison", 0.4),
            ("похож", 0.5),
            ("аналог", 0.6),
            ("альтернатив", 0.6),
            ("компании как", 0.9),
            ("сайты как", 0.8),
            ("сервисы как", 0.8),
        ],
        "chinese": [
            ("中文", 0.8),
            ("中国", 0.6),
            ("搜索", 0.5),
            ("查找", 0.5),
            ("百度", 0.9),
            ("知乎", 0.8),
            ("微信", 0.7),
            ("淘宝", 0.7),
            ("微博", 0.7),
            ("bilibili", 0.7),
            ("[\u4e00-\u9fff]{3,}", 0.7),
        ],
    }

    INTENT_PROVIDERS: ClassVar[dict[str, list[str]]] = {
        "news": ["brave", "serper", "tavily", "duckduckgo"],
        "technical": ["serper", "tavily", "brave", "exa", "duckduckgo"],
        "shopping": ["serper", "brave", "google", "duckduckgo"],
        "research": ["exa", "tavily", "brave", "serper", "duckduckgo"],
        "semantic": ["exa", "tavily", "brave", "duckduckgo"],
        "chinese": ["bocha", "tavily", "brave", "duckduckgo"],
        "general": ["brave", "tavily", "serper", "duckduckgo"],
    }

    # ------------------------------------------------------------------

    def __init__(
        self,
        available_providers: dict[str, SearchProvider] | None = None,
        fallback_providers: list[str] | None = None,
        *,
        intent_classifier: IntentClassifier | None = None,
        classifier_weight: float = 0.7,
    ):
        self._available = available_providers or {}
        self._fallback = fallback_providers or ["duckduckgo"]
        self._classifier = intent_classifier
        self._classifier_weight = max(0.0, min(1.0, classifier_weight))
        if SearchRouter._signal_cache is None:
            SearchRouter._signal_cache = SearchRouter._build_signals()

    # ------------------------------------------------------------------

    _RE_CYRILLIC: ClassVar[re.Pattern[str]] = re.compile(r"[\u0400-\u04ff]")
    _RE_CJK: ClassVar[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]")

    @staticmethod
    def _build_signals() -> dict[str, list["SearchRouter._Signal"]]:
        cache: dict[str, list[SearchRouter._Signal]] = {}
        has_cyrillic = SearchRouter._RE_CYRILLIC.search
        has_cjk = SearchRouter._RE_CJK.search

        for intent, raw_list in SearchRouter.INTENT_SIGNALS.items():
            signals: list[SearchRouter._Signal] = []
            for entry in raw_list:
                text, weight = entry[0], entry[1]
                is_phrase = " " in text and not text.startswith("(") and not text.startswith("[")

                if text.startswith(("(", "[", "^")):
                    pat = re.compile(text, re.IGNORECASE)
                elif has_cjk(text):
                    pat = re.compile(re.escape(text), re.IGNORECASE)
                elif has_cyrillic(text):
                    escaped = re.escape(text)
                    pat = re.compile(r"\b" + escaped, re.IGNORECASE)
                elif is_phrase:
                    pat = re.compile(r"\b" + re.escape(text) + r"\b", re.IGNORECASE)
                else:
                    escaped = re.escape(text)
                    pat = re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)

                signals.append(
                    SearchRouter._Signal(
                        pattern=pat,
                        weight=weight,
                        is_phrase=is_phrase,
                    )
                )
            cache[intent] = signals
        return cache

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_intents(self, query: str) -> dict[str, float]:
        assert self._signal_cache is not None  # noqa: S101

        keyword_scores: dict[str, float] = {}
        for intent, signals in self._signal_cache.items():
            score = 0.0
            for sig in signals:
                if sig.pattern.search(query):
                    score += sig.weight
            keyword_scores[intent] = max(0.0, score)

        if self._classifier is not None:
            try:
                ext_scores = self._classifier.classify(query)
            except (ValueError, TypeError, RuntimeError, AttributeError, OSError, ImportError, KeyError):
                logger.opt(exception=True).warning(
                    "External intent classifier failed for {!r} — falling back to keyword scoring only",
                    query,
                )
                ext_scores = {}

            if ext_scores:
                cw = self._classifier_weight
                kw = 1.0 - cw
                all_intents = set(keyword_scores) | set(ext_scores)
                blended: dict[str, float] = {}
                for intent in all_intents:
                    ks = keyword_scores.get(intent, 0.0)
                    es = ext_scores.get(intent, 0.0)
                    blended[intent] = max(0.0, kw * ks + cw * es)
                return blended

        return keyword_scores

    def detect_intent(self, query: str) -> str:
        scores = self.score_intents(query)
        if not scores:
            return "general"
        best_intent = max(scores, key=lambda k: scores[k])
        if scores[best_intent] < self.CONFIDENCE_THRESHOLD:
            return "general"
        return best_intent

    def detect_intents(self, query: str) -> list[tuple[str, float]]:
        scores = self.score_intents(query)
        above = [(intent, score) for intent, score in scores.items() if score >= self.CONFIDENCE_THRESHOLD]
        above.sort(key=lambda t: t[1], reverse=True)
        return above

    def route(self, query: str, *, intent: str | None = None) -> list[str]:
        matched_intents = [(intent, 1.0)] if intent is not None else self.detect_intents(query)

        if not matched_intents:
            matched_intents = [("general", 0.0)]

        seen: set[str] = set()
        ordered: list[str] = []

        for intent_name, _score in matched_intents:
            preferred = self.INTENT_PROVIDERS.get(
                intent_name,
                self.INTENT_PROVIDERS["general"],
            )
            for name in preferred:
                if name in seen:
                    continue
                seen.add(name)
                if self._available and name not in self._available and name not in self._fallback:
                    continue
                ordered.append(name)

        for name in self._fallback:
            if name not in seen:
                seen.add(name)
                ordered.append(name)

        return ordered

    def route_providers(
        self,
        query: str,
        *,
        intent: str | None = None,
    ) -> list[SearchProvider]:
        names = self.route(query, intent=intent)
        return [self._available[n] for n in names if n in self._available]
