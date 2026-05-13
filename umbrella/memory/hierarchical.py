"""Hierarchical Umbrella memory for Ouroboros-facing tools.

The storage model is deliberately small and local: JSONL entries under
``.umbrella/memory/palace.jsonl`` with ``palace_path`` values such as
``workspaces/agent_research/errors`` or ``ideas/gmas``.  It mirrors the
useful MemPalace hierarchy without making ChromaDB or an MCP server a hard
runtime dependency for Umbrella startup.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _utc_timestamp() -> float:
    return time.time()


def _normalize_path(path: str) -> str:
    clean = str(path or "general").strip().replace("\\", "/").strip("/")
    parts = [part for part in clean.split("/") if part and part not in {".", ".."}]
    return "/".join(parts) or "general"


def _split_tags(tags: str | list[str] | set[str] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = tags.replace(";", ",").split(",")
    else:
        raw = [str(tag) for tag in tags]
    return sorted({tag.strip().lower() for tag in raw if tag.strip()})


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w']+", text.lower()))


@dataclass
class HierarchicalMemoryRecord:
    id: str
    palace_path: str
    title: str
    content: str
    kind: str = "observation"
    workspace_id: str = ""
    task_id: str = ""
    tags: list[str] = field(default_factory=list)
    source_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_utc_timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "palace_path": self.palace_path,
            "title": self.title,
            "content": self.content,
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "tags": self.tags,
            "source_path": self.source_path,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HierarchicalMemoryRecord":
        return cls(
            id=str(payload.get("id") or f"mem_{uuid.uuid4().hex[:12]}"),
            palace_path=_normalize_path(str(payload.get("palace_path") or "general")),
            title=str(payload.get("title") or "").strip()[:240],
            content=str(payload.get("content") or ""),
            kind=str(payload.get("kind") or "observation").strip() or "observation",
            workspace_id=str(payload.get("workspace_id") or "").strip(),
            task_id=str(payload.get("task_id") or "").strip(),
            tags=_split_tags(payload.get("tags")),
            source_path=str(payload.get("source_path") or "").strip(),
            metadata=dict(payload.get("metadata") or {}),
            created_at=float(payload.get("created_at") or _utc_timestamp()),
        )


class HierarchicalMemory:
    """Append-only hierarchical memory with lightweight lexical retrieval."""

    def __init__(self, memory_root: Path):
        self.memory_root = memory_root
        self.memory_root.mkdir(parents=True, exist_ok=True)
        # Primary store for new records; ``palace.jsonl`` is still read for backward compatibility.
        self.path = memory_root / "ideas.jsonl"

    def add(
        self,
        *,
        palace_path: str,
        title: str,
        content: str,
        kind: str = "observation",
        workspace_id: str = "",
        task_id: str = "",
        tags: str | list[str] | set[str] | None = None,
        source_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> HierarchicalMemoryRecord:
        record = HierarchicalMemoryRecord(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            palace_path=_normalize_path(palace_path),
            title=title.strip()[:240] or "Untitled memory",
            content=content.strip(),
            kind=kind.strip() or "observation",
            workspace_id=workspace_id.strip(),
            task_id=task_id.strip(),
            tags=_split_tags(tags),
            source_path=source_path.strip(),
            metadata=dict(metadata or {}),
        )
        self._append(record)
        return record

    def query(
        self,
        *,
        query: str = "",
        palace_path: str = "",
        workspace_id: str = "",
        limit: int = 10,
    ) -> list[HierarchicalMemoryRecord]:
        records = self.read_all()
        path_filter = _normalize_path(palace_path) if palace_path.strip() else ""
        query_tokens = _tokens(query)
        ranked: list[tuple[float, HierarchicalMemoryRecord]] = []

        for record in records:
            if path_filter and not record.palace_path.startswith(path_filter):
                continue
            if workspace_id and record.workspace_id != workspace_id:
                continue
            ranked.append((self._score(record, query_tokens), record))

        ranked.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [
            record for score, record in ranked[: max(1, min(limit, 100))] if score >= 0
        ]

    def recent(
        self, *, palace_path: str = "", limit: int = 20
    ) -> list[HierarchicalMemoryRecord]:
        return self.query(query="", palace_path=palace_path, limit=limit)

    def stats(self) -> dict[str, Any]:
        records = self.read_all()
        by_path: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for record in records:
            by_path[record.palace_path] = by_path.get(record.palace_path, 0) + 1
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
        return {
            "total": len(records),
            "paths": dict(sorted(by_path.items())),
            "kinds": dict(sorted(by_kind.items())),
            "storage_path": str(self.path),
            "legacy_storage_path": str(self.memory_root / "palace.jsonl"),
        }

    def read_all(self) -> list[HierarchicalMemoryRecord]:
        records: list[HierarchicalMemoryRecord] = []
        for fname in ("ideas.jsonl", "palace.jsonl"):
            path = self.memory_root / fname
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(HierarchicalMemoryRecord.from_dict(payload))
        return records

    def _append(self, record: HierarchicalMemoryRecord) -> None:
        target = self.memory_root / "ideas.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    @staticmethod
    def _score(record: HierarchicalMemoryRecord, query_tokens: set[str]) -> float:
        if not query_tokens:
            return 0.0
        haystack = " ".join(
            [
                record.palace_path,
                record.title,
                record.content,
                record.kind,
                record.workspace_id,
                " ".join(record.tags),
            ]
        )
        record_tokens = _tokens(haystack)
        overlap = len(query_tokens.intersection(record_tokens))
        substring_hits = sum(1 for token in query_tokens if token in haystack.lower())
        return float(overlap * 3 + substring_hits)
