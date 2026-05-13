import hashlib
from datetime import datetime, timezone
from typing import Any

from umbrella.artifacts.models import ErrorSeverity, ErrorSignature


def classify_error_type(message: str) -> ErrorSeverity:
    text = (message or "").lower()
    if "critical" in text:
        return ErrorSeverity.CRITICAL
    if "warning" in text:
        return ErrorSeverity.WARNING
    if "info" in text:
        return ErrorSeverity.INFO
    return ErrorSeverity.ERROR


def extract_error_signatures(events: list[dict[str, Any]]) -> list[ErrorSignature]:
    signatures: list[ErrorSignature] = []
    seen: set[str] = set()
    for event in events:
        message = str(event.get("error") or "").strip()
        failed_run = (
            event.get("event_type") == "run_end" and event.get("success") is False
        )
        if failed_run and not message:
            message = "Run reported failure"
        if not message:
            continue
        error_type = "RunFailure" if failed_run else "EventError"
        digest = hashlib.sha1(f"{error_type}:{message}".encode()).hexdigest()[
            :12
        ]
        if digest in seen:
            continue
        seen.add(digest)
        signatures.append(
            ErrorSignature(
                error_id=digest,
                error_type=error_type,
                severity=classify_error_type(message),
                message=message,
                timestamp=datetime.now(timezone.utc),
                agent_id=event.get("agent_id"),
                context={"event_type": event.get("event_type")},
                raw_line=str(event),
            )
        )
    return signatures
