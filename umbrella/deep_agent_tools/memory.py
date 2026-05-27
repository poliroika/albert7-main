"""Umbrella-owned memory, prompt, and lesson handlers for deep agents.

Ouroboros exposes these through its tool registry, but the behavior belongs to
Umbrella so future deep agents can reuse the same memory contract without
copying agent-specific bridge files.
"""

import ast
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ouroboros.limits import MEMORY_HIT_PREVIEW_CHARS
from umbrella.deep_agent_tools.context import (
    _current_workspace_id_from_drive,
    _json,
    _memory_store,
    _palace_backend,
    _PROMPT_NAME_TO_FILE,
    _resolve_prompt_name,
    _resolve_umbrella_repo_root,
    _split_tags,
    _stop_requested_block,
    _workspace_memory_root,
    _workspace_root,
)
from umbrella.memory.palace.facade import MemPalace

log = logging.getLogger(__name__)

_PHASE_MEMORY_TAGS: dict[str, set[str]] = {
    "planner": {"design", "architecture", "discovery", "prior_art"},
    "subtask": {"implementation", "skill", "gmas", "code_pattern", "subtask"},
    "implement": {"implementation", "skill", "gmas", "code_pattern", "subtask"},
    "review": {"review", "defect_pattern"},
    "remediation": {"verification_failure", "lesson", "bug_fix", "verify", "fail"},
}


def _rank_lessons_for_query(lessons: list[Any], query: str, limit: int) -> list[Any]:
    query_tokens = {token for token in query.lower().split() if token}
    if not query_tokens:
        return lessons[:limit]

    def score(lesson: Any) -> tuple[int, int, float]:
        haystack = " ".join(
            [
                str(getattr(lesson, "workspace_id", "") or ""),
                str(getattr(lesson, "change_summary", "") or ""),
                str(getattr(lesson, "expected_effect", "") or ""),
                str(getattr(lesson, "observed_effect", "") or ""),
                str(getattr(lesson, "conclusion", "") or ""),
                " ".join(sorted(getattr(lesson, "tags", set()) or set())),
            ]
        ).lower()
        overlap = sum(1 for token in query_tokens if token in haystack)
        return (
            overlap,
            int(getattr(lesson, "priority", 0) or 0),
            float(getattr(lesson, "created_at", 0.0) or 0.0),
        )

    return sorted(lessons, key=score, reverse=True)[:limit]


def _preferred_memory_tags_for_phase(phase: str) -> set[str]:
    normalized = str(phase or "").lower()
    tags: set[str] = set()
    for key, values in _PHASE_MEMORY_TAGS.items():
        if key in normalized:
            tags.update(values)
    return tags


def _memory_hit_tags(hit: Any) -> set[str]:
    if isinstance(hit, dict):
        metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        raw = hit.get("tags") or metadata.get("tags")
        text = " ".join(
            str(hit.get(key) or "") for key in ("room", "hall", "content", "title")
        ).lower()
    else:
        raw = getattr(hit, "tags", None)
        text = " ".join(
            str(getattr(hit, attr, "") or "")
            for attr in (
                "change_summary",
                "expected_effect",
                "observed_effect",
                "context",
            )
        ).lower()
    tags: set[str] = set()
    if isinstance(raw, str):
        tags.update(
            part.strip().lower()
            for part in raw.replace(";", ",").split(",")
            if part.strip()
        )
    elif isinstance(raw, (list, tuple, set)):
        tags.update(str(part).strip().lower() for part in raw if str(part).strip())
    for marker in (
        "gmas",
        "verification_failure",
        "bug_fix",
        "implementation",
        "cleanup",
        "hygiene",
        "review",
    ):
        if marker in text:
            tags.add(marker)
    return tags


def _phase_rerank_memory_hits(items: list[Any], phase: str = "") -> list[Any]:
    preferred = _preferred_memory_tags_for_phase(phase)
    if not preferred or not items:
        return items
    scored = [
        (bool(_memory_hit_tags(item) & preferred), index, item)
        for index, item in enumerate(items)
    ]
    tagged = [item for matched, _index, item in scored if matched]
    if len(tagged) >= 3:
        return tagged
    return [
        item
        for _matched, _index, item in sorted(
            scored, key=lambda row: (not row[0], row[1])
        )
    ]


_UNVERIFIED_MEMORY_TAGS = {
    "candidate",
    "hypothesis",
    "unverified",
    "unverified_lesson",
}
_UNVERIFIED_MEMORY_ROOMS = {
    "observation",
    "ideas-hypothesis",
    "ideas-observation_from_log",
    "scratchpad",
    "terminal_scrollback",
}

_DURABLE_MEMORY_MARKERS = {
    "architecture_decision",
    "completion_memory",
    "durable",
    "durable_finding",
    "verified_finding",
    "manager_lesson",
    "competency_gap",
    "self_improvement_trigger",
}
_EVIDENCE_REF_KEYS = {
    "evidence_ref",
    "evidence_refs",
    "source_id",
    "source_ids",
    "tool_call_id",
    "tool_result_id",
    "artifact_id",
    "artifact_path",
    "command_evidence_id",
    "verify_run_id",
    "verification_report_id",
    "ledger_event_id",
}
_MEMORY_TRUST_LEVELS = {
    "agent_claim": 0,
    "observed_artifact": 1,
    "public_verified": 2,
    "mutation_verified": 3,
    "hidden_verified": 4,
    "adversarial_verified": 5,
    "contradicted": -1,
    "retracted": -2,
}
_VERIFIED_MEMORY_TRUST_LEVELS = {
    "public_verified",
    "mutation_verified",
    "hidden_verified",
    "adversarial_verified",
}
_SUPERVISOR_EVIDENCE_PRODUCERS = {"supervisor", "verifier", "watcher", "harness"}
_RUN_SCOPED_MEMORY_ROOMS = {
    "phase",
    "phase_plan",
    "research_summary",
    "subtask_card",
    "run",
    "subtask",
    "command",
    "verification",
    "verify_run",
    "verify_runs",
    "preflight",
    "research",
    "research_review",
    "plan",
    "plan_review",
    "execute",
    "final_review",
    "verify",
}


def _memory_tags_from_value(value: Any) -> set[str]:
    raw_tags: Any = None
    if isinstance(value, dict):
        raw_tags = value.get("tags")
        meta = value.get("metadata")
        if raw_tags is None and isinstance(meta, dict):
            raw_tags = meta.get("tags")
    else:
        raw_tags = getattr(value, "tags", None)
    if raw_tags is None:
        return set()
    if isinstance(raw_tags, str):
        parts = raw_tags.replace(";", ",").split(",")
    else:
        try:
            parts = list(raw_tags)
        except TypeError:
            parts = [raw_tags]
    return {str(tag).strip().lower() for tag in parts if str(tag).strip()}


def _memory_evidence_kind(value: Any) -> str:
    if isinstance(value, dict):
        meta = value.get("metadata")
        if isinstance(meta, dict):
            return str(meta.get("evidence_kind") or "").strip().lower()
        return str(value.get("evidence_kind") or "").strip().lower()
    meta = getattr(value, "metadata", None)
    if isinstance(meta, dict):
        return str(meta.get("evidence_kind") or "").strip().lower()
    return ""


def _memory_kind(value: Any) -> str:
    if isinstance(value, dict):
        meta = value.get("metadata")
        if isinstance(meta, dict) and meta.get("kind") is not None:
            return str(meta.get("kind") or "").strip().lower()
        if value.get("kind") is not None:
            return str(value.get("kind") or "").strip().lower()
    meta = getattr(value, "metadata", None)
    if isinstance(meta, dict):
        return str(meta.get("kind") or "").strip().lower()
    return ""


def memory_write_policy_issues(
    *,
    kind: str = "",
    tags: Iterable[str] = (),
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """Return evidence-bound memory policy issues for durable writes."""
    from umbrella.memory.kernel.policy import memory_write_policy_issues as _kernel_policy

    return _kernel_policy(kind=kind, tags=tags, metadata=metadata)


def _memory_source_path(value: Any) -> str:
    if isinstance(value, dict):
        meta = value.get("metadata")
        if isinstance(meta, dict):
            return str(
                meta.get("source_path") or meta.get("source_file") or ""
            ).strip().lower()
        return str(value.get("source_path") or value.get("source_file") or "").strip().lower()
    meta = getattr(value, "metadata", None)
    if isinstance(meta, dict):
        return str(meta.get("source_path") or meta.get("source_file") or "").strip().lower()
    return ""


def _memory_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        meta = value.get("metadata")
        return meta if isinstance(meta, dict) else value
    meta = getattr(value, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _metadata_bool_is_false(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    text = str(value or "").strip().lower()
    return text in {"0", "false", "no", "off", "unverified"}


def _memory_room(value: Any) -> str:
    if isinstance(value, dict):
        room = value.get("room")
        if room is not None:
            return str(room).strip().lower()
    return str(_memory_metadata(value).get("room") or "").strip().lower()


def _memory_task_id(value: Any) -> str:
    meta = _memory_metadata(value)
    return str(meta.get("task_id") or "").strip()


def _run_id_from_task_id(task_id: str) -> str:
    task = str(task_id or "").strip()
    if not task:
        return ""
    return task.split(":", 1)[0]


def _current_run_id_from_ctx(ctx: Any) -> str:
    return _run_id_from_task_id(str(getattr(ctx, "task_id", "") or ""))


def _memory_run_id(value: Any) -> str:
    if isinstance(value, dict):
        run_id = str(value.get("run_id") or "").strip()
        if run_id:
            return run_id
    meta = _memory_metadata(value)
    run_id = str(meta.get("run_id") or "").strip()
    if run_id:
        return run_id
    return _run_id_from_task_id(_memory_task_id(value))


def _is_run_scoped_memory(value: Any) -> bool:
    if isinstance(value, dict):
        scope = str(value.get("scope") or "").strip().lower()
        if scope in {"run_scoped", "subtask_scoped"}:
            return True
    room = _memory_room(value)
    return room in _RUN_SCOPED_MEMORY_ROOMS


def _is_stale_run_scoped_memory(value: Any, current_run_id: str = "") -> bool:
    if not current_run_id:
        return False
    if not _is_run_scoped_memory(value):
        return False
    item_run_id = _memory_run_id(value)
    if not item_run_id:
        return True
    return item_run_id != current_run_id


def _is_unverified_memory(value: Any) -> bool:
    tags = _memory_tags_from_value(value)
    if tags & _UNVERIFIED_MEMORY_TAGS:
        return True
    meta = _memory_metadata(value)
    if "verified" in meta and _metadata_bool_is_false(meta.get("verified")):
        return True
    evidence_kind = _memory_evidence_kind(value)
    if evidence_kind and evidence_kind != "verified_outcome":
        return True
    if (
        evidence_kind == "verified_outcome"
        and _memory_kind(value) == "observation"
        and _memory_source_path(value) == "tool:palace_add"
    ):
        return True
    if isinstance(value, dict):
        room = str(value.get("room") or "").strip().lower()
        if room in _UNVERIFIED_MEMORY_ROOMS:
            return True
    return False


def _split_verified_first(
    items: list[Any], *, current_run_id: str = ""
) -> tuple[list[Any], list[Any]]:
    trusted: list[Any] = []
    unverified: list[Any] = []
    for item in items:
        if _is_unverified_memory(item) or _is_stale_run_scoped_memory(
            item, current_run_id
        ):
            unverified.append(item)
        else:
            trusted.append(item)
    return trusted, unverified


def _lesson_is_verified(lesson: Any) -> bool:
    tags = _memory_tags_from_value(lesson)
    if tags & _UNVERIFIED_MEMORY_TAGS:
        return False
    try:
        priority = int(getattr(lesson, "priority", 0) or 0)
    except Exception:
        priority = 0
    return priority >= 5


def _resolve_memory_query_scope(palace_path: str, workspace_id: str) -> tuple[str, str]:
    from umbrella.memory.paths import parse_palace_path_hint

    ws, _event_type, room = parse_palace_path_hint(palace_path, workspace_id=workspace_id)
    return ws or workspace_id, room


_UUID_TOKEN_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _canonical_mempalace_lookup(
    repo_root: Path,
    *,
    workspace_id: str,
    query: str,
    include_unverified: bool,
    current_run_id: str = "",
) -> tuple[list[dict[str, Any]], list[str]] | None:
    ids = list(dict.fromkeys(match.group(0) for match in _UUID_TOKEN_RE.finditer(query)))
    if not ids:
        return None
    palace = MemPalace(repo_root, workspace_id or None)
    hits: list[dict[str, Any]] = []
    missing: list[str] = []
    for node_id in ids:
        node = palace.get(node_id)
        if not node:
            missing.append(node_id)
            continue
        if not include_unverified and _is_unverified_memory(node):
            missing.append(node_id)
            continue
        if _is_stale_run_scoped_memory(node, current_run_id):
            missing.append(node_id)
            continue
        hits.append(node)
    return hits, missing


def _canonical_mempalace_health(repo_root: Path, workspace_id: str) -> dict[str, Any]:
    palace = MemPalace(repo_root, workspace_id or None)
    try:
        return palace.health()
    finally:
        palace.close()


def _legacy_palace_available() -> bool:
    try:
        import mempalace  # noqa: F401

        return True
    except ImportError:
        return False


def canonical_palace_add(
    repo_root: Path,
    *,
    workspace_id: str,
    content: str,
    title: str = "",
    kind: str = "observation",
    store: str = "palace.idea",
    tier: str = "warm",
    scope: str = "run_scoped",
    tags: list[str] | None = None,
    phase: str = "",
    run_id: str | None = None,
    verified: bool = False,
    source_path: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical MemPalace write; optional legacy mirror when mempalace is installed."""
    from umbrella.memory.kernel.models import memory_event_from_tool_write
    from umbrella.memory.kernel.writer import write_memory_event

    tag_list = list(tags or [])
    mem_body = content or title or ""
    mem_content = f"[{title}]\n{mem_body}" if title else mem_body
    meta = {"title": title, "type": kind, **(extra or {})}
    event = memory_event_from_tool_write(
        content=mem_content,
        title=title,
        memory_kind=kind,
        workspace_id=workspace_id,
        tags=tag_list,
        scope=scope,
        tier=tier,
        phase_id=phase,
        run_id=str(run_id or ""),
        source_path=source_path or "canonical_palace_add",
        verified=verified,
        palace_store=store,
        metadata=meta,
    )
    try:
        result = write_memory_event(
            repo_root,
            event,
            workspace_id=workspace_id,
            mirror_legacy=_legacy_palace_available(),
        )
    except Exception:
        log.debug("canonical_palace_add failed", exc_info=True)
        return {"saved": False, "canonical_id": "", "store": store}

    payload: dict[str, Any] = {
        "saved": result.saved,
        "canonical_id": result.canonical_id,
        "store": result.store or store,
    }
    if result.policy_issues:
        payload["issues"] = list(result.policy_issues)
    return payload


def _empty_memory_response(
    *,
    include_unverified: bool,
    source: str = "canonical_mempalace",
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "palace_memory": [],
        "lesson_memory": [],
        "hierarchical_ideas": [],
        "unverified_candidates": {
            "palace_memory": [],
            "lesson_memory": [],
            "hierarchical_ideas": [],
            "note": "No matching memory found.",
        },
        "include_unverified": bool(include_unverified),
        "source": source,
        "contrastive_lessons": {},
        "stats": stats or {"ok": True, "backend": "canonical_mempalace"},
    }


def _canonical_mempalace_search(
    repo_root: Path,
    *,
    workspace_id: str,
    query: str,
    limit: int,
    include_unverified: bool,
    current_run_id: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    palace = MemPalace(repo_root, workspace_id or None)
    try:
        health = palace.health()
        if not health.get("ok"):
            return [], health
        if query.strip():
            hits = palace.search(
                query,
                stores=[
                    "palace.charter",
                    "palace.lesson",
                    "palace.idea",
                    "palace.codeptr",
                    "palace.skill_index",
                    "palace.run",
                    "palace.phase",
                    "palace.subtask",
                    "palace.durable",
                ],
                n=limit * 3,
            )
        else:
            hits = palace.list_all(n=limit * 3)
        filtered: list[dict[str, Any]] = []
        for hit in hits:
            if not include_unverified and _is_unverified_memory(hit):
                continue
            if _is_stale_run_scoped_memory(hit, current_run_id):
                continue
            filtered.append(hit)
        return filtered[:limit], health
    finally:
        palace.close()


def _palace_lookup(
    repo_root: Path,
    *,
    workspace_id: str,
    room: str,
    query: str,
    limit: int,
    include_unverified: bool,
    phase: str = "",
    current_run_id: str = "",
) -> tuple[Any, list[Any], list[Any]]:
    palace = _palace_backend(repo_root, workspace_id)
    if query.strip():
        hits = palace.search(
            query, workspace_id=workspace_id, room=room, n_results=limit
        )
    else:
        hits = palace.recent(workspace_id=workspace_id, limit=limit)
    trusted_hits, unverified_hits = _split_verified_first(
        hits, current_run_id=current_run_id
    )
    palace_hits = (
        (trusted_hits + unverified_hits) if include_unverified else trusted_hits
    )
    palace_hits = _phase_rerank_memory_hits(palace_hits, phase)
    return palace, palace_hits, unverified_hits


def _lessons_lookup(
    repo_root: Path,
    *,
    workspace_id: str,
    query: str,
    limit: int,
    include_unverified: bool,
    phase: str = "",
) -> tuple[Any, list[Any], list[Any]]:
    from umbrella.memory.models import LessonType, MemoryQuery

    store_for_contrastive = _memory_store(repo_root, "")
    if workspace_id.strip():
        mq_ws = MemoryQuery(
            limit=max(limit * 4, 20), include_stale=False, workspace_id=workspace_id
        )
        lessons_ws = _memory_store(repo_root, workspace_id).query_lessons(mq_ws)
        mq_mgr = MemoryQuery(
            limit=max(limit * 4, 20),
            include_stale=False,
            lesson_type=LessonType.MANAGER,
        )
        lessons_mgr = store_for_contrastive.query_lessons(mq_mgr)
        lessons = _rank_lessons_for_query(lessons_ws + lessons_mgr, query, limit)
    else:
        mq = MemoryQuery(limit=max(limit * 4, 20), include_stale=False)
        lessons = store_for_contrastive.query_lessons(mq)
        lessons = _rank_lessons_for_query(lessons, query, limit)

    try:
        from umbrella.memory.relevance import deduplicate_lessons

        lessons = deduplicate_lessons(lessons)
    except Exception:
        log.debug("deduplicate_lessons skipped", exc_info=True)

    verified_lessons = [lesson for lesson in lessons if _lesson_is_verified(lesson)]
    unverified_lessons = [
        lesson for lesson in lessons if not _lesson_is_verified(lesson)
    ]
    lesson_hits = (
        (verified_lessons + unverified_lessons)
        if include_unverified
        else verified_lessons
    )
    lesson_hits = _phase_rerank_memory_hits(lesson_hits, phase)
    return store_for_contrastive, lesson_hits, unverified_lessons


def _hierarchical_ideas_lookup(
    repo_root: Path,
    *,
    workspace_id: str,
    query: str,
    palace_path: str,
    limit: int,
    include_unverified: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read ideas from ideas.jsonl directly (no HierarchicalMemory)."""
    from umbrella.memory.paths import manager_memory_root, workspace_memory_root

    hierarchical_ideas: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    query_lower = query.lower()

    roots = []
    if workspace_id.strip():
        roots.append(workspace_memory_root(repo_root, workspace_id))
    roots.append(manager_memory_root(repo_root))

    for root in roots:
        ideas_path = root / "ideas.jsonl"
        if not ideas_path.exists():
            continue
        try:
            with ideas_path.open(encoding="utf-8", errors="replace") as _fh:
                for line in _fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    rec_id = str(rec.get("id") or "")
                    if rec_id and rec_id in seen_ids:
                        continue
                    content = str(rec.get("content") or rec.get("body") or "")
                    title = str(rec.get("title") or "")
                    if query_lower and not (
                        query_lower in content.lower() or query_lower in title.lower()
                    ):
                        continue
                    if palace_path and rec.get("palace_path") and not str(
                        rec["palace_path"]
                    ).startswith(palace_path):
                        continue
                    if rec_id:
                        seen_ids.add(rec_id)
                    hierarchical_ideas.append(rec)
                    if len(hierarchical_ideas) >= limit * 3:
                        break
        except Exception:
            pass

    hierarchical_ideas = hierarchical_ideas[: limit * 2]
    verified_ideas = [idea for idea in hierarchical_ideas if not _is_unverified_memory(idea)]
    unverified_ideas = [idea for idea in hierarchical_ideas if _is_unverified_memory(idea)]
    idea_hits = (verified_ideas + unverified_ideas) if include_unverified else verified_ideas
    return idea_hits, verified_ideas, unverified_ideas


def _contrastive_lessons_lookup(
    store_for_contrastive: Any,
    *,
    query: str,
    workspace_id: str,
) -> dict[str, Any]:
    try:
        from umbrella.memory.contrastive import retrieve_contrastive_lessons

        return retrieve_contrastive_lessons(
            store_for_contrastive,
            query=query,
            workspace_id=workspace_id or None,
            limit_successes=3,
            limit_failures=3,
        )
    except Exception:
        log.debug("Contrastive retrieval failed in get_umbrella_memory", exc_info=True)
        return {}


def _publish_recall_state_to_ctx(
    ctx: Any,
    *,
    palace_hits: list[Any],
    lesson_hits: list[Any],
    verified_ideas: list[dict[str, Any]],
) -> None:
    try:
        total_verified = (
            (len(palace_hits) if isinstance(palace_hits, list) else 0)
            + (len(lesson_hits) if isinstance(lesson_hits, list) else 0)
            + (len(verified_ideas) if isinstance(verified_ideas, list) else 0)
        )
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            view = {}
            try:
                ctx.loop_state_view = view  # type: ignore[attr-defined]
            except Exception:
                view = None
        if isinstance(view, dict):
            view["last_memory_recall_empty"] = total_verified == 0
    except Exception:
        log.debug("get_umbrella_memory live-flag publish skipped", exc_info=True)


def _canonical_memory_items(hits: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return [
        {
            "id": h.get("id", ""),
            "store": h.get("store", ""),
            "tier": h.get("tier", ""),
            "scope": h.get("scope", ""),
            "tags": h.get("tags", ""),
            "phase": h.get("phase", ""),
            "subtask_id": h.get("subtask_id", ""),
            "run_id": h.get("run_id", ""),
            "verified": bool(h.get("verified", False)),
            "content": str(h.get("content") or "")[:MEMORY_HIT_PREVIEW_CHARS],
            "source": "canonical_mempalace",
        }
        for h in hits[:limit]
    ]


def get_umbrella_memory(
    ctx: Any,
    query: str = "",
    palace_path: str = "",
    limit: int = 10,
    workspace_id: str = "",
    include_unverified: bool = False,
) -> str:
    """Query Umbrella memory via MemPalace semantic search + structured lessons."""
    try:
        if isinstance(include_unverified, str):
            include_unverified = include_unverified.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            include_unverified = bool(include_unverified)
        from umbrella.memory.paths import normalize_workspace_id

        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_id = normalize_workspace_id(workspace_id)
        workspace_id, room = _resolve_memory_query_scope(palace_path, workspace_id)
        current_run_id = _current_run_id_from_ctx(ctx)
        phase = ""
        view = getattr(ctx, "loop_state_view", None)
        if isinstance(view, dict):
            phase = str(view.get("phase_label") or "")

        canonical_lookup = _canonical_mempalace_lookup(
            repo_root,
            workspace_id=workspace_id,
            query=query,
            include_unverified=include_unverified,
            current_run_id=current_run_id,
        )
        if canonical_lookup is not None:
            canonical_hits, missing_ids = canonical_lookup
            _publish_recall_state_to_ctx(
                ctx,
                palace_hits=canonical_hits,
                lesson_hits=[],
                verified_ideas=[],
            )
            return _json(
                {
                    "palace_memory": _canonical_memory_items(canonical_hits, limit),
                    "lesson_memory": [],
                    "hierarchical_ideas": [],
                    "unverified_candidates": {
                        "palace_memory": [],
                        "lesson_memory": [],
                        "hierarchical_ideas": [],
                        "note": (
                            "Exact canonical id lookup bypasses semantic legacy "
                            "neighbors. If an id is missing, cite the primary "
                            "MemPalace id returned by palace_add."
                        ),
                    },
                    "include_unverified": bool(include_unverified),
                    "exact_lookup": {
                        "ids": [
                            item.get("id", "") for item in canonical_hits
                        ],
                        "missing_ids": missing_ids,
                        "source": "canonical_mempalace",
                    },
                    "contrastive_lessons": {},
                    "stats": _canonical_mempalace_health(repo_root, workspace_id),
                }
            )

        try:
            canonical_hits, search_health = _canonical_mempalace_search(
                repo_root,
                workspace_id=workspace_id,
                query=query,
                limit=limit,
                include_unverified=include_unverified,
                current_run_id=current_run_id,
            )
            if not search_health.get("ok"):
                return _json(
                    _empty_memory_response(
                        include_unverified=include_unverified,
                        stats=search_health,
                    )
                )
            if canonical_hits:
                trusted_hits, unverified_hits = _split_verified_first(
                    canonical_hits, current_run_id=current_run_id
                )
                palace_hits = (
                    (trusted_hits + unverified_hits)
                    if include_unverified
                    else trusted_hits
                )
                _publish_recall_state_to_ctx(
                    ctx,
                    palace_hits=palace_hits,
                    lesson_hits=[],
                    verified_ideas=[],
                )
                return _json(
                    {
                        "palace_memory": _canonical_memory_items(
                            palace_hits[:limit], limit
                        ),
                        "lesson_memory": [],
                        "hierarchical_ideas": [],
                        "unverified_candidates": {
                            "palace_memory": _canonical_memory_items(
                                unverified_hits[: min(limit, 5)],
                                min(limit, 5),
                            ),
                            "lesson_memory": [],
                            "hierarchical_ideas": [],
                            "note": (
                                "These are candidates/hypotheses. Treat them as leads, "
                                "not facts, unless you verify them in the current run."
                            ),
                        },
                        "include_unverified": bool(include_unverified),
                        "source": "canonical_mempalace",
                        "contrastive_lessons": {},
                        "stats": search_health,
                    }
                )
        except Exception:
            log.debug("canonical MemPalace search failed; falling back", exc_info=True)

        store_for_contrastive, lesson_hits, unverified_lessons = _lessons_lookup(
            repo_root,
            workspace_id=workspace_id,
            query=query,
            limit=limit,
            include_unverified=include_unverified,
            phase=phase,
        )
        idea_hits, verified_ideas, unverified_ideas = _hierarchical_ideas_lookup(
            repo_root,
            workspace_id=workspace_id,
            query=query,
            palace_path=palace_path,
            limit=limit,
            include_unverified=include_unverified,
        )
        if lesson_hits or idea_hits:
            contrastive = _contrastive_lessons_lookup(
                store_for_contrastive,
                query=query,
                workspace_id=workspace_id,
            )
            _publish_recall_state_to_ctx(
                ctx,
                palace_hits=[],
                lesson_hits=lesson_hits,
                verified_ideas=verified_ideas,
            )
            return _json(
                {
                    "palace_memory": [],
                    "lesson_memory": [
                        {
                            "id": lesson.id,
                            "workspace_id": lesson.workspace_id,
                            "change_summary": lesson.change_summary,
                            "expected_effect": lesson.expected_effect,
                            "observed_effect": lesson.observed_effect,
                            "conclusion": lesson.conclusion,
                            "tags": sorted(lesson.tags),
                        }
                        for lesson in lesson_hits[:limit]
                    ],
                    "hierarchical_ideas": idea_hits[: max(limit * 2, 20)],
                    "unverified_candidates": {
                        "palace_memory": [],
                        "lesson_memory": [],
                        "hierarchical_ideas": unverified_ideas[: min(max(limit, 5), 10)],
                    },
                    "include_unverified": bool(include_unverified),
                    "source": "jsonl_fallback",
                    "contrastive_lessons": contrastive,
                    "stats": _canonical_mempalace_health(repo_root, workspace_id),
                }
            )

        if not _legacy_palace_available():
            _publish_recall_state_to_ctx(
                ctx, palace_hits=[], lesson_hits=[], verified_ideas=[]
            )
            health = _canonical_mempalace_health(repo_root, workspace_id)
            return _json(
                _empty_memory_response(
                    include_unverified=include_unverified,
                    stats=health,
                )
            )

        palace, palace_hits, unverified_hits = _palace_lookup(
            repo_root,
            workspace_id=workspace_id,
            room=room,
            query=query,
            limit=limit,
            include_unverified=include_unverified,
            phase=phase,
            current_run_id=current_run_id,
        )
        store_for_contrastive, lesson_hits, unverified_lessons = _lessons_lookup(
            repo_root,
            workspace_id=workspace_id,
            query=query,
            limit=limit,
            include_unverified=include_unverified,
            phase=phase,
        )
        idea_hits, verified_ideas, unverified_ideas = _hierarchical_ideas_lookup(
            repo_root,
            workspace_id=workspace_id,
            query=query,
            palace_path=palace_path,
            limit=limit,
            include_unverified=include_unverified,
        )
        contrastive = _contrastive_lessons_lookup(
            store_for_contrastive,
            query=query,
            workspace_id=workspace_id,
        )
        _publish_recall_state_to_ctx(
            ctx,
            palace_hits=palace_hits,
            lesson_hits=lesson_hits,
            verified_ideas=verified_ideas,
        )

        return _json(
            {
                "palace_memory": [
                    {
                        "id": h["id"],
                        "wing": h.get("wing", ""),
                        "room": h.get("room", ""),
                        "hall": h.get("hall", ""),
                        "content": h.get("content", "")[:MEMORY_HIT_PREVIEW_CHARS],
                        "distance": round(h.get("distance", 1.0), 4),
                    }
                    for h in palace_hits[:limit]
                ],
                "lesson_memory": [
                    {
                        "id": lesson.id,
                        "workspace_id": lesson.workspace_id,
                        "change_summary": lesson.change_summary,
                        "expected_effect": lesson.expected_effect,
                        "observed_effect": lesson.observed_effect,
                        "conclusion": lesson.conclusion,
                        "tags": sorted(lesson.tags),
                    }
                    for lesson in lesson_hits[:limit]
                ],
                "hierarchical_ideas": idea_hits[: max(limit * 2, 20)],
                "unverified_candidates": {
                    "palace_memory": [
                        {
                            "id": h["id"],
                            "wing": h.get("wing", ""),
                            "room": h.get("room", ""),
                            "hall": h.get("hall", ""),
                            "content": h.get("content", "")[:1000],
                            "distance": round(h.get("distance", 1.0), 4),
                        }
                        for h in unverified_hits[: min(limit, 5)]
                    ],
                    "lesson_memory": [
                        {
                            "id": lesson.id,
                            "workspace_id": lesson.workspace_id,
                            "change_summary": lesson.change_summary,
                            "conclusion": lesson.conclusion,
                            "priority": getattr(lesson, "priority", 0),
                            "tags": sorted(lesson.tags),
                        }
                        for lesson in unverified_lessons[: min(limit, 5)]
                    ],
                    "hierarchical_ideas": unverified_ideas[: min(max(limit, 5), 10)],
                    "note": (
                        "These are candidates/hypotheses. Treat them as leads, "
                        "not facts, unless you verify them in the current run."
                    ),
                },
                "include_unverified": bool(include_unverified),
                "contrastive_lessons": contrastive,
                "stats": palace.stats(),
            }
        )
    except Exception as e:
        log.error("Memory query failed: %s", e, exc_info=True)
        return f"WARNING: memory error: {e}"


def list_memory_tree(ctx: Any, workspace_id: str = "") -> str:
    """Return ideas JSONL stats (count per kind) for a workspace or manager root."""
    try:
        from umbrella.memory.paths import manager_memory_root, workspace_memory_root

        repo_root = _resolve_umbrella_repo_root(ctx)
        if workspace_id.strip():
            root = workspace_memory_root(repo_root, workspace_id)
        else:
            root = manager_memory_root(repo_root)
        ideas_path = root / "ideas.jsonl"
        tree: dict[str, int] = {}
        if ideas_path.exists():
            with ideas_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        path = rec.get("palace_path") or rec.get("kind") or "unknown"
                        tree[path] = tree.get(path, 0) + 1
                    except Exception:
                        pass
        return _json(
            {
                "workspace_id": workspace_id,
                "memory_root": str(root),
                "tree": tree,
                "total": sum(tree.values()),
            }
        )
    except Exception as e:
        log.error("list_memory_tree failed: %s", e, exc_info=True)
        return f"WARNING: list_memory_tree error: {e}"


def save_umbrella_memory(
    ctx: Any,
    palace_path: str,
    title: str,
    content: str,
    kind: str = "observation",
    workspace_id: str = "",
    tags: str = "",
    metadata_extra: dict[str, Any] | None = None,
) -> str:
    """Save memory: canonical MemPalace first, legacy backend optional mirror."""
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)

        from umbrella.memory.paths import normalize_workspace_id, parse_palace_path_hint

        workspace_id = normalize_workspace_id(workspace_id)
        ws_hint, event_type, room = parse_palace_path_hint(
            palace_path,
            workspace_id=workspace_id,
            default_kind=kind,
        )
        if ws_hint:
            workspace_id = ws_hint

        task_id = str(getattr(ctx, "task_id", "") or "")
        metadata_extra = dict(metadata_extra or {})
        run_id = _run_id_from_task_id(task_id)
        if run_id:
            metadata_extra.setdefault("run_id", run_id)
        tag_list = _split_tags(tags)
        policy_issues = memory_write_policy_issues(
            kind=kind,
            tags=tag_list,
            metadata=metadata_extra,
        )
        if policy_issues:
            return _json(
                {
                    "saved": False,
                    "status": "blocked",
                    "reason": "evidence_bound_memory",
                    "issues": policy_issues,
                    "next_step": (
                        "Record this as observation/candidate memory, or include "
                        "evidence refs such as source_id, tool_result_id, "
                        "artifact_id, verify_run_id, or ledger_event_id before "
                        "promoting it to durable/manager/competency memory."
                    ),
                }
            )

        from umbrella.memory.kernel.models import memory_event_from_tool_write
        from umbrella.memory.kernel.writer import write_memory_event

        store = "palace.durable" if str(kind or "").lower() == "durable" else "palace.idea"
        existing_id = str(metadata_extra.get("canonical_id") or "").strip()
        mem_body = content or title or ""
        mem_content = f"[{title}]\n{mem_body}" if title else mem_body
        write_meta = {
            **metadata_extra,
            "room": room,
            "palace_store": store,
        }
        if existing_id:
            write_meta["canonical_id"] = existing_id
        event = memory_event_from_tool_write(
            content=mem_content,
            title=title,
            memory_kind=kind,
            workspace_id=workspace_id,
            tags=tag_list,
            scope=str(metadata_extra.get("scope") or "run_scoped"),
            tier=str(metadata_extra.get("tier") or "warm"),
            phase_id=str(metadata_extra.get("phase") or ""),
            run_id=run_id or "",
            source_path=str(
                metadata_extra.get("source_path") or room or "save_umbrella_memory"
            ),
            trust_level=str(metadata_extra.get("trust_level") or "agent_claim"),
            verified=bool(metadata_extra.get("verified", False)),
            evidence_refs=metadata_extra.get("evidence_refs") or [],
            metadata=write_meta,
        )
        write_result = write_memory_event(
            repo_root,
            event,
            workspace_id=workspace_id,
            skip_if_exists=bool(existing_id),
            mirror_legacy=_legacy_palace_available(),
        )
        if write_result.skipped_duplicate or write_result.saved:
            from umbrella.memory.palace_backend import _workspace_to_wing

            canonical_id = write_result.canonical_id
            payload: dict[str, Any] = {
                "saved": True,
                "canonical_id": canonical_id,
                "id": canonical_id,
                "store": write_result.store or store,
                "room": room,
                "wing": _workspace_to_wing(workspace_id) if workspace_id else "",
            }
            if store == "palace.durable":
                payload["durable_store"] = store
                payload["durable_node_id"] = canonical_id
            return _json(payload)
        if write_result.policy_issues:
            return _json(
                {
                    "saved": False,
                    "status": "blocked",
                    "reason": "evidence_bound_memory",
                    "issues": list(write_result.policy_issues),
                }
            )

        if not _legacy_palace_available():
            return _json(
                {
                    "saved": False,
                    "status": "error",
                    "reason": "canonical_memory_unavailable",
                }
            )

        palace = _palace_backend(repo_root, workspace_id)
        result = palace.add(
            workspace_id=workspace_id,
            event_type=event_type,
            room=room,
            title=title,
            content=content,
            kind=kind,
            tags=tag_list or None,
            task_id=task_id,
            metadata_extra=metadata_extra or None,
        )
        return _json({"saved": True, **result})
    except Exception as e:
        return f"WARNING: save memory error: {e}"


def record_workspace_event(
    ctx: Any,
    workspace_id: str,
    event_type: str,
    summary: str,
    details: str = "",
    severity: str = "info",
    tags: str = "",
) -> str:
    content = f"{summary.strip()}\n\n{details.strip()}".strip()
    return save_umbrella_memory(
        ctx,
        palace_path=f"workspaces/{workspace_id}/{event_type or 'events'}",
        title=summary[:180] or event_type,
        content=content,
        kind=severity or "info",
        workspace_id=workspace_id,
        tags=tags or event_type,
    )


_RECORD_IDEA_VALID_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"hypothesis", "observation_from_log", "verified_outcome"}
)


def record_idea(
    ctx: Any,
    content: str = "",
    tags: str = "",
    workspace_id: str = "",
    kind: str = "",
    title: str = "",
    body: str = "",
    palace_path: str = "",
    evidence_kind: str = "",
) -> str:
    """Record a structured idea/observation in workspace hierarchical memory.

    Tier 2.1 — write-time hygiene:

    - ``kind="lesson"`` is **rejected**. Lessons must go through
      ``save_umbrella_lesson`` so they get verify-run-id binding,
      priority/tags reflecting verification status, and proper recall
      surfacing. ``record_idea`` is for hypotheses and observations.
    - New parameter ``evidence_kind`` documents how the idea was obtained:
      ``hypothesis`` (default — agent guess), ``observation_from_log``
      (saw it in a tool output), or ``verified_outcome`` (post-verify
      confirmation). Only ``verified_outcome`` ideas are mirrored to
      semantic palace; hypotheses stay in hierarchical JSONL so they
      don't pollute recall search results.
    - Unknown ``evidence_kind`` values are normalised to ``hypothesis``
      with a warning in the response payload so the agent can correct.
    """

    try:
        from umbrella.memory.paths import normalize_workspace_id, parse_palace_path_hint

        repo_root = _resolve_umbrella_repo_root(ctx)
        ws = normalize_workspace_id(
            workspace_id or _current_workspace_id_from_drive(ctx)
        )
        if not ws:
            return "ERROR: workspace_id is required or must be present in drive state"
        root = _workspace_memory_root(repo_root, ws, ctx)
        root.mkdir(parents=True, exist_ok=True)
        idea_body = str(body or content or "").strip()
        if not idea_body:
            return "ERROR: content or body is required"

        kind_norm = (
            re.sub(r"[^a-z0-9_-]+", "_", str(kind or "idea").strip().lower()).strip("_")
            or "idea"
        )
        if kind_norm == "lesson":
            return (
                "ERROR: record_idea does not accept kind='lesson'. "
                "Use `save_umbrella_lesson(workspace_id=..., change_summary=..., "
                "expected_effect=..., observed_effect=..., verification_passed=..., "
                "verify_run_id=...)` instead. record_idea is for hypotheses and "
                "observations; lessons carry verification-status invariants and "
                "must go through the lesson path so they rank correctly in recall."
            )
        evidence_kind_norm = str(evidence_kind or "").strip().lower()
        warning: str = ""
        if (
            evidence_kind_norm
            and evidence_kind_norm not in _RECORD_IDEA_VALID_EVIDENCE_KINDS
        ):
            warning = (
                f"evidence_kind={evidence_kind_norm!r} is not one of "
                f"{sorted(_RECORD_IDEA_VALID_EVIDENCE_KINDS)}; "
                "recorded as 'hypothesis'."
            )
            evidence_kind_norm = "hypothesis"
        if not evidence_kind_norm:
            evidence_kind_norm = "hypothesis"

        title_text = str(title or "").strip()
        if not title_text:
            first_line = (
                idea_body.splitlines()[0].strip() if idea_body.splitlines() else ""
            )
            title_text = first_line[:120] or f"{kind_norm} idea"
        tag_list = _split_tags(tags)
        for extra in ("idea", kind_norm, f"evidence:{evidence_kind_norm}"):
            if extra and extra not in tag_list:
                tag_list.append(extra)
        if evidence_kind_norm != "verified_outcome":
            # Mark unverified content so recall ranking can de-prioritise.
            for extra in ("candidate", "unverified"):
                if extra not in tag_list:
                    tag_list.append(extra)

        _ws_hint, _event, logical = parse_palace_path_hint(
            palace_path,
            workspace_id=ws,
            default_kind=kind_norm,
        )
        if _ws_hint:
            ws = _ws_hint
        hier_path = logical or f"ideas/{kind_norm}"

        # Write directly to ideas.jsonl (replaces HierarchicalMemory.add)
        import uuid as _uuid_mod
        record_id = str(_uuid_mod.uuid4())
        ideas_path = root / "ideas.jsonl"
        idea_record = {
            "id": record_id,
            "palace_path": hier_path,
            "title": title_text,
            "content": idea_body,
            "kind": kind_norm,
            "workspace_id": ws,
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            "tags": tag_list,
            "evidence_kind": evidence_kind_norm,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            ideas_path.parent.mkdir(parents=True, exist_ok=True)
            with ideas_path.open("a", encoding="utf-8") as _f:
                _f.write(json.dumps(idea_record, ensure_ascii=False) + "\n")
        except Exception as _e:
            log.debug("record_idea JSONL write failed: %s", _e)

        palace_result: dict[str, Any] = {}
        # Mirror to semantic palace only after verification confirms the
        # outcome. Hypotheses and log observations stay in JSONL only so they
        # don't pollute semantic recall.
        if evidence_kind_norm == "verified_outcome":
            try:
                palace_result = canonical_palace_add(
                    repo_root,
                    workspace_id=ws,
                    content=idea_body,
                    title=title_text,
                    kind=kind_norm,
                    store="palace.idea",
                    tags=tag_list,
                    phase=str(
                        (getattr(ctx, "loop_state_view", None) or {}).get("phase_label", "")
                    ),
                    run_id=_run_id_from_task_id(str(getattr(ctx, "task_id", "") or "")) or None,
                    source_path=hier_path,
                    extra={
                        "room": f"ideas-{kind_norm}",
                        "idea_id": record_id,
                        "palace_path": hier_path,
                        "evidence_kind": evidence_kind_norm,
                    },
                )
            except Exception:
                log.debug("record_idea semantic mirror skipped", exc_info=True)

        payload: dict[str, Any] = {
            "saved": True,
            "workspace_id": ws,
            "path": str(ideas_path),
            "id": record_id,
            "palace_path": hier_path,
            "evidence_kind": evidence_kind_norm,
            "mirrored_to_semantic": bool(
                palace_result.get("saved") or palace_result.get("canonical_id")
            ),
            "semantic_memory": palace_result,
        }
        if warning:
            payload["warning"] = warning
        return _json(payload)
    except Exception as e:
        log.error("record_idea failed: %s", e, exc_info=True)
        return f"WARNING: record idea error: {e}"


def update_prompt(
    ctx: Any,
    name: str,
    new_content: str,
    reason: str,
    workspace_id: str = "",
) -> str:
    """Update the workspace-scoped prompt overlay, never the repo seed prompt."""
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        ws = workspace_id or _current_workspace_id_from_drive(ctx)
        if not ws:
            return "ERROR: workspace_id is required or must be present in drive state"
        prompt_name = _resolve_prompt_name(name)
        prompt_dir = _workspace_memory_root(repo_root, ws, ctx) / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        path = prompt_dir / _PROMPT_NAME_TO_FILE[prompt_name]
        old_content = path.read_text(encoding="utf-8") if path.exists() else ""
        text = str(new_content or "")
        if not text.strip():
            return "ERROR: new_content must not be empty"
        path.write_text(text, encoding="utf-8")

        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                old_content.splitlines(),
                text.splitlines(),
                fromfile=f"{prompt_name}.old",
                tofile=f"{prompt_name}.new",
                lineterm="",
            )
        )
        log_path = (
            Path(getattr(ctx, "drive_root", prompt_dir.parent / "drive"))
            / "logs"
            / "prompt_changes.jsonl"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "task_id": str(getattr(ctx, "task_id", "") or ""),
                        "workspace_id": ws,
                        "name": prompt_name,
                        "reason": reason,
                        "path": str(path),
                        "diff": diff[:20000],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return _json(
            {
                "updated": True,
                "workspace_id": ws,
                "name": prompt_name,
                "path": str(path),
            }
        )
    except Exception as e:
        log.error("update_prompt failed: %s", e, exc_info=True)
        return f"WARNING: update prompt error: {e}"


_PYTHON_EVAL_FORBIDDEN_IMPORTS = {"subprocess", "shutil"}
_PYTHON_EVAL_FORBIDDEN_CALLS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("os", "replace"),
    ("os", "rename"),
}


def _python_eval_guard(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [
                alias.name.split(".", 1)[0] for alias in getattr(node, "names", [])
            ]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".", 1)[0])
            blocked = sorted(set(names).intersection(_PYTHON_EVAL_FORBIDDEN_IMPORTS))
            if blocked:
                return f"blocked import(s): {', '.join(blocked)}"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = ""
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = str(kw.value.value)
                if any(ch in (mode or "r") for ch in "wax+"):
                    return "open(..., write/append/create mode) is blocked"
            if isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name
            ):
                pair = (node.func.value.id, node.func.attr)
                if pair in _PYTHON_EVAL_FORBIDDEN_CALLS:
                    return f"{pair[0]}.{pair[1]} is blocked"
    return ""


def python_eval(
    ctx: Any,
    code: str,
    timeout_seconds: int = 30,
    workspace_id: str = "",
) -> str:
    """Run guarded Python code from a string in workspace .memory/drive/tmp."""
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        ws = workspace_id or _current_workspace_id_from_drive(ctx)
        if not ws:
            return "ERROR: workspace_id is required or must be present in drive state"
        if stop_payload := _stop_requested_block(
            ctx, tool_name="python_eval", workspace_id=ws
        ):
            return _json(stop_payload)
        reason = _python_eval_guard(str(code or ""))
        if reason:
            return f"ERROR: python_eval guard rejected code: {reason}"
        tmp_dir = _workspace_memory_root(repo_root, ws, ctx) / "drive" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        script_path = (
            tmp_dir / f"eval_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.py"
        )
        script_path.write_text(str(code or ""), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(_workspace_root(repo_root, ws, ctx)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, min(int(timeout_seconds or 30), 120)),
            check=False,
        )
        return _json(
            {
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-8000:],
                "stderr": proc.stderr[-8000:],
                "script_path": str(script_path),
            }
        )
    except subprocess.TimeoutExpired as exc:
        return _json(
            {
                "exit_code": None,
                "error": "timeout",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
            }
        )
    except Exception as e:
        log.error("python_eval failed: %s", e, exc_info=True)
        return f"WARNING: python_eval error: {e}"


def save_umbrella_lesson(
    ctx: Any,
    workspace_id: str,
    change_summary: str,
    expected_effect: str,
    observed_effect: str = "",
    tags: str = "",
    candidate_id: str = "",
    raw_evidence_paths: list[str] | None = None,
    verification_passed: bool = False,
    critic_verdict: str = "",
    verify_run_id: str = "",
    failed_step_count: int = 0,
) -> str:
    """Record a workspace lesson bound to a verify run.

    Tier 2.2 — lessons are first-class verified knowledge. To be marked
    ``verified=True`` (priority 5, recall-eligible) the caller must
    supply ALL of:

    - ``verification_passed=True`` AND ``critic_verdict='pass'``
      (legacy contract — preserved),
    - ``verify_run_id`` — the id of the ``run_workspace_verify`` call
      that backed this lesson (or a stable identifier the operator can
      look up in the ``verify_runs`` palace later),
    - ``failed_step_count == 0`` — a lesson that claims success while
      verify reports failing required steps is incoherent.

    Lessons missing ``verify_run_id`` are demoted to priority 1 and
    tagged ``unverified_lesson`` even if the agent ticked the boolean
    flag. This breaks the pattern where multiple contradictory
    "lessons" pile up around a single verifier mismatch.
    """

    try:
        from umbrella.memory.models import WorkspaceLessonRecord, generate_lesson_id

        repo_root = _resolve_umbrella_repo_root(ctx)
        store = _memory_store(repo_root, workspace_id)
        verify_run_id_norm = str(verify_run_id or "").strip()
        if not verify_run_id_norm:
            view = getattr(ctx, "loop_state_view", None) or {}
            view_run_id = (
                view.get("last_verify_run_id") if isinstance(view, dict) else ""
            )
            view_passed = (
                view.get("last_verify_passed") if isinstance(view, dict) else False
            )
            view_failed = (
                view.get("last_verify_failed_count") if isinstance(view, dict) else 0
            )
            if (
                isinstance(view_run_id, str)
                and view_run_id.strip()
                and bool(view_passed)
                and int(view_failed or 0) == 0
            ):
                verify_run_id_norm = view_run_id.strip()
        verified_inputs = (
            bool(verification_passed) and str(critic_verdict).strip().lower() == "pass"
        )
        try:
            failed_count = max(0, int(failed_step_count))
        except (TypeError, ValueError):
            failed_count = 0
        verified = verified_inputs and bool(verify_run_id_norm) and failed_count == 0
        normalized_observed = observed_effect.strip() or (
            "Verified" if verified else "Unverified / avoid until proven"
        )
        tags_set = set(_split_tags(tags))
        metadata = {
            "source": "save_umbrella_lesson_tool",
            "verified_at": datetime.now(timezone.utc).isoformat() if verified else "",
            "evidence_sha": "",
            "critic_verdict": critic_verdict,
            "verification_passed": bool(verification_passed),
            "verify_run_id": verify_run_id_norm,
            "failed_step_count": failed_count,
        }
        evidence_blob = "\n".join(
            [
                change_summary,
                expected_effect,
                normalized_observed,
                *(raw_evidence_paths or []),
            ]
        )
        import hashlib

        metadata["evidence_sha"] = hashlib.sha256(
            evidence_blob.encode("utf-8", errors="replace")
        ).hexdigest()
        downgrade_reason = ""
        if not verified:
            tags_set.update({"avoid", "unverified_lesson"})
            if not verified_inputs:
                downgrade_reason = "verification_passed/critic_verdict not both true"
            elif not verify_run_id_norm:
                downgrade_reason = "verify_run_id missing"
            elif failed_count > 0:
                downgrade_reason = f"failed_step_count={failed_count} > 0"
            metadata["unverified_reason"] = downgrade_reason
        lesson = WorkspaceLessonRecord(
            id=generate_lesson_id(),
            task_id=str(getattr(ctx, "task_id", "") or "ouroboros_task"),
            workspace_id=workspace_id,
            change_summary=change_summary,
            expected_effect=expected_effect,
            observed_effect=normalized_observed,
            conclusion=(
                normalized_observed
                if verified
                else f"AVOID relying on this lesson until verification+critic pass: {normalized_observed}"
            ),
            evidence_summary=(
                f"Verified by runtime verification and critic (verify_run_id={verify_run_id_norm})"
                if verified
                else (
                    "Unverified lesson recorded as AVOID — "
                    + (downgrade_reason or "verification+critic evidence missing")
                )
            ),
            tags=tags_set,
            avoid_tags=[] if verified else ["unverified_lesson"],
            priority=5 if verified else 1,
            candidate_id=candidate_id or None,
            raw_evidence_paths=list(raw_evidence_paths or []),
            metadata=metadata,
        )
        store.add_lesson(lesson)
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="lessons",
            summary=change_summary,
            details=f"Expected: {expected_effect}\nObserved: {normalized_observed}",
            severity="lesson",
            tags=",".join(sorted(tags_set)),
        )
        try:
            from umbrella.memory.palace.facade import MemPalace

            lesson_content_str = (
                f"{lesson.change_summary} | expected:{lesson.expected_effect}"
                f" | observed:{lesson.observed_effect}"
            )
            _palace = MemPalace(repo_root, workspace_id)
            _palace.add(
                store="palace.lesson",
                content=lesson_content_str,
                tier="warm",
                scope="cross_run_durable",
                tags=(
                    ["lesson", "verified"]
                    if verified
                    else ["lesson", "unverified_lesson", "avoid"]
                ),
                verified=verified,
                phase="verify",
                run_id=verify_run_id_norm,
                extra={
                    "lesson_id": lesson.id,
                    "verification_passed": bool(verification_passed),
                    "critic_verdict": critic_verdict,
                    "failed_step_count": failed_count,
                    "unverified_reason": downgrade_reason,
                },
            )
        except Exception:
            pass
        return _json(
            {
                "saved": True,
                "lesson_id": lesson.id,
                "verified": verified,
                "verify_run_id": verify_run_id_norm,
                "downgrade_reason": downgrade_reason or None,
            }
        )
    except Exception as e:
        log.error("Save lesson failed: %s", e, exc_info=True)
        return f"WARNING: save lesson error: {e}"
