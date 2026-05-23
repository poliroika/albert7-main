"""Dual-write durable memory to canonical + optional Hindsight."""

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from umbrella.memory.backends.canonical import CanonicalMemoryBackend
from umbrella.memory.backends.hindsight import HindsightBackend


def create_durable_backend(
    repo_root: Path,
    *,
    workspace_id: str = "",
) -> Any:
    """Select durable backend. Hindsight-only bypasses MemPalace unless explicitly opted in."""
    mode = str(os.environ.get("UMBRELLA_MEMORY_DURABLE_BACKEND", "canonical")).strip().lower()
    canonical = CanonicalMemoryBackend(repo_root, workspace_id)
    if mode == "hindsight":
        if str(os.environ.get("UMBRELLA_ALLOW_UNSAFE_HINDSIGHT_ONLY", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            log.warning(
                "Using hindsight-only durable backend (unsafe; MemPalace is not source of truth)"
            )
            return HindsightBackend(
                bank_id=f"ub:workspace:{workspace_id}" if workspace_id else "ub:manager"
            )
        log.warning(
            "UMBRELLA_MEMORY_DURABLE_BACKEND=hindsight without "
            "UMBRELLA_ALLOW_UNSAFE_HINDSIGHT_ONLY; falling back to canonical MemPalace"
        )
        return canonical
    if mode == "dual":
        return _DualWriteBackend(canonical, HindsightBackend(
            bank_id=f"ub:workspace:{workspace_id}" if workspace_id else "ub:manager"
        ))
    return canonical


class _DualWriteBackend:
    def __init__(self, canonical: CanonicalMemoryBackend, hindsight: HindsightBackend) -> None:
        self._canonical = canonical
        self._hindsight = hindsight

    def retain_lesson(self, lesson: dict[str, Any] | Any) -> str:
        node_id = self._canonical.retain_lesson(lesson)
        if lesson.get("verified") and self._hindsight.health().get("ok"):
            try:
                self._hindsight.retain_lesson(lesson)
            except (NotImplementedError, RuntimeError) as exc:
                try:
                    from umbrella.memory.kernel.telemetry import record_memory_event

                    record_memory_event(
                        self._canonical._repo_root,
                        event_type="memory_dual_write_secondary_failed",
                        workspace_id=self._canonical._workspace_id,
                        status="failed",
                        error=str(exc),
                        data={"backend": "hindsight", "op": "retain_lesson"},
                    )
                except Exception:
                    log.debug(
                        "dual_write hindsight retain_lesson telemetry skipped",
                        exc_info=True,
                    )
        return node_id

    def retain_event(self, event: dict[str, Any] | Any) -> str:
        node_id = self._canonical.retain_event(event)
        if event.get("verified") and self._hindsight.health().get("ok"):
            try:
                self._hindsight.retain_event(event)
            except (NotImplementedError, RuntimeError) as exc:
                try:
                    from umbrella.memory.kernel.telemetry import record_memory_event

                    record_memory_event(
                        self._canonical._repo_root,
                        event_type="memory_dual_write_secondary_failed",
                        workspace_id=self._canonical._workspace_id,
                        status="failed",
                        error=str(exc),
                        data={"backend": "hindsight", "op": "retain_event"},
                    )
                except Exception:
                    log.debug(
                        "dual_write hindsight retain_event telemetry skipped",
                        exc_info=True,
                    )
        return node_id

    def recall_evidence(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return self._canonical.recall_evidence(query)

    def reflect_candidates(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return self._hindsight.reflect_candidates(query)
        except Exception as exc:
            try:
                from umbrella.memory.kernel.telemetry import record_memory_event

                record_memory_event(
                    Path(str(query.get("repo_root") or ".")),
                    event_type="memory_backend_unavailable",
                    workspace_id=str(query.get("workspace_id") or ""),
                    status="unavailable",
                    error=str(exc),
                    data={"backend": "hindsight", "op": "reflect_candidates"},
                )
            except Exception:
                log.debug(
                    "hindsight reflect_candidates telemetry skipped",
                    exc_info=True,
                )
            return []

    def health(self) -> dict[str, Any]:
        return {
            "canonical": self._canonical.health(),
            "hindsight": self._hindsight.health(),
        }

    def close(self) -> None:
        self._canonical.close()
