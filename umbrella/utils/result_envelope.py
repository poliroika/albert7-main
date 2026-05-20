import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterator


@dataclass
class EnvelopeError:
    code: str
    message: str
    retryable: bool = False
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResultEnvelope:
    ok: bool
    data: Any = None
    errors: list[EnvelopeError] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "errors": [asdict(e) for e in self.errors],
            "meta": self.meta,
        }

    def to_json(self, *, pretty: bool = False) -> str:
        d = self.to_dict()
        if pretty:
            return json.dumps(d, indent=2, ensure_ascii=False)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def success(cls, data: Any = None, **meta: Any) -> "ResultEnvelope":
        return cls(ok=True, data=data, meta=dict(meta))

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        data: Any = None,
        **meta: Any,
    ) -> "ResultEnvelope":
        return cls(
            ok=False,
            data=data,
            errors=[EnvelopeError(code=code, message=message, retryable=retryable)],
            meta=dict(meta),
        )


def emit(envelope: ResultEnvelope, *, stream: bool = False) -> None:
    """Print envelope to stdout; NDJSON if stream=True."""
    line = envelope.to_json()
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def stream_event(event: dict[str, Any], *, run_id: str = "", phase: str = "") -> None:
    """Emit a single NDJSON event (for --stream mode)."""
    env = ResultEnvelope.success(
        data=event,
        run_id=run_id,
        phase=phase,
        took_ms=0,
    )
    emit(env, stream=True)


class ErrorCode:
    PHASE_MANIFEST_INVALID = "PHASE_MANIFEST_INVALID"
    TOOL_DENIED_BY_ENVELOPE = "TOOL_DENIED_BY_ENVELOPE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    WATCHER_ABORT = "WATCHER_ABORT"
    VERIFY_FAILED = "VERIFY_FAILED"
    EVIDENCE_VALIDATION_FAILED = "EVIDENCE_VALIDATION_FAILED"
    UNKNOWN_PHASE = "UNKNOWN_PHASE"
    PALACE_UNAVAILABLE = "PALACE_UNAVAILABLE"
    WORKER_PANIC = "WORKER_PANIC"
    REFLEXION_PROMOTE_DENIED = "REFLEXION_PROMOTE_DENIED"
