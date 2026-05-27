"""External knowledge catalog: handles over disk + palace, not prompt blobs."""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from umbrella.discovery.web_page_chunks import preview_text, slugify

log = logging.getLogger(__name__)

CATALOG_FILENAME = "external_knowledge_catalog.json"
_EK_PREFIX = "ek:"
_PREVIEW_DEFAULT = 400
_MAX_CARDS = 500


def _drive_root_from_ctx(ctx: Any) -> Path | None:
    raw = getattr(ctx, "drive_root", None)
    if not raw:
        return None
    path = Path(raw)
    if path.name == "drive" and path.parent.name == ".memory":
        return path.resolve()
    return None


def catalog_path(ctx: Any) -> Path | None:
    drive = _drive_root_from_ctx(ctx)
    if drive is None:
        return None
    return drive / "state" / CATALOG_FILENAME


def _load_cards(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.debug("catalog read failed for %s", path, exc_info=True)
        return []
    cards = payload.get("cards") if isinstance(payload, dict) else payload
    return [c for c in cards if isinstance(c, dict)] if isinstance(cards, list) else []


def _save_cards(path: Path, cards: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cards": cards[-_MAX_CARDS:],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _card_id(kind: str, handle: str) -> str:
    safe = slugify(handle.replace("://", "_").replace("/", "_"), max_len=120)
    return f"{_EK_PREFIX}{kind}:{safe}"


def register_card(
    ctx: Any,
    *,
    kind: str,
    source_id: str,
    storage_ref: str = "",
    preview: str = "",
    tags: list[str] | None = None,
    intent: str = "",
    licence: str = "",
    size_bytes: int = 0,
    parent_id: str | None = None,
    url: str = "",
    title: str = "",
    palace_room: str = "",
) -> str:
    path = catalog_path(ctx)
    if path is None:
        return ""
    handle = source_id or storage_ref or url or title
    card_id = _card_id(kind, handle)
    cards = _load_cards(path)
    by_id = {str(c.get("id") or ""): c for c in cards}
    card = {
        "id": card_id,
        "kind": kind,
        "source_id": source_id,
        "storage_ref": storage_ref,
        "preview": preview_text(preview, limit=_PREVIEW_DEFAULT),
        "tags": list(tags or [])[:20],
        "intent": (intent or "").strip(),
        "licence": (licence or "").strip(),
        "size_bytes": int(size_bytes or 0),
        "parent_id": parent_id,
        "url": url,
        "title": (title or "")[:200],
        "palace_room": palace_room,
        "children": [],
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    by_id[card_id] = card
    if parent_id and parent_id in by_id:
        children = list(by_id[parent_id].get("children") or [])
        if card_id not in children:
            children.append(card_id)
            by_id[parent_id]["children"] = children
    _save_cards(path, list(by_id.values()))
    return card_id


def resolve_ref(ctx: Any, ref: str) -> dict[str, Any] | None:
    text = (ref or "").strip()
    if not text:
        return None
    path = catalog_path(ctx)
    if path is None:
        return None
    cards = _load_cards(path)
    if text.startswith(_EK_PREFIX):
        for card in cards:
            if str(card.get("id") or "") == text:
                return card
        return None
    norm = text.replace("\\", "/").lstrip("/")
    for card in cards:
        if str(card.get("storage_ref") or "").replace("\\", "/").lstrip("/") == norm:
            return card
        if str(card.get("source_id") or "") == text:
            return card
    return None


def find_by_storage_ref(ctx: Any, storage_ref: str) -> str:
    card = resolve_ref(ctx, storage_ref)
    return str(card.get("id") or "") if card else ""


def list_cards(
    ctx: Any,
    *,
    kinds: set[str] | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    path = catalog_path(ctx)
    if path is None:
        return []
    cards = _load_cards(path)
    out: list[dict[str, Any]] = []
    for card in reversed(cards):
        if kinds and str(card.get("kind") or "") not in kinds:
            continue
        out.append(card)
        if len(out) >= limit:
            break
    return out


def catalog_summary_for_prompt(ctx: Any, *, max_cards: int = 40) -> str:
    cards = list_cards(ctx, limit=max_cards)
    if not cards:
        return ""
    lines = ["## External knowledge catalog (handles — load bodies via memory_scope or read_file)"]
    for card in cards:
        lines.append(
            f"- `{card.get('id')}` **{card.get('kind')}** "
            f"src=`{card.get('source_id')}` "
            f"ref=`{card.get('storage_ref')}` "
            f"tags={','.join(card.get('tags') or []) or '-'} "
            f"— {(card.get('preview') or '')[:200]}"
        )
    return "\n".join(lines)


def mirror_preview_body(
    *,
    source_id: str,
    url: str = "",
    preview: str = "",
    storage_ref: str = "",
) -> str:
    parts = [source_id]
    if url:
        parts.append(f"url: {url}")
    if storage_ref:
        parts.append(f"storage: {storage_ref}")
    if preview:
        parts.append(f"preview: {preview_text(preview)}")
    return "\n".join(parts)


_EXTERNAL_GOAL_RE = re.compile(
    r"(?i)\b(?:github|prior\s+art|external|web\s+fetch|deep_search|"
    r"pattern_adapt|codeptr|inspiration|third[- ]party|reference\s+impl)\b"
)


def subtask_needs_external_memory(subtask: dict[str, Any]) -> bool:
    if subtask.get("no_external_deps"):
        return False
    goal = str(subtask.get("goal") or subtask.get("title") or "")
    if _EXTERNAL_GOAL_RE.search(goal):
        return True
    if subtask.get("codeptr_refs") or subtask.get("external_asset_refs"):
        return True
    scope = subtask.get("memory_scope")
    if isinstance(scope, dict) and scope.get("assets"):
        return True
    return False


def subtask_has_external_wiring(subtask: dict[str, Any]) -> bool:
    if subtask.get("no_external_deps"):
        return True
    if subtask.get("codeptr_refs"):
        return True
    refs = subtask.get("external_asset_refs")
    if isinstance(refs, list) and refs:
        return True
    scope = subtask.get("memory_scope")
    if isinstance(scope, dict) and scope.get("assets"):
        return True
    return False


def plan_external_memory_issues(plan: dict[str, Any], ctx: Any) -> list[str]:
    from umbrella.deep_agent_tools.phase_contract_policy import _iter_plan_subtasks

    issues: list[str] = []
    summary = catalog_summary_for_prompt(ctx, max_cards=12)
    hint = summary.split("\n")[1:4] if summary else []
    for idx, subtask in enumerate(_iter_plan_subtasks(plan), start=1):
        if not subtask_needs_external_memory(subtask):
            continue
        if subtask_has_external_wiring(subtask):
            continue
        sid = str(subtask.get("id") or subtask.get("subtask_id") or idx)
        msg = (
            f"subtask {sid}: external prior art implied in goal but missing "
            "memory_scope.assets, codeptr_refs, external_asset_refs, or "
            "no_external_deps:true"
        )
        if hint:
            msg += f"; catalog: {'; '.join(h.strip('- ') for h in hint)}"
        issues.append(msg)
    return issues


def persist_fetched_page(
    ctx: Any,
    *,
    url: str,
    body: str,
    intent: str = "",
    parent_id: str | None = None,
    extract_sections: bool = True,
) -> dict[str, Any]:
    """Write page + optional sections under drive/memory/knowledge/web/pages/."""
    from umbrella.discovery.web_page_chunks import (
        canonical_url,
        page_paths_for_url,
        preview_text,
        split_sections,
    )

    drive = _drive_root_from_ctx(ctx)
    if drive is None or not (body or "").strip():
        return {}
    canonical = canonical_url(url)
    _host, _page_slug, rel_dir = page_paths_for_url(canonical)
    base = drive / rel_dir
    base.mkdir(parents=True, exist_ok=True)
    index_path = base / "index.md"
    index_path.write_text(
        f"# {canonical}\n\n{body.strip()}\n",
        encoding="utf-8",
    )
    rel_posix = rel_dir.as_posix()
    storage_ref = f".memory/drive/{rel_posix}/index.md"
    page_id = register_card(
        ctx,
        kind="web_page",
        source_id=f"web_fetch:{canonical}",
        storage_ref=storage_ref,
        preview=body,
        tags=["web", "web_page", intent] if intent else ["web", "web_page"],
        intent=intent,
        url=canonical,
        parent_id=parent_id,
        size_bytes=len(body),
        palace_room="web_pages",
    )
    section_ids: list[str] = []
    if extract_sections:
        for heading, chunk in split_sections(body):
            slug = slugify(heading)
            section_file = base / "sections" / f"{slug}.md"
            section_file.parent.mkdir(parents=True, exist_ok=True)
            section_file.write_text(f"## {heading}\n\n{chunk}\n", encoding="utf-8")
            sec_ref = f"{storage_ref.rsplit('/index.md', 1)[0]}/sections/{slug}.md"
            sid = register_card(
                ctx,
                kind="web_section",
                source_id=f"web_fetch:{canonical}#{slug}",
                storage_ref=sec_ref,
                preview=chunk,
                tags=["web", "web_section"],
                parent_id=page_id,
                url=canonical,
                title=heading,
                palace_room="web_pages",
            )
            if sid:
                section_ids.append(sid)
    return {
        "catalog_id": page_id,
        "section_ids": section_ids,
        "page_storage_ref": storage_ref,
        "preview": preview_text(body),
        "url": canonical,
    }


def persist_search_session(
    ctx: Any,
    *,
    query: str,
    intent: str,
    results: list[dict[str, str]],
) -> tuple[str, list[str]]:
    """Per-query markdown + web_search_hit cards. Returns (knowledge_md, catalog_ids)."""
    drive = _drive_root_from_ctx(ctx)
    if drive is None:
        return "", []
    intent_slug = slugify(intent or "research")
    query_slug = slugify(query)[:60]
    md_dir = drive / "memory" / "knowledge" / "web" / "search" / intent_slug
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / f"{query_slug}.md"
    lines = [f"## {intent} — {query}", ""]
    catalog_ids: list[str] = []
    for idx, item in enumerate(results, start=1):
        title = item.get("title") or "Untitled"
        url = item.get("url") or ""
        snippet = (item.get("snippet") or "").replace("\n", " ").strip()
        lines.append(f"{idx}. [{title}]({url}) — {snippet[:280]}")
        hit_ref = (
            f".memory/drive/memory/knowledge/web/search/{intent_slug}/{query_slug}.md"
        )
        hit_source = f"web:{url}" if url else f"deep_search:{intent}:{query}"
        cid = register_card(
            ctx,
            kind="web_search_hit",
            source_id=hit_source,
            storage_ref=hit_ref,
            preview=snippet or title,
            tags=["web", "deep_search", intent],
            intent=intent,
            url=url,
            title=title,
            palace_room="web_search",
        )
        if cid:
            catalog_ids.append(cid)
            item["catalog_id"] = cid
    lines.append("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    knowledge_md = (
        f".memory/drive/memory/knowledge/web/search/{intent_slug}/{query_slug}.md"
    )
    return knowledge_md, catalog_ids


def suggest_memory_scope_for_goal(ctx: Any, goal: str, *, limit: int = 4) -> dict[str, Any]:
    """Pick catalog cards whose preview/tags loosely match subtask goal."""
    words = {w.lower() for w in re.findall(r"[a-z0-9]{4,}", goal or "")}
    assets: list[dict[str, Any]] = []
    for card in list_cards(ctx, limit=80):
        blob = " ".join(
            [
                str(card.get("preview") or ""),
                " ".join(card.get("tags") or []),
                str(card.get("source_id") or ""),
                str(card.get("title") or ""),
            ]
        ).lower()
        if words and not any(w in blob for w in words):
            continue
        assets.append(
            {
                "kind": card.get("kind") or "knowledge_md",
                "ref": card.get("id") or card.get("storage_ref") or "",
                "source_id": card.get("source_id") or "",
                "inject_mode": "on_demand",
                "max_chars": 4000,
            }
        )
        if len(assets) >= limit:
            break
    return {"assets": assets, "palace_search_queries": [goal[:200]] if goal else []}
