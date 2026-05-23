"""Optional Hindsight archive backend.

Umbrella calls retain/recall/reflect explicitly. This module never installs a
Hindsight LLM wrapper and is never imported by the proactive prompt compiler.
"""

import importlib.util
import logging
import os
import time
from pathlib import Path
from typing import Any

from umbrella.contracts import json_ready
from umbrella.memory.backends.base import (
    DurableEvent,
    DurableLesson,
    MemoryHit,
    MemoryQuery,
    ReflectionCandidate,
    ReflectionQuery,
)
from umbrella.memory.hindsight import banks
from umbrella.memory.hindsight.candidates import (
    BKB_CANDIDATE_SCHEMA,
    build_reflection_question,
    parse_reflection_candidates,
)
from umbrella.memory.hindsight.config import HindsightConfig
from umbrella.memory.hindsight.errors import (
    HindsightPolicyError,
    HindsightUnavailableError,
)
from umbrella.memory.hindsight.mapping import (
    derived_tags,
    normalize_metadata,
    normalize_tags,
    stable_hash,
)
from umbrella.memory.hindsight.payloads import (
    build_event_payload,
    build_lesson_payload,
    document_id_for_event,
    document_id_for_lesson,
)
from umbrella.memory.kernel.telemetry import record_memory_event

log = logging.getLogger(__name__)

_VERIFIED_TRUST = {
    "workspace_verified",
    "public_verified",
    "supervisor_verified",
    "mutation_verified",
    "hidden_verified",
    "adversarial_verified",
}
_ALLOWED_EVENT_KINDS = {
    "phase_completed",
    "phase_failed",
    "verification_report",
    "durable_promotion",
    "bkb_patch_accepted",
    "bkb_patch_rejected",
    "architecture_decision",
    "run_summary",
}


def _now_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dict__"):
        return dict(getattr(value, "__dict__"))
    return {}


def _lesson_from_any(value: DurableLesson | Any) -> DurableLesson:
    if isinstance(value, DurableLesson):
        return value
    data = _as_dict(value)
    evidence = data.get("evidence_refs") or ()
    return DurableLesson(
        lesson_id=str(
            data.get("lesson_id")
            or data.get("id")
            or data.get("event_id")
            or stable_hash(data)[:24]
        ),
        kind=str(data.get("kind") or data.get("memory_kind") or "lesson"),
        title=str(data.get("title") or data.get("kind") or "Verified lesson"),
        content=str(data.get("content") or ""),
        workspace_id=str(data.get("workspace_id") or ""),
        run_id=str(data.get("run_id") or ""),
        phase_id=str(data.get("phase_id") or data.get("phase") or ""),
        trust_level=str(data.get("trust_level") or "public_verified"),
        evidence_refs=[json_ready(ref) for ref in evidence],
        tags=list(data.get("tags") or []),
        metadata=dict(data.get("metadata") or {}),
    )


def _event_from_any(value: DurableEvent | Any) -> DurableEvent:
    if isinstance(value, DurableEvent):
        return value
    data = _as_dict(value)
    evidence = data.get("evidence_refs") or ()
    return DurableEvent(
        event_id=str(data.get("event_id") or data.get("id") or stable_hash(data)[:24]),
        kind=str(data.get("kind") or data.get("memory_kind") or "durable_promotion"),
        content=str(data.get("content") or ""),
        workspace_id=str(data.get("workspace_id") or ""),
        run_id=str(data.get("run_id") or ""),
        phase_id=str(data.get("phase_id") or data.get("phase") or ""),
        subtask_id=str(data.get("subtask_id") or ""),
        agent=str(data.get("agent") or data.get("agent_kind") or ""),
        trust_level=str(data.get("trust_level") or "workspace_verified"),
        evidence_refs=[json_ready(ref) for ref in evidence],
        tags=list(data.get("tags") or []),
        metadata=dict(data.get("metadata") or {}),
    )


class HindsightBackend:
    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        workspace_id: str = "",
        config: HindsightConfig | None = None,
        client: Any | None = None,
        bank_id: str = "",
    ) -> None:
        self._repo_root = repo_root
        self._workspace_id = workspace_id
        self.config = config or HindsightConfig.from_env()
        self._provided_client = client
        self._client_instance: Any | None = None
        self._default_bank_id = bank_id or (
            banks.workspace_bank_id(workspace_id) if workspace_id else banks.MANAGER_BANK_ID
        )

    @classmethod
    def from_env(cls, *, repo_root: Path, workspace_id: str = "") -> "HindsightBackend":
        return cls(
            repo_root=repo_root,
            workspace_id=workspace_id,
            config=HindsightConfig.from_env(),
        )

    def _dependency_available(self) -> bool:
        if self._provided_client is not None:
            return True
        module_name = "hindsight" if self.config.embedded else "hindsight_client"
        return importlib.util.find_spec(module_name) is not None

    def _create_client(self) -> Any:
        if self._provided_client is not None:
            return self._provided_client
        if self.config.embedded:
            from hindsight import HindsightEmbedded  # type: ignore[import-untyped]

            return HindsightEmbedded(
                profile=self.config.profile,
                llm_provider=self.config.llm_provider,
                llm_model=self.config.llm_model,
                llm_api_key=os.environ.get("HINDSIGHT_API_LLM_API_KEY")
                or os.environ.get("OPENAI_API_KEY"),
            )
        from hindsight_client import Hindsight  # type: ignore[import-untyped]

        kwargs: dict[str, Any] = {
            "base_url": self.config.base_url,
            "timeout": self.config.timeout_seconds,
        }
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        return Hindsight(**kwargs)

    def _client(self) -> Any:
        if self._client_instance is None:
            self._client_instance = self._create_client()
        return self._client_instance

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise HindsightUnavailableError("Hindsight is disabled")
        if not self._dependency_available():
            raise HindsightUnavailableError("Hindsight client dependency is not installed")

    def _bank_for_workspace(self, workspace_id: str = "") -> str:
        ws = workspace_id or self._workspace_id
        return banks.workspace_bank_id(ws) if ws else banks.MANAGER_BANK_ID

    def _record(
        self,
        *,
        event_type: str,
        workspace_id: str = "",
        run_id: str = "",
        phase_id: str = "",
        status: str = "",
        error: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        if self._repo_root is None:
            return
        record_memory_event(
            self._repo_root,
            event_type=event_type,
            workspace_id=workspace_id or self._workspace_id,
            run_id=run_id,
            phase_id=phase_id,
            backend="hindsight",
            status=status,
            error=error,
            data=data or {},
        )

    def health(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {
                "ok": False,
                "enabled": False,
                "backend": "hindsight",
                "reason": "disabled",
                "mode": self.config.backend_mode,
            }
        if not self._dependency_available():
            return {
                "ok": False,
                "enabled": True,
                "backend": "hindsight",
                "reason": "dependency_unavailable",
                "embedded": self.config.embedded,
                "mode": self.config.backend_mode,
            }
        try:
            client = self._client()
            client_banks = getattr(client, "banks", None)
            if client_banks is not None and hasattr(client_banks, "list"):
                client_banks.list()
            return {
                "ok": True,
                "enabled": True,
                "backend": "hindsight",
                "base_url": self.config.base_url,
                "embedded": self.config.embedded,
                "mode": self.config.backend_mode,
                "bank_id": self._default_bank_id,
            }
        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "backend": "hindsight",
                "base_url": self.config.base_url,
                "embedded": self.config.embedded,
                "mode": self.config.backend_mode,
                "error": str(exc),
            }

    def ensure_banks(self, *, workspace_id: str = "") -> dict[str, Any]:
        self._require_enabled()
        return banks.ensure_banks(self._client(), workspace_id=workspace_id or self._workspace_id)

    def _retain(self, **kwargs: Any) -> Any:
        client = self._client()
        if hasattr(client, "retain"):
            return client.retain(**kwargs)
        memories = getattr(client, "memories", None)
        if memories is not None and hasattr(memories, "retain"):
            return memories.retain(**kwargs)
        raise HindsightUnavailableError("Hindsight client has no retain API")

    @staticmethod
    def _normalize_retain_response(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            result = dict(response)
        else:
            result = {
                key: getattr(response, key)
                for key in ("id", "document_id", "status")
                if hasattr(response, key)
            }
        result.setdefault("ok", True)
        result.setdefault("backend", "hindsight")
        return result

    def retain_event(self, event: DurableEvent | Any) -> dict[str, Any]:
        self._require_enabled()
        durable = _event_from_any(event)
        if durable.kind not in _ALLOWED_EVENT_KINDS:
            raise HindsightPolicyError(f"Hindsight retain_event disallows kind {durable.kind!r}")
        if durable.trust_level in {"untrusted", "agent_reported", "agent_claim"}:
            raise HindsightPolicyError("Hindsight retain_event requires verified trust")
        self.ensure_banks(workspace_id=durable.workspace_id)
        bank_id = self._bank_for_workspace(durable.workspace_id)
        tags = normalize_tags(
            durable.tags
            + derived_tags(
                kind=durable.kind,
                workspace_id=durable.workspace_id,
                run_id=durable.run_id,
                phase_id=durable.phase_id,
                subtask_id=durable.subtask_id,
                agent=durable.agent,
                trust_level=str(durable.trust_level),
                scope="workspace" if durable.workspace_id else "manager",
            )
        )
        metadata = normalize_metadata(
            {
                **durable.metadata,
                "umbrella_id": durable.event_id,
                "workspace_id": durable.workspace_id,
                "run_id": durable.run_id,
                "phase_id": durable.phase_id,
                "subtask_id": durable.subtask_id,
                "agent": durable.agent,
                "kind": durable.kind,
                "trust_level": durable.trust_level,
                "source_hash": durable.metadata.get("source_hash")
                or stable_hash(durable.content),
            },
            durable.evidence_refs,
        )
        start = time.perf_counter()
        response = self._retain(
            bank_id=bank_id,
            content=build_event_payload(durable),
            context=f"Umbrella durable event: {durable.kind}",
            timestamp=durable.occurred_at or None,
            document_id=document_id_for_event(durable),
            metadata=metadata,
            tags=tags,
            retain_async=self.config.retain_async,
        )
        result = self._normalize_retain_response(response)
        result["latency_ms"] = _now_ms(start)
        result["bank_id"] = bank_id
        self._record(
            event_type="hindsight_retain_success",
            workspace_id=durable.workspace_id,
            run_id=durable.run_id,
            phase_id=durable.phase_id,
            status="ok",
            data={
                "kind": durable.kind,
                "latency_ms": result["latency_ms"],
                "bank_id": bank_id,
            },
        )
        return result

    def retain_lesson(self, lesson: DurableLesson | Any) -> dict[str, Any]:
        self._require_enabled()
        durable = _lesson_from_any(lesson)
        if str(durable.trust_level) not in _VERIFIED_TRUST:
            raise HindsightPolicyError(
                "Hindsight retain_lesson requires verified trust level"
            )
        if not durable.evidence_refs:
            raise HindsightPolicyError("Hindsight retain_lesson requires evidence_refs")
        self.ensure_banks(workspace_id=durable.workspace_id)
        bank_id = self._bank_for_workspace(durable.workspace_id)
        scope = "workspace" if durable.workspace_id else "manager"
        tags = normalize_tags(
            durable.tags
            + derived_tags(
                kind=durable.kind,
                workspace_id=durable.workspace_id,
                run_id=durable.run_id,
                phase_id=durable.phase_id,
                trust_level=str(durable.trust_level),
                scope=scope,
                store="bkb" if "bkb" in durable.kind else "palace.lesson",
            )
        )
        metadata = normalize_metadata(
            {
                **durable.metadata,
                "umbrella_id": durable.lesson_id,
                "workspace_id": durable.workspace_id,
                "run_id": durable.run_id,
                "phase_id": durable.phase_id,
                "kind": durable.kind,
                "trust_level": durable.trust_level,
                "source_hash": durable.metadata.get("source_hash")
                or stable_hash(durable.content),
            },
            durable.evidence_refs,
        )
        start = time.perf_counter()
        response = self._retain(
            bank_id=bank_id,
            content=build_lesson_payload(durable),
            context=f"Umbrella verified lesson: {durable.kind}",
            timestamp=None,
            document_id=document_id_for_lesson(durable),
            metadata=metadata,
            tags=tags,
            retain_async=self.config.retain_async,
        )
        result = self._normalize_retain_response(response)
        result["latency_ms"] = _now_ms(start)
        result["bank_id"] = bank_id
        self._record(
            event_type="hindsight_retain_success",
            workspace_id=durable.workspace_id,
            run_id=durable.run_id,
            phase_id=durable.phase_id,
            status="ok",
            data={
                "kind": durable.kind,
                "latency_ms": result["latency_ms"],
                "bank_id": bank_id,
            },
        )
        return result

    def recall_evidence(self, query: MemoryQuery | dict[str, Any]) -> list[MemoryHit]:
        if not self.config.enabled:
            return []
        self._require_enabled()
        if isinstance(query, MemoryQuery):
            q = query
        else:
            data = dict(query)
            q = MemoryQuery(
                query=str(data.get("query") or ""),
                workspace_id=str(data.get("workspace_id") or ""),
                run_id=str(data.get("run_id") or ""),
                phase_id=str(data.get("phase_id") or ""),
                tags=list(data.get("tags") or []),
                max_tokens=int(data.get("max_tokens") or 2048),
                budget=str(data.get("budget") or "low"),
            )
        bank_id = self._bank_for_workspace(q.workspace_id)
        client = self._client()
        if not hasattr(client, "recall"):
            return []
        response = client.recall(
            bank_id=bank_id,
            query=q.query,
            max_tokens=q.max_tokens,
            budget=q.budget,
            tags=q.tags or None,
        )
        rows = getattr(response, "results", response)
        if isinstance(rows, dict):
            rows = rows.get("results") or []
        hits: list[MemoryHit] = []
        for row in rows or []:
            data = dict(row) if isinstance(row, dict) else _as_dict(row)
            hits.append(
                MemoryHit(
                    text=str(data.get("text") or data.get("content") or ""),
                    source=str(data.get("source") or data.get("id") or "hindsight"),
                    score=data.get("score"),
                    kind=str(data.get("kind") or ""),
                    tags=list(data.get("tags") or []),
                    metadata=dict(data.get("metadata") or {}),
                )
            )
        return hits

    @staticmethod
    def tag_supplemental_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tagged: list[dict[str, Any]] = []
        for hit in hits:
            row = dict(hit)
            row.setdefault("surface", "supplemental_evidence")
            row.setdefault("directive", False)
            row.setdefault("source_backend", "hindsight")
            tagged.append(row)
        return tagged

    def reflect_candidates(
        self, query: ReflectionQuery | dict[str, Any]
    ) -> list[ReflectionCandidate]:
        if not self.config.enabled or not self.config.reflect_enabled:
            return []
        self._require_enabled()
        if isinstance(query, ReflectionQuery):
            q = query
        else:
            data = dict(query)
            q = ReflectionQuery(
                question=str(data.get("question") or ""),
                workspace_id=str(data.get("workspace_id") or ""),
                run_id=str(data.get("run_id") or ""),
                phase_id=str(data.get("phase_id") or "reflexion"),
                tags=list(data.get("tags") or []),
                max_candidates=int(data.get("max_candidates") or 3),
                budget=str(data.get("budget") or "mid"),
            )
        bank_id = self._bank_for_workspace(q.workspace_id)
        client = self._client()
        if not hasattr(client, "reflect"):
            return []
        question = q.question or build_reflection_question(
            max_candidates=q.max_candidates or self.config.max_candidates
        )
        kwargs = {
            "bank_id": bank_id,
            "question": question,
            "budget": q.budget,
            "tags": q.tags or None,
            "response_schema": BKB_CANDIDATE_SCHEMA,
        }
        start = time.perf_counter()
        try:
            response = client.reflect(**kwargs)
        except TypeError:
            kwargs.pop("response_schema", None)
            response = client.reflect(**kwargs)
        candidates = parse_reflection_candidates(
            response,
            bank_id=bank_id,
            max_candidates=min(q.max_candidates, self.config.max_candidates),
            budget=q.budget,
        )
        self._record(
            event_type="hindsight_reflect_success",
            workspace_id=q.workspace_id,
            run_id=q.run_id,
            phase_id=q.phase_id,
            status="ok",
            data={
                "candidate_count": len(candidates),
                "latency_ms": _now_ms(start),
                "bank_id": bank_id,
            },
        )
        return candidates
