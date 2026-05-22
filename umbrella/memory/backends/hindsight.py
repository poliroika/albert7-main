"""Hindsight HTTP client backend (optional dependency).

Do not use Hindsight LLM Wrapper — Umbrella controls retain/recall/reflect explicitly.
"""

import os
from typing import Any


class HindsightBackend:
    """Stage 0: health probe only until UMBRELLA_MEMORY_DURABLE_BACKEND enables dual-write."""

    def __init__(self, *, bank_id: str = "ub:manager") -> None:
        self._bank_id = bank_id
        self._available = False
        try:
            import hindsight_client  # type: ignore[import-untyped]  # noqa: F401

            self._available = True
        except ImportError:
            pass

    def retain_event(self, event: dict[str, Any]) -> str:
        if not self._available:
            raise RuntimeError("hindsight-client is not installed")
        raise NotImplementedError("Hindsight retain_event: enable Stage 1 dual-write explicitly")

    def retain_lesson(self, lesson: dict[str, Any]) -> str:
        return self.retain_event(lesson)

    def recall_evidence(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._available:
            return []
        raise NotImplementedError("Hindsight recall must not feed core overlay directly")

    @staticmethod
    def tag_supplemental_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tag recall rows as non-directive supplemental evidence only."""
        tagged: list[dict[str, Any]] = []
        for hit in hits:
            row = dict(hit)
            row.setdefault("surface", "supplemental_evidence")
            row.setdefault("directive", False)
            row.setdefault("source_backend", "hindsight")
            tagged.append(row)
        return tagged

    def reflect_candidates(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._available:
            return []
        return []

    def health(self) -> dict[str, Any]:
        return {
            "ok": self._available,
            "backend": "hindsight-client",
            "bank_id": self._bank_id,
            "enabled": os.environ.get("UMBRELLA_MEMORY_DURABLE_BACKEND", "canonical"),
        }
