"""Bridge Umbrella host context into the Ouroboros drive."""

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - fallback for <3.11
    import tomli as _toml  # type: ignore[no-redef]

from umbrella.config import OUROBOROS_BRIDGE_TEXT_PREVIEW_LIMIT
from umbrella.memory.models import LessonType, MemoryConfig, MemoryQuery
from umbrella.memory.store import MemoryStore
from umbrella.skills import (
    Domain,
    detect_task_domains,
    summarize_domains,
)

log = logging.getLogger(__name__)

_DEFAULT_STATE = {
    "spent_usd": 0.0,
    "openrouter_total_usd": 0.0,
    "budget_drift_alert": False,
    "budget_drift_pct": 0.0,
}


def resolve_ouroboros_repo_root(repo_root: Path) -> Path:
    """Return the standalone Ouroboros repository root inside the monorepo."""
    return (repo_root.resolve() / "ouroboros").resolve()


def safe_workspace_segment(workspace_id: str | None) -> str:
    from umbrella.memory.paths import _safe_workspace_segment

    return _safe_workspace_segment(workspace_id or "")


def workspace_drive_root(repo_root: Path, workspace_id: str | None) -> Path:
    """Return the workspace-scoped Ouroboros drive root when a workspace exists."""
    seg = safe_workspace_segment(workspace_id)
    if not seg:
        return (repo_root.resolve() / ".umbrella" / "ouroboros_drive").resolve()
    return (repo_root.resolve() / "workspaces" / seg / ".memory" / "drive").resolve()


_PROMPT_SEEDS: dict[str, str] = {
    "SYSTEM": "prompts/SYSTEM.md",
    "BIBLE": "BIBLE.md",
    "CONSCIOUSNESS": "prompts/CONSCIOUSNESS.md",
}


_LEGACY_PROMPT_MARKERS = (
    "identity.md",
    "update_identity",
    "Telegram",
    "Google Colab",
    "MyDrive/Ouroboros",
)


def _workspace_prompt_is_stale(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in text for marker in _LEGACY_PROMPT_MARKERS)


def seed_workspace_prompts(repo_root: Path, workspace_id: str | None) -> Path | None:
    """Copy canonical Ouroboros prompts into the workspace overlay.

    Existing overlays are preserved unless they are known stale seed copies that
    still contain standalone Ouroboros/legacy-memory instructions.
    """
    seg = safe_workspace_segment(workspace_id)
    if not seg:
        return None
    repo_root = repo_root.resolve()
    ouroboros_root = resolve_ouroboros_repo_root(repo_root)
    prompt_dir = repo_root / "workspaces" / seg / ".memory" / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for name, rel in _PROMPT_SEEDS.items():
        dest = prompt_dir / f"{name}.md"
        if dest.exists() and not _workspace_prompt_is_stale(dest):
            continue
        src = ouroboros_root / rel
        if src.exists():
            shutil.copyfile(src, dest)
    return prompt_dir


def ensure_drive_layout(drive_root: Path) -> None:
    """Create the minimum drive layout expected by Ouroboros."""
    drive_root = drive_root.resolve()
    for rel in (
        "logs",
        "memory",
        "memory/knowledge",
        "state",
        "task_results",
    ):
        (drive_root / rel).mkdir(parents=True, exist_ok=True)

    state_path = drive_root / "state" / "state.json"
    if not state_path.exists():
        state_path.write_text(
            json.dumps(_DEFAULT_STATE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def sync_umbrella_context_to_drive(
    repo_root: Path,
    drive_root: Path,
    *,
    workspace_id: str | None = None,
    task_input: str | None = None,
    task_id: str | None = None,
    user_message: str | None = None,
    memory_payload: dict[str, Any] | None = None,
) -> None:
    """Refresh Umbrella-derived state and knowledge inside the Ouroboros drive."""
    repo_root = repo_root.resolve()
    drive_root = drive_root.resolve()
    ensure_drive_layout(drive_root)
    seed_workspace_prompts(repo_root, workspace_id)
    try:
        from umbrella.memory.paths import palace_path_for

        palace_path = palace_path_for(repo_root, workspace_id or "")
    except Exception:
        palace_path = repo_root / ".umbrella" / "palace"

    try:
        _write_state_snapshot(
            repo_root=repo_root,
            drive_root=drive_root,
            workspace_id=workspace_id,
            task_input=task_input,
            task_id=task_id,
            user_message=user_message,
            memory_payload=memory_payload,
        )

        knowledge_dir = drive_root / "memory" / "knowledge"
        _write_optional_markdown(
            knowledge_dir / "umbrella_memory.md",
            _build_umbrella_memory_summary(
                repo_root=repo_root,
                workspace_id=workspace_id,
                task_input=task_input,
                user_message=user_message,
                memory_payload=memory_payload,
            ),
        )
        _write_optional_markdown(
            knowledge_dir / "umbrella_retrieval.md",
            _build_retrieval_summary(
                repo_root=repo_root,
                task_input=task_input,
                user_message=user_message,
            ),
        )
        _write_optional_markdown(
            knowledge_dir / "meta_harness_experience.md",
            _build_meta_harness_summary(repo_root=repo_root),
        )
        _sync_active_skill_packs(
            repo_root=repo_root,
            drive_root=drive_root,
            knowledge_dir=knowledge_dir,
            workspace_id=workspace_id,
            task_input=task_input,
            user_message=user_message,
        )
        _rebuild_knowledge_index(knowledge_dir)
    finally:
        try:
            from umbrella.memory.palace_backend import clear_palace_backend_cache

            clear_palace_backend_cache(palace_path)
        except Exception:
            log.debug(
                "Failed to clear palace backend cache after drive sync", exc_info=True
            )


def _write_state_snapshot(
    *,
    repo_root: Path,
    drive_root: Path,
    workspace_id: str | None,
    task_input: str | None,
    task_id: str | None,
    user_message: str | None,
    memory_payload: dict[str, Any] | None,
) -> None:
    state_path = drive_root / "state" / "state.json"
    try:
        state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = dict(_DEFAULT_STATE)

    state.setdefault("spent_usd", 0.0)
    state.setdefault("openrouter_total_usd", 0.0)
    state.setdefault("budget_drift_alert", False)
    state.setdefault("budget_drift_pct", 0.0)
    state["host_repo_root"] = str(repo_root)
    state["ouroboros_repo_root"] = str(resolve_ouroboros_repo_root(repo_root))
    state["umbrella_memory_root"] = str(repo_root / ".umbrella" / "memory")
    state["last_umbrella_sync"] = _utc_now_iso()
    state["current_task"] = {
        "id": task_id or "",
        "workspace_id": workspace_id or "",
        "task_input": (task_input or "")[:OUROBOROS_BRIDGE_TEXT_PREVIEW_LIMIT],
        "user_message": (user_message or "")[:OUROBOROS_BRIDGE_TEXT_PREVIEW_LIMIT],
        "memory": _sanitize_memory_payload(memory_payload),
    }

    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_umbrella_memory_summary(
    *,
    repo_root: Path,
    workspace_id: str | None,
    task_input: str | None,
    user_message: str | None,
    memory_payload: dict[str, Any] | None,
) -> str:
    memory_root = repo_root / ".umbrella" / "memory"
    store = MemoryStore(_memory_config(memory_root))
    stats = store.get_stats()

    workspace_lessons = (
        store.query_lessons(
            MemoryQuery(
                workspace_id=workspace_id,
                lesson_type=LessonType.WORKSPACE,
                limit=12,
                include_stale=False,
            )
        )
        if workspace_id
        else []
    )
    manager_lessons = store.query_lessons(
        MemoryQuery(
            lesson_type=LessonType.MANAGER,
            limit=12,
            include_stale=False,
        )
    )

    query_text = task_input or workspace_id or ""
    selected_workspace_lessons = _rank_lessons(
        workspace_lessons,
        query_text=query_text,
        limit=4,
    )
    selected_manager_lessons = _rank_lessons(
        manager_lessons,
        query_text=query_text,
        limit=4,
    )

    lines = [
        "# Umbrella Memory Bridge",
        "",
        f"Generated: {_utc_now_iso()}",
        f"Workspace focus: {workspace_id or 'auto'}",
        f"Task hint: {(task_input or '').strip()[:240] or '(none)'}",
        "",
        "## Memory Stats",
        f"- Total lessons: {stats.total_lessons}",
        f"- Workspace lessons: {stats.workspace_lessons}",
        f"- Manager lessons: {stats.manager_lessons}",
        f"- Active gaps: {stats.active_gaps}",
        f"- Signals: {stats.total_signals}",
        "- Palace backend: MemPalace (ChromaDB semantic search)",
        "",
        "## Workspace Lessons",
    ]

    if selected_workspace_lessons:
        for lesson in selected_workspace_lessons:
            lines.append(_format_lesson_bullet(lesson))
    else:
        lines.append("- No workspace-specific lessons yet.")

    lines.extend(["", "## Manager Lessons"])
    if selected_manager_lessons:
        for lesson in selected_manager_lessons:
            lines.append(_format_lesson_bullet(lesson))
    else:
        lines.append("- No manager-level lessons yet.")

    gaps = store.get_active_gaps()
    lines.extend(["", "## Active Gaps"])
    if gaps:
        for gap in gaps[:5]:
            lines.append(f"- [{gap.severity}] {gap.capability_area}: {gap.description}")
    else:
        lines.append("- No active competency gaps.")

    lines.extend(["", "## Palace Memory (MemPalace semantic search)"])
    lines.extend(
        _format_palace_memory(
            repo_root, workspace_id=workspace_id, task_input=task_input
        )
    )

    live_context_lines = _format_live_context_section(
        user_message=user_message,
        memory_payload=memory_payload,
    )
    if live_context_lines:
        lines.extend(["", "## Live Umbrella Task Context", *live_context_lines])

    return "\n".join(lines).strip()


def _format_canonical_hits(hits: list[dict]) -> list[str]:
    if not hits:
        return ["- No palace memories yet."]
    lines: list[str] = []
    for hit in hits:
        store = str(hit.get("store") or "palace")
        phase = str(hit.get("phase") or "")
        content = str(hit.get("content") or "")[:240]
        score = hit.get("score")
        score_str = f" (score={score:.2f})" if isinstance(score, (int, float)) else ""
        label = f"{store}/{phase}" if phase else store
        lines.append(f"- [{label}]{score_str} {content}")
    return lines


def _format_palace_memory(
    repo_root: Path,
    *,
    workspace_id: str | None,
    task_input: str | None,
) -> list[str]:
    query = task_input or workspace_id or ""
    try:
        from umbrella.memory.palace.facade import MemPalace

        palace = MemPalace(repo_root, workspace_id or None)
        try:
            health = palace.health()
            if not health.get("ok"):
                return ["- Memory unavailable (canonical backend not ready)."]
            if query.strip():
                hits = palace.search(query, n=8)
            else:
                hits = palace.list_all(n=8)
        finally:
            palace.close()
        if hits:
            return _format_canonical_hits(hits)
    except Exception as exc:
        log.warning("Canonical MemPalace bridge error: %s", exc, exc_info=True)

    try:
        from umbrella.memory.paths import palace_path_for
        from umbrella.memory.palace_backend import get_palace_backend

        palace_path = palace_path_for(repo_root, workspace_id or "")
    except Exception:
        return ["- No palace memories yet."]
    if not palace_path.exists():
        return ["- No palace memories yet."]

    try:
        palace = get_palace_backend(palace_path)
        try:
            if query.strip():
                hits = palace.search(query, workspace_id=workspace_id or "", n_results=8)
            else:
                hits = palace.recent(workspace_id=workspace_id or "", limit=8)
        finally:
            palace.close()
        if not hits:
            return ["- No palace memories yet."]
        lines = []
        for h in hits:
            wing = h.get("wing", "")
            room = h.get("room", "")
            content = h.get("content", "")[:240]
            dist = h.get("distance")
            dist_str = f" (d={dist:.2f})" if isinstance(dist, (int, float)) else ""
            lines.append(f"- [{wing}/{room}]{dist_str} {content}")
        return lines
    except Exception as exc:
        log.warning("Legacy MemPalace bridge error: %s", exc, exc_info=True)
        return ["- Memory unavailable (see logs)."]


def _build_meta_harness_summary(*, repo_root: Path) -> str:
    """Meta-harness was removed in the PhaseRunner refactor; return empty."""
    return ""


def _build_retrieval_summary(
    *,
    repo_root: Path,
    task_input: str | None,
    user_message: str | None,
) -> str:
    query = (task_input or user_message or "").strip()
    if not query:
        return ""

    try:
        from umbrella.retrieval.service import RetrievalService

        card = RetrievalService(repo_root).search(query, max_results=5)
    except Exception:
        log.debug("Failed to build Umbrella retrieval bridge", exc_info=True)
        return ""

    lines = [
        "# Umbrella Retrieval Bridge",
        "",
        f"Generated: {_utc_now_iso()}",
        f"Query: {query[:300]}",
        "",
        f"Recommended pattern: {card.recommended_pattern}",
        f"Confidence: {card.confidence:.2f}",
    ]
    if card.key_symbols:
        lines.append("Key symbols: " + ", ".join(card.key_symbols[:8]))
    if card.key_files:
        lines.append(
            "Key files: " + ", ".join(str(path) for path in card.key_files[:8])
        )
    if card.anti_patterns:
        lines.append("Avoid: " + " | ".join(card.anti_patterns[:4]))
    if card.example_usage:
        lines.append("Examples: " + " | ".join(card.example_usage[:3]))

    return "\n".join(lines).strip()


def _rank_lessons(lessons: list[Any], *, query_text: str, limit: int) -> list[Any]:
    query_tokens = set(_tokenize(query_text))

    def sort_key(lesson: Any) -> tuple[int, int, float]:
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
        lesson_tokens = set(_tokenize(haystack))
        overlap = len(query_tokens.intersection(lesson_tokens))
        return (
            overlap,
            int(getattr(lesson, "priority", 0) or 0),
            float(getattr(lesson, "created_at", 0.0) or 0.0),
        )

    return sorted(lessons, key=sort_key, reverse=True)[:limit]


def _format_lesson_bullet(lesson: Any) -> str:
    conclusion = str(getattr(lesson, "conclusion", "") or "").strip()
    observed = str(getattr(lesson, "observed_effect", "") or "").strip()
    if not conclusion:
        conclusion = observed or "No conclusion recorded yet."
    tags = sorted(getattr(lesson, "tags", set()) or set())
    suffix = f" | tags: {', '.join(tags[:5])}" if tags else ""
    scope = getattr(lesson, "workspace_id", None) or lesson.lesson_type
    return f"- [{scope}] {lesson.change_summary} -> {conclusion[:240]}{suffix}"


def _write_optional_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if content.strip():
        path.write_text(content.strip() + "\n", encoding="utf-8")


def _read_workspace_task_main(repo_root: Path, workspace_id: str | None) -> str:
    """Best-effort read of ``workspaces/<id>/TASK_MAIN.md`` for skill detection.

    Returns an empty string when the workspace id is missing or the file
    can't be read; skill detection then falls back to ``task_input`` /
    ``user_message`` only.
    """
    if not workspace_id:
        return ""
    candidate = repo_root / "workspaces" / workspace_id / "TASK_MAIN.md"
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _workspace_skill_signal(
    repo_root: Path, workspace_id: str | None
) -> tuple[str, set[Domain], list[str]]:
    """Return deterministic workspace-local hints for skill detection.

    Workspace policy contributes durable hints, but the current task can
    override a stale opt-out when it explicitly requires LLM/model/agent
    work. In this repo LLM-backed agents should be built on the in-repo
    GMAS framework, even when the workspace seed forgot to opt in.
    """
    if not workspace_id:
        return "", set(), []
    try:
        seg = safe_workspace_segment(workspace_id)
    except ValueError:
        return "", set(), []
    workspace_root = repo_root / "workspaces" / seg
    parts: list[str] = []
    forced: set[Domain] = set()
    reasons: list[str] = []
    gmas_policy = _workspace_gmas_policy(repo_root, workspace_id)
    toml_path = workspace_root / "workspace.toml"
    try:
        toml_text = toml_path.read_text(encoding="utf-8", errors="replace")
        parts.append("## workspace.toml\n" + toml_text[:8000])
    except OSError:
        pass
    if gmas_policy is True:
        forced.add(Domain.MULTI_AGENT_GMAS)
        reasons.append("workspace.toml explicitly enables multi_agent_gmas")
    elif gmas_policy is False:
        reasons.append("workspace.toml explicitly disables multi_agent_gmas")
    return "\n\n".join(parts), forced, reasons


def _workspace_gmas_policy(repo_root: Path, workspace_id: str | None) -> bool | None:
    """Read explicit GMAS enable/disable policy from ``workspace.toml``.

    Returns:
      * ``True``  — workspace explicitly forces ``multi_agent_gmas`` on.
      * ``False`` — workspace explicitly opts out.
      * ``None``  — no policy; normal LLM/model/agent task detection decides.
    """

    if not workspace_id:
        return None
    try:
        seg = safe_workspace_segment(workspace_id)
    except ValueError:
        return None

    path = repo_root / "workspaces" / seg / "workspace.toml"
    try:
        with path.open("rb") as fh:
            data: dict[str, Any] = _toml.load(fh)
    except Exception:
        return None

    skills = data.get("skills")
    if isinstance(skills, dict):
        value = skills.get("multi_agent_gmas")
        if value is True:
            return True
        if value is False:
            return False

    gmas = data.get("gmas")
    if isinstance(gmas, dict):
        enabled = gmas.get("enabled")
        if enabled is True:
            return True
        if enabled is False:
            return False

    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        requires = workspace.get("requires_gmas")
        if requires is True:
            return True
        if requires is False:
            return False
        explicit = workspace.get("multi_agent_gmas")
        if explicit is True:
            return True
        if explicit is False:
            return False

    return None


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _upsert_toml_key(toml_text: str, section: str, key: str, value: Any) -> str:
    lines = toml_text.splitlines()
    in_section = False
    section_start: int | None = None
    section_end: int | None = None
    rendered = _toml_value(value)

    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip()
            if in_section and section_end is None:
                section_end = idx
            if current_section.lower() == section.lower():
                in_section = True
                section_start = idx
            else:
                in_section = False
            continue
        if in_section and stripped.split("=", 1)[0].strip() == key:
            lines[idx] = f"{key} = {rendered}"
            return "\n".join(lines) + ("\n" if toml_text.endswith("\n") else "")

    if section_start is not None:
        insert_at = section_end if section_end is not None else len(lines)
        while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"{key} = {rendered}")
        return "\n".join(lines) + ("\n" if toml_text.endswith("\n") else "")

    block = ["", f"[{section}]", f"{key} = {rendered}"]
    if not toml_text.strip():
        return f"[{section}]\n{key} = {rendered}\n"
    suffix = "\n" if toml_text.endswith("\n") else ""
    return toml_text.rstrip("\n") + "\n" + "\n".join(block).lstrip("\n") + suffix


def _record_workspace_skill_decision(
    repo_root: Path,
    workspace_id: str | None,
    *,
    domain: Domain,
    enabled: bool,
    reason: str,
    source: str,
    override_existing: bool = False,
) -> bool:
    """Persist Umbrella's skill decision into ``workspace.toml``.

    The active skill cache is useful for the current run, but
    ``workspace.toml`` is the durable workspace contract. Auto-detected
    skills therefore get written there unless the workspace already made
    an explicit opposite decision.
    """
    if not workspace_id:
        return False
    try:
        seg = safe_workspace_segment(workspace_id)
    except ValueError:
        return False
    workspace_root = repo_root / "workspaces" / seg
    toml_path = workspace_root / "workspace.toml"
    try:
        original = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    except OSError:
        return False

    try:
        parsed = _toml.loads(original) if original.strip() else {}
    except Exception:
        log.warning(
            "Not updating %s because workspace.toml is not parseable", toml_path
        )
        return False

    skills = parsed.get("skills") if isinstance(parsed, dict) else None
    existing_value = skills.get(domain.value) if isinstance(skills, dict) else None
    if existing_value is (not enabled) and not override_existing:
        return False

    updated = original
    updated = _upsert_toml_key(updated, "skills", domain.value, enabled)
    decision_section = f"skill_decisions.{domain.value}"
    updated = _upsert_toml_key(updated, decision_section, "enabled", enabled)
    updated = _upsert_toml_key(
        updated, decision_section, "detected_by", "umbrella.skill_detector"
    )
    updated = _upsert_toml_key(updated, decision_section, "source", source)
    updated = _upsert_toml_key(updated, decision_section, "reason", reason)
    updated = _upsert_toml_key(updated, decision_section, "updated_at", _utc_now_iso())

    if updated == original:
        return False
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
        toml_path.write_text(updated, encoding="utf-8")
        return True
    except OSError:
        log.debug("Failed to persist workspace skill decision", exc_info=True)
        return False


_SKILL_ARTIFACT_FILES: dict[Domain, str] = {
    Domain.MULTI_AGENT_GMAS: "gmas_active_context.md",
}

_SKILL_CACHE_SCHEMA_VERSION = 3


def _skill_cache_path(drive_root: Path) -> Path:
    return drive_root / "state" / "active_skills.json"


def _hash_task_text(task_text: str) -> str:
    return hashlib.sha256(task_text.encode("utf-8", errors="replace")).hexdigest()


def _load_skill_cache(drive_root: Path) -> dict[str, Any]:
    path = _skill_cache_path(drive_root)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_skill_cache(drive_root: Path, payload: dict[str, Any]) -> None:
    path = _skill_cache_path(drive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _log_skill_detection_missed(
    drive_root: Path,
    *,
    workspace_id: str | None,
    reason: str,
    signal_preview: str,
) -> None:
    try:
        events_path = drive_root / "logs" / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": _utc_now_iso(),
            "type": "skill_detection_missed",
            "workspace_id": workspace_id or "",
            "reason": reason,
            "signal_preview": signal_preview[:1000],
        }
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        log.debug("Failed to log skill_detection_missed", exc_info=True)


def _resolve_domains(
    *, drive_root: Path, task_text: str, workspace_id: str | None
) -> tuple[set[Domain], bool]:
    """Return detected domains for ``task_text`` and whether they're fresh.

    Caches the verdict in ``drive/state/active_skills.json`` keyed by
    ``sha256(task_text)`` + ``workspace_id``. ``fresh`` is ``True`` when
    the verdict had to be (re)computed -- the bridge uses this to decide
    whether the per-domain artifact (e.g. ``gmas_active_context.md``)
    must be rebuilt.
    """
    if not task_text.strip():
        return set(), False

    text_hash = _hash_task_text(task_text)
    cache = _load_skill_cache(drive_root)
    cached = cache.get("entry") if isinstance(cache, dict) else None
    if (
        isinstance(cached, dict)
        and cached.get("text_hash") == text_hash
        and cached.get("workspace_id") == (workspace_id or "")
        and cached.get("schema_version") == _SKILL_CACHE_SCHEMA_VERSION
    ):
        raw_domains = cached.get("domains") or []
        domains: set[Domain] = set()
        for value in raw_domains:
            try:
                domains.add(Domain(value))
            except ValueError:
                continue
        return domains, False

    domains = detect_task_domains(task_text)
    _save_skill_cache(
        drive_root,
        {
            "entry": {
                "schema_version": _SKILL_CACHE_SCHEMA_VERSION,
                "text_hash": text_hash,
                "workspace_id": workspace_id or "",
                "domains": sorted(d.value for d in domains),
                "computed_at": _utc_now_iso(),
            }
        },
    )
    return domains, True


def _build_gmas_active_context(*, repo_root: Path, task_text: str) -> str | None:
    """Run the existing GMAS retrieval tool and render its output as md.

    The skill layer does not invent new GMAS knowledge -- it dispatches
    ``umbrella.retrieval.gmas_context.build_gmas_context`` (the same tool
    Ouroboros calls via ``get_gmas_context``) so the agent starts with the
    same kind of payload it would have produced itself, only without
    burning a round on the first call.
    """
    try:
        from umbrella.retrieval.gmas_context import build_gmas_context
    except Exception as exc:
        log.debug("GMAS context module unavailable: %s", exc, exc_info=True)
        return None

    try:
        payload = build_gmas_context(
            repo_root,
            task_text,
            max_results=3,
            max_chars_per_hit=2500,
            token_budget=12000,
            auto_grow=False,
        )
    except Exception as exc:
        log.warning("Failed to pre-fetch GMAS context for skill: %s", exc)
        return None

    results = payload.get("results") or []
    if not results:
        return None

    def _clip_excerpt(text: str, limit: int = 1400) -> str:
        cleaned = str(text or "").rstrip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit].rstrip() + "\n...[excerpt truncated]"

    lines: list[str] = [
        "# GMAS Active Context (auto-fetched by Umbrella skill)",
        "",
        "> Umbrella detected that this task requires a multi-agent system and "
        "called `get_gmas_context` on your behalf using the task text as "
        "the query. The hits below are your starting point. **For any "
        "follow-up question, call `get_gmas_context` or "
        "`search_gmas_knowledge` yourself with a more specific query** "
        "(graph construction, runner invocation, tools, streaming, "
        "memory, routing) -- do not invent gmas APIs from memory.",
        "",
        f"- query: `{payload.get('query', task_text)[:160]}`",
        f"- recommended pattern: {payload.get('recommended_pattern') or 'n/a'}",
        f"- confidence: {payload.get('confidence', 0):.2f}"
        if isinstance(payload.get("confidence"), int | float)
        else "- confidence: n/a",
    ]
    key_files = payload.get("key_files") or []
    if key_files:
        lines.append("- key files: " + ", ".join(str(p) for p in key_files[:6]))
    lines.append("")
    for idx, result in enumerate(results, 1):
        title = result.get("title") or result.get("path") or f"hit {idx}"
        path = result.get("path") or "?"
        score = result.get("score")
        header = f"## {idx}. {title}"
        meta = f"`{path}`"
        if isinstance(score, int | float):
            meta += f"  · score={score}"
        truncated = result.get("content_truncated")
        if truncated:
            meta += "  · (truncated)"
        body = _clip_excerpt(result.get("content") or "")
        lang = "python" if str(path).endswith(".py") else ""
        fence = f"```{lang}" if body else ""
        lines.extend([header, meta, ""])
        if body:
            lines.extend([fence, body, "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def _clear_skill_artifacts(knowledge_dir: Path) -> None:
    for filename in _SKILL_ARTIFACT_FILES.values():
        try:
            (knowledge_dir / filename).unlink()
        except FileNotFoundError:
            pass


def _sync_active_skill_packs(
    *,
    repo_root: Path,
    drive_root: Path,
    knowledge_dir: Path,
    workspace_id: str | None,
    task_input: str | None,
    user_message: str | None,
) -> None:
    """Detect task domains and dispatch the matching Umbrella tool.

    Instead of duplicating in-repo docs into the drive, this calls the
    *existing* tool for each detected domain (e.g. ``build_gmas_context``)
    and writes its output as ``drive/memory/knowledge/<artifact>.md``.
    A short ``active_skills.md`` banner summarises what was prepared so
    the agent can find the artifact on its own.
    """
    task_main = _read_workspace_task_main(repo_root, workspace_id)
    # IMPORTANT: skill detection input MUST be stable across attempts so the
    # cache (active_skills.json) hits on subsequent runs. ``task_input`` here
    # is the fully rendered Ouroboros prompt, which itself embeds Prior
    # knowledge from the *previous* run -- including it would change the
    # text_hash on every attempt and force re-detection (and an LLM call,
    # which can be flaky). We therefore detect on TASK_MAIN.md alone, with
    # a short ``user_message`` only when no TASK_MAIN.md exists.
    workspace_signal, forced_domains, signal_reasons = _workspace_skill_signal(
        repo_root, workspace_id
    )
    if task_main.strip():
        detection_text = task_main
    else:
        detection_text = "\n\n".join(
            chunk for chunk in (user_message, task_input) if chunk
        )
    composite = "\n\n".join(
        chunk for chunk in (detection_text, workspace_signal) if chunk
    )

    domains, fresh = _resolve_domains(
        drive_root=drive_root,
        task_text=composite,
        workspace_id=workspace_id,
    )
    explicit_gmas_policy = _workspace_gmas_policy(repo_root, workspace_id)
    # GMAS is automatic for LLM/agent tasks. The classifier already
    # distinguishes pure plumbing from LLM-backed work; when it fires we
    # keep the domain so the prompt and knowledge bridge load GMAS
    # context before the first write. A stale workspace.toml false must
    # not silently override a current task that explicitly asks for
    # LLM/model/agent behavior.
    auto_detected_gmas = Domain.MULTI_AGENT_GMAS in domains
    if explicit_gmas_policy is True:
        domains.add(Domain.MULTI_AGENT_GMAS)
    elif explicit_gmas_policy is False and auto_detected_gmas:
        wrote_policy = _record_workspace_skill_decision(
            repo_root,
            workspace_id,
            domain=Domain.MULTI_AGENT_GMAS,
            enabled=True,
            reason=(
                "Current TASK_MAIN.md explicitly requires LLM/model/agent work; "
                "overriding a stale workspace opt-out so LLM-backed code uses GMAS."
            ),
            source="TASK_MAIN.md",
            override_existing=True,
        )
        signal_reasons.append(
            "current LLM task overrides prior workspace.toml multi_agent_gmas=false"
        )
        if wrote_policy:
            signal_reasons.append(
                "workspace.toml auto-updated: skills.multi_agent_gmas = true"
            )
    elif Domain.MULTI_AGENT_GMAS in domains:
        wrote_policy = _record_workspace_skill_decision(
            repo_root,
            workspace_id,
            domain=Domain.MULTI_AGENT_GMAS,
            enabled=True,
            reason=(
                "Umbrella detected LLM/model/agent/prompt work in the workspace task; "
                "LLM-backed agents in this repo should use GMAS."
            ),
            source="TASK_MAIN.md" if task_main.strip() else "task_input",
        )
        if wrote_policy:
            signal_reasons.append(
                "workspace.toml auto-updated: skills.multi_agent_gmas = true"
            )
    if forced_domains:
        domains.update(forced_domains)
        _save_skill_cache(
            drive_root,
            {
                "entry": {
                    "schema_version": _SKILL_CACHE_SCHEMA_VERSION,
                    "text_hash": _hash_task_text(composite),
                    "workspace_id": workspace_id or "",
                    "domains": sorted(d.value for d in domains),
                    "computed_at": _utc_now_iso(),
                    "forced_reasons": signal_reasons,
                }
            },
        )
    elif auto_detected_gmas and Domain.MULTI_AGENT_GMAS not in domains:
        _save_skill_cache(
            drive_root,
            {
                "entry": {
                    "schema_version": _SKILL_CACHE_SCHEMA_VERSION,
                    "text_hash": _hash_task_text(composite),
                    "workspace_id": workspace_id or "",
                    "domains": sorted(d.value for d in domains),
                    "computed_at": _utc_now_iso(),
                    "policy_override": "multi_agent_gmas disabled by explicit workspace.toml opt-out",
                }
            },
        )
    elif not domains:
        _log_skill_detection_missed(
            drive_root,
            workspace_id=workspace_id,
            reason="no domains detected from task text or workspace signals",
            signal_preview=composite[:1000],
        )
    if workspace_id:
        try:
            seg = safe_workspace_segment(workspace_id)
            domains_path = repo_root / "workspaces" / seg / ".memory" / "domains.json"
            domains_path.parent.mkdir(parents=True, exist_ok=True)
            domains_path.write_text(
                json.dumps(
                    {
                        "workspace_id": workspace_id,
                        "domains": sorted(d.value for d in domains),
                        "updated_at": _utc_now_iso(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            log.debug("Failed to persist workspace domains", exc_info=True)

    banner_path = knowledge_dir / "active_skills.md"
    if not domains:
        try:
            banner_path.unlink()
        except FileNotFoundError:
            pass
        _clear_skill_artifacts(knowledge_dir)
        return

    if Domain.MULTI_AGENT_GMAS in domains:
        try:
            (knowledge_dir / _SKILL_ARTIFACT_FILES[Domain.MULTI_AGENT_GMAS]).unlink()
        except FileNotFoundError:
            pass

    banner_lines = [
        "# Active Skills",
        "",
        f"Generated: {_utc_now_iso()}",
        "",
        summarize_domains(domains),
        "",
        "When a skill looks relevant, call `load_skill` with its slug for full L3 instructions.",
    ]
    artifacts: list[str] = []
    if artifacts:
        banner_lines.extend(["", "Artifacts:", *[f"- {a}" for a in artifacts]])
    _write_optional_markdown(banner_path, "\n".join(banner_lines))


def prepare_active_skills_for_workspace(
    repo_root: Path,
    workspace_id: str,
    *,
    user_message: str | None = None,
    task_input: str | None = None,
    phase_id: str | None = None,
) -> set[Domain]:
    """Run skill detection eagerly so ``active_skills.json`` and the
    matching ``gmas_active_context.md`` artifact exist *before* the
    workspace prompt is rendered.

    Without this, ``app_ouroboros.py`` hits a chicken-and-egg: it builds
    the prompt (which reads ``active_skills.json``) before
    ``sync_umbrella_context_to_drive`` populates it. As a result the very
    first attempt ships a prompt without the ``### Detected skills``
    block and the agent silently falls back to a non-GMAS stack.

    Returns the detected domains.
    """
    repo_root = repo_root.resolve()
    drive_root = workspace_drive_root(repo_root, workspace_id)
    ensure_drive_layout(drive_root)
    seed_workspace_prompts(repo_root, workspace_id)
    knowledge_dir = drive_root / "memory" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    try:
        _sync_active_skill_packs(
            repo_root=repo_root,
            drive_root=drive_root,
            knowledge_dir=knowledge_dir,
            workspace_id=workspace_id,
            task_input=task_input,
            user_message=user_message,
        )
    except Exception:
        log.debug(
            "prepare_active_skills_for_workspace failed for %s",
            workspace_id,
            exc_info=True,
        )
        return set()

    # Re-read the cache to surface what was detected to the caller. The
    # cache is the single source of truth for ``load_detected_domains`` so
    # the prompt renderer reads from the same file.
    try:
        cache_path = drive_root / "state" / "active_skills.json"
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    entry = data.get("entry") if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return set()
    raw = entry.get("domains") or []
    out: set[Domain] = set()
    for value in raw:
        try:
            out.add(Domain(value))
        except ValueError:
            continue

    if phase_id:
        from umbrella.skills.registry import discover_skills, filter_by_phase, skill_library_root
        all_skills = discover_skills(skill_library_root(repo_root))
        skills = filter_by_phase(all_skills, phase_id, status=None)
        phase_domains: set[Domain] = set()
        for sk in skills:
            for d in sk.domains:
                try:
                    phase_domains.add(Domain(d))
                except ValueError:
                    continue
        if phase_domains:
            out = out.intersection(phase_domains)

    return out


def _rebuild_knowledge_index(knowledge_dir: Path) -> None:
    docs = sorted(
        path for path in knowledge_dir.glob("*.md") if path.name != "_index.md"
    )
    lines = [
        "# Knowledge Index",
        "",
        f"Generated: {_utc_now_iso()}",
    ]

    if not docs:
        lines.extend(["", "No knowledge documents synced yet."])
    else:
        for doc in docs:
            try:
                content = doc.read_text(encoding="utf-8").strip()
            except Exception:
                log.debug("Failed to read knowledge document %s", doc, exc_info=True)
                continue
            if not content:
                continue
            lines.extend(
                [
                    "",
                    f"## {doc.stem.replace('_', ' ').title()}",
                    "",
                    content,
                ]
            )

    (knowledge_dir / "_index.md").write_text(
        "\n".join(lines).strip() + "\n",
        encoding="utf-8",
    )


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def _memory_config(memory_root: Path) -> MemoryConfig:
    return MemoryConfig(
        memory_root=memory_root,
        lessons_path=memory_root / "lessons.jsonl",
        gaps_path=memory_root / "gaps.jsonl",
        signals_path=memory_root / "signals.jsonl",
    )


def _format_live_context_section(
    *,
    user_message: str | None,
    memory_payload: dict[str, Any] | None,
) -> list[str]:
    lines: list[str] = []
    if (user_message or "").strip():
        lines.append(f"- User message: {user_message.strip()[:400]}")
    sanitized_memory = _sanitize_memory_payload(memory_payload)
    if sanitized_memory:
        for key, value in sanitized_memory.items():
            lines.append(f"- {key}: {value}")
    return lines


def _sanitize_memory_payload(memory_payload: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(memory_payload, dict):
        return {}

    sanitized: dict[str, str] = {}
    for key, value in memory_payload.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        value_text = str(value).strip()
        if not value_text:
            continue
        sanitized[key_text[:80]] = value_text[:800]
    return sanitized


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
