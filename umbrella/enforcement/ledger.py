"""Hash-chained supervisor ledger for Umbrella tool evidence."""


from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import time
from typing import Any, Iterable


@dataclass(frozen=True)
class SupervisorLedgerEvent:
    event_id: str
    prev_hash: str
    event_hash: str
    timestamp: str
    actor: str
    phase: str
    tool: str
    workspace_id: str
    args_hash: str
    result_hash: str
    touched_files: tuple[str, ...]


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


def _hash_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _ledger_path(repo_root: Path, workspace_id: str) -> Path:
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in workspace_id)
    return repo_root / ".umbrella" / "supervisor_ledger" / f"{safe_id or 'default'}.jsonl"


def _last_hash(path: Path) -> str:
    if not path.exists():
        return "0" * 64
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = str(row.get("event_hash") or "").strip()
                if value:
                    last = value
        return locals().get("last", "0" * 64)
    except OSError:
        return "0" * 64


def append_supervisor_ledger_event(
    *,
    repo_root: str | Path,
    workspace_id: str,
    actor: str,
    phase: str,
    tool: str,
    args: Any = None,
    result: Any = None,
    touched_files: Iterable[str] = (),
) -> SupervisorLedgerEvent:
    """Append a supervisor-owned ledger event and return its hashes."""

    root = Path(repo_root).resolve()
    path = _ledger_path(root, str(workspace_id or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_hash = _last_hash(path)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args_hash = _hash_value(args)
    result_hash = _hash_value(result)
    touched = tuple(str(p).replace("\\", "/") for p in touched_files)
    event_seed = {
        "prev_hash": prev_hash,
        "timestamp": ts,
        "actor": actor,
        "phase": phase,
        "tool": tool,
        "workspace_id": workspace_id,
        "args_hash": args_hash,
        "result_hash": result_hash,
        "touched_files": touched,
    }
    event_hash = _hash_value(event_seed)
    event_id = event_hash[:16]
    row = {
        "event_id": event_id,
        "prev_hash": prev_hash,
        "event_hash": event_hash,
        "timestamp": ts,
        "actor": actor,
        "phase": phase,
        "tool": tool,
        "workspace_id": workspace_id,
        "args_hash": args_hash,
        "result_hash": result_hash,
        "touched_files": list(touched),
        "signature": event_hash,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return SupervisorLedgerEvent(
        event_id=event_id,
        prev_hash=prev_hash,
        event_hash=event_hash,
        timestamp=ts,
        actor=actor,
        phase=phase,
        tool=tool,
        workspace_id=str(workspace_id or ""),
        args_hash=args_hash,
        result_hash=result_hash,
        touched_files=touched,
    )


def supervisor_ledger_path(repo_root: str | Path, workspace_id: str) -> Path:
    """Return the supervisor ledger path for a workspace."""

    return _ledger_path(Path(repo_root).resolve(), str(workspace_id or ""))


def read_supervisor_ledger_events(
    *, repo_root: str | Path, workspace_id: str
) -> list[dict[str, Any]]:
    """Read supervisor ledger rows as dictionaries.

    Invalid JSON rows are skipped. The ledger remains append-only from the
    writer side; readers use this for evidence validation and freshness checks.
    """

    path = supervisor_ledger_path(repo_root, workspace_id)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return rows
    return rows


def supervisor_ledger_ref(event: SupervisorLedgerEvent) -> dict[str, str]:
    """Portable ids for CompletionContract / tool JSON payloads."""

    return {
        "ledger_event_id": event.event_id,
        "ledger_event_hash": event.event_hash,
    }


def latest_ledger_event_id(
    *,
    repo_root: str | Path,
    workspace_id: str,
    tool: str | None = None,
) -> str:
    """Return the most recent ledger event id, optionally filtered by tool name."""

    for row in reversed(
        read_supervisor_ledger_events(repo_root=repo_root, workspace_id=workspace_id)
    ):
        if tool is not None and str(row.get("tool") or "") != tool:
            continue
        event_id = str(row.get("event_id") or "").strip()
        if event_id:
            return event_id
    return ""


def find_supervisor_ledger_event(
    *, repo_root: str | Path, workspace_id: str, event_id: str
) -> dict[str, Any] | None:
    """Find a ledger event by short event id or full event hash."""

    needle = str(event_id or "").strip()
    if not needle:
        return None
    for row in read_supervisor_ledger_events(
        repo_root=repo_root, workspace_id=workspace_id
    ):
        if needle in {str(row.get("event_id") or ""), str(row.get("event_hash") or "")}:
            return row
    return None


__all__ = [
    "SupervisorLedgerEvent",
    "append_supervisor_ledger_event",
    "find_supervisor_ledger_event",
    "latest_ledger_event_id",
    "read_supervisor_ledger_events",
    "supervisor_ledger_path",
    "supervisor_ledger_ref",
]
