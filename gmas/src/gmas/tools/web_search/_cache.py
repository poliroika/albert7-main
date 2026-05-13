"""Thread-safe TTL cache for search queries and page fetches."""

import copy
import dataclasses
import hashlib
import threading
import time as _time
from collections import OrderedDict
from typing import Any

from ._utils import normalize_url


class SearchCache:
    """
    Thread-safe TTL cache with LRU eviction for search results and page fetches.

    Two logical namespaces share the same storage:

    * **search** — keyed by ``(query, max_results, provider, intent, …)``
    * **fetch** — keyed by ``(normalized_url, use_browser, wait_for_selector)``
    """

    @dataclasses.dataclass(slots=True)
    class _Entry:
        value: Any
        expires_at: float

    def __init__(
        self,
        max_entries: int = 256,
        ttl: float = 300.0,
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._ttl = max(0.0, ttl)
        self._store: OrderedDict[str, SearchCache._Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_search_key(
        self,
        query: str,
        max_results: int,
        *,
        provider: str | None = None,
        intent: str | None = None,
        deduplicate: bool = True,
        namespace: str = "",
        source_provider: str = "",
    ) -> str:
        raw = (
            f"search:{query.strip().lower()}:{max_results}"
            f":p={provider or ''}:i={intent or ''}:d={deduplicate}"
            f":ns={namespace}:sp={source_provider}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def _make_fetch_key(
        self,
        url: str,
        *,
        use_browser: bool = False,
        wait_for_selector: str | None = None,
    ) -> str:
        parts = [normalize_url(url)]
        if use_browser:
            parts.append("browser")
        if wait_for_selector:
            parts.append(f"ws={wait_for_selector}")
        raw = "|".join(parts)
        return "fetch:" + hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _get(self, key: str) -> Any | None:
        """Get a value by key (must be called under lock). Returns a deep copy."""
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if _time.monotonic() > entry.expires_at:
            del self._store[key]
            self._misses += 1
            return None
        self._store.move_to_end(key)
        self._hits += 1
        return copy.deepcopy(entry.value)

    def _put(self, key: str, value: Any) -> None:
        """Store a value by key (must be called under lock). Stores a deep copy."""
        snapshot = copy.deepcopy(value)
        now = _time.monotonic()
        if key in self._store:
            self._store.move_to_end(key)
            self._store[key] = self._Entry(value=snapshot, expires_at=now + self._ttl)
        else:
            while len(self._store) >= self._max_entries:
                self._store.popitem(last=False)
            self._store[key] = self._Entry(value=snapshot, expires_at=now + self._ttl)

    # ------------------------------------------------------------------
    # Public API — search
    # ------------------------------------------------------------------

    def get_search(
        self,
        query: str,
        max_results: int,
        *,
        provider: str | None = None,
        intent: str | None = None,
        deduplicate: bool = True,
        namespace: str = "",
        source_provider: str = "",
    ) -> list[dict[str, str]] | None:
        key = self._make_search_key(
            query,
            max_results,
            provider=provider,
            intent=intent,
            deduplicate=deduplicate,
            namespace=namespace,
            source_provider=source_provider,
        )
        with self._lock:
            return self._get(key)

    def put_search(
        self,
        query: str,
        max_results: int,
        results: list[dict[str, str]],
        *,
        provider: str | None = None,
        intent: str | None = None,
        deduplicate: bool = True,
        namespace: str = "",
        source_provider: str = "",
    ) -> None:
        key = self._make_search_key(
            query,
            max_results,
            provider=provider,
            intent=intent,
            deduplicate=deduplicate,
            namespace=namespace,
            source_provider=source_provider,
        )
        with self._lock:
            self._put(key, results)

    def _make_image_search_key(
        self,
        query: str,
        max_results: int,
        *,
        provider: str | None = None,
    ) -> str:
        raw = f"img_search:{query.strip().lower()}:{max_results}:p={provider or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def get_image_search(
        self,
        query: str,
        max_results: int,
        *,
        provider: str | None = None,
    ) -> list[dict[str, str]] | None:
        key = self._make_image_search_key(query, max_results, provider=provider)
        with self._lock:
            return self._get(key)

    def put_image_search(
        self,
        query: str,
        max_results: int,
        results: list[dict[str, str]],
        *,
        provider: str | None = None,
    ) -> None:
        key = self._make_image_search_key(query, max_results, provider=provider)
        with self._lock:
            self._put(key, results)

    # ------------------------------------------------------------------
    # Public API — fetch
    # ------------------------------------------------------------------

    def get_fetch(
        self,
        url: str,
        *,
        use_browser: bool = False,
        wait_for_selector: str | None = None,
    ) -> dict[str, Any] | None:
        key = self._make_fetch_key(
            url,
            use_browser=use_browser,
            wait_for_selector=wait_for_selector,
        )
        with self._lock:
            return self._get(key)

    def put_fetch(
        self,
        url: str,
        data: dict[str, Any],
        *,
        use_browser: bool = False,
        wait_for_selector: str | None = None,
    ) -> None:
        key = self._make_fetch_key(
            url,
            use_browser=use_browser,
            wait_for_selector=wait_for_selector,
        )
        with self._lock:
            self._put(key, data)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._store),
                "max_entries": self._max_entries,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
