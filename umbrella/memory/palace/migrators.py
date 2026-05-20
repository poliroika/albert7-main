import json
import pathlib
from typing import Any

from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.palace.tiers import Tier, Scope


def _iter_jsonl(path: pathlib.Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def migrate_lessons(palace: MemPalace, lessons_path: pathlib.Path) -> int:
    count = 0
    for record in _iter_jsonl(lessons_path):
        content = record.get("content") or record.get("text") or str(record)
        tags = record.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        palace.add(
            store="palace.lesson",
            content=content,
            tier=Tier.WARM,
            scope=Scope.CROSS_RUN_DURABLE,
            tags=tags,
            verified=True,
            extra={"migrated_from": "lessons.jsonl"},
        )
        count += 1
    if count:
        _rename_migrated(lessons_path)
    return count


def migrate_ideas(palace: MemPalace, ideas_path: pathlib.Path) -> int:
    count = 0
    for record in _iter_jsonl(ideas_path):
        content = record.get("content") or record.get("text") or str(record)
        evidence_kind = record.get("evidence_kind", "")
        verified = evidence_kind == "verified_outcome"
        tags = record.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        palace.add(
            store="palace.idea",
            content=content,
            tier=Tier.WARM,
            scope=Scope.CROSS_RUN_DURABLE,
            tags=tags,
            verified=verified,
            extra={"migrated_from": "ideas.jsonl", "evidence_kind": evidence_kind},
        )
        count += 1
    if count:
        _rename_migrated(ideas_path)
    return count


def migrate_gaps_signals(palace: MemPalace, path: pathlib.Path, tag: str) -> int:
    count = 0
    for record in _iter_jsonl(path):
        content = record.get("content") or record.get("text") or str(record)
        palace.add(
            store="palace.idea",
            content=content,
            tier=Tier.COLD,
            scope=Scope.CROSS_RUN_DURABLE,
            tags=[tag],
            verified=False,
            extra={"migrated_from": path.name},
        )
        count += 1
    if count:
        _rename_migrated(path)
    return count


def run_full_migration(palace: MemPalace, memory_root: pathlib.Path) -> dict[str, int]:
    results: dict[str, int] = {}
    results["lessons"] = migrate_lessons(palace, memory_root / "lessons.jsonl")
    results["ideas"] = migrate_ideas(palace, memory_root / "ideas.jsonl")
    results["gaps"] = migrate_gaps_signals(palace, memory_root / "gaps.jsonl", tag="gap")
    results["signals"] = migrate_gaps_signals(palace, memory_root / "signals.jsonl", tag="signal")
    return results


def _rename_migrated(path: pathlib.Path) -> None:
    migrated = path.with_suffix(path.suffix + ".migrated")
    if not migrated.exists():
        path.rename(migrated)
