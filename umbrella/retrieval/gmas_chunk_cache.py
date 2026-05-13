"""Palace-backed cache for summarized GMAS retrieval chunks (shared across workspaces)."""

import hashlib
import logging
from pathlib import Path

from umbrella.memory.paths import palace_path_for

log = logging.getLogger(__name__)

_ROOM = "gmas_chunks"


def make_cache_key(rel_path: str, content_sha16: str, target_tokens: int) -> str:
    return f"gmas_chunk::{rel_path}::{content_sha16}::{int(target_tokens)}"


def content_hash_prefix(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def get_cached_summary(repo_root: Path, cache_key: str) -> str | None:
    try:
        from umbrella.memory.palace_backend import get_palace_backend

        palace = get_palace_backend(palace_path_for(repo_root, "__shared__"))
        doc = palace.fetch_document_by_metadata(
            workspace_id="__shared__",
            room=_ROOM,
            field="gmas_cache_key",
            value=cache_key,
        )
        if doc:
            return doc
    except Exception as exc:
        log.debug("GMAS chunk cache read failed: %s", exc)
    return None


def put_cached_summary(
    repo_root: Path,
    cache_key: str,
    summary: str,
    rel_path: str,
) -> None:
    try:
        from umbrella.memory.palace_backend import get_palace_backend

        palace = get_palace_backend(palace_path_for(repo_root, "__shared__"))
        palace.add(
            workspace_id="__shared__",
            event_type="observation",
            room=_ROOM,
            title="gmas_cache",
            content=summary,
            kind="cache",
            tags=["gmas", "cache"],
            source_path=rel_path[:480],
            metadata_extra={
                "gmas_cache_key": cache_key[:480],
                "source_rel_path": rel_path[:480],
            },
        )
    except Exception as exc:
        log.debug("GMAS chunk cache write failed: %s", exc)
