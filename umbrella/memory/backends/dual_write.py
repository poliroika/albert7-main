"""Dual-write durable memory to canonical storage plus Hindsight mirror."""

import logging
from pathlib import Path
from typing import Any

from umbrella.memory.backends.canonical import CanonicalMemoryBackend
from umbrella.memory.backends.hindsight import HindsightBackend
from umbrella.memory.kernel.telemetry import record_memory_event

log = logging.getLogger(__name__)


def _ok(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("ok") or result.get("saved"))
    return bool(result)


class DualWriteDurableBackend:
    def __init__(
        self,
        *,
        primary: CanonicalMemoryBackend,
        secondary: HindsightBackend,
        secondary_best_effort: bool = True,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._secondary_best_effort = secondary_best_effort

    def _record_secondary_warning(
        self,
        *,
        op: str,
        error: str = "",
        status: str = "unavailable",
    ) -> None:
        try:
            record_memory_event(
                self._primary._repo_root,
                event_type="hindsight_backend_warnings",
                workspace_id=self._primary._workspace_id,
                backend="hindsight",
                status=status,
                error=error,
                data={"op": op},
            )
        except Exception:
            log.debug("dual-write telemetry skipped", exc_info=True)

    def _secondary_available(self, *, op: str) -> bool:
        health = self._secondary.health()
        if health.get("ok"):
            return True
        if health.get("enabled"):
            self._record_secondary_warning(
                op=op,
                error=str(health.get("error") or health.get("reason") or "unavailable"),
            )
        return False

    def _mirror(self, op: str, payload: Any) -> dict[str, Any]:
        if not self._secondary_available(op=op):
            return {"ok": False, "skipped": True, "reason": "secondary_unavailable"}
        try:
            return getattr(self._secondary, op)(payload)
        except Exception as exc:
            if not self._secondary_best_effort:
                raise
            self._record_secondary_warning(op=op, error=str(exc), status="failed")
            return {"ok": False, "error": str(exc), "best_effort": True}

    def ensure_banks(self, *, workspace_id: str = "") -> dict[str, Any]:
        primary = self._primary.ensure_banks(workspace_id=workspace_id)
        secondary = {}
        if self._secondary_available(op="ensure_banks"):
            try:
                secondary = self._secondary.ensure_banks(workspace_id=workspace_id)
            except Exception as exc:
                if not self._secondary_best_effort:
                    raise
                self._record_secondary_warning(op="ensure_banks", error=str(exc))
        return {"ok": _ok(primary), "canonical": primary, "hindsight": secondary}

    def retain_lesson(self, lesson: Any) -> dict[str, Any]:
        primary = self._primary.retain_lesson(lesson)
        secondary: dict[str, Any] = {}
        if _ok(primary):
            secondary = self._mirror("retain_lesson", lesson)
        return {
            "ok": _ok(primary),
            "backend": "dual",
            "canonical": primary,
            "hindsight": secondary,
        }

    def retain_event(self, event: Any) -> dict[str, Any]:
        primary = self._primary.retain_event(event)
        secondary: dict[str, Any] = {}
        if _ok(primary):
            secondary = self._mirror("retain_event", event)
        return {
            "ok": _ok(primary),
            "backend": "dual",
            "canonical": primary,
            "hindsight": secondary,
        }

    def recall_evidence(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return self._primary.recall_evidence(query)

    def reflect_candidates(self, query: Any) -> list[Any]:
        if not self._secondary_available(op="reflect_candidates"):
            return []
        try:
            return self._secondary.reflect_candidates(query)
        except Exception as exc:
            if not self._secondary_best_effort:
                raise
            self._record_secondary_warning(
                op="reflect_candidates", error=str(exc), status="failed"
            )
            return []

    def health(self) -> dict[str, Any]:
        canonical = self._primary.health()
        hindsight = self._secondary.health()
        return {
            "ok": bool(canonical.get("ok")),
            "backend": "dual",
            "canonical": canonical,
            "hindsight": hindsight,
            "warning": "" if hindsight.get("ok") else "hindsight unavailable",
        }

    def close(self) -> None:
        self._primary.close()


def create_durable_backend(*args: Any, **kwargs: Any) -> Any:
    from umbrella.memory.backends.factory import create_durable_backend as factory

    return factory(*args, **kwargs)


_DualWriteBackend = DualWriteDurableBackend
