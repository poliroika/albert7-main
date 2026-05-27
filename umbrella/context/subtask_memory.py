"""Resolve per-subtask memory scope for execute-phase LLM input."""

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

MemoryInjectMode = Literal["preload", "on_demand", "search_only"]
MemoryAssetKind = Literal[
    "codeptr",
    "knowledge_md",
    "github_repo",
    "github_snippet",
    "web_page",
    "web_section",
    "web_search_hit",
    "palace_finding",
    "gmas_context",
    "workspace_file",
    "mcp_server",
    "skill",
    "terminal_tail",
]

DEFAULT_EXECUTE_BASELINE: tuple[str, ...] = (
    "accepted_plan",
    "bkb_identity",
    "phase_commitments",
    "allowed_tools",
    "terminal_tail",
)

_PRELOAD_KINDS = frozenset({
    "codeptr",
    "knowledge_md",
    "github_repo",
    "github_snippet",
    "web_page",
    "web_section",
    "palace_finding",
})
_PRELOAD_BODY_CAP = 3


@dataclass(frozen=True)
class SubtaskMemoryAsset:
    kind: MemoryAssetKind
    ref: str
    title: str = ""
    inject_mode: MemoryInjectMode = "preload"
    max_chars: int = 6000
    source_id: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "SubtaskMemoryAsset | None":
        if not isinstance(value, dict):
            return None
        kind = str(value.get("kind") or value.get("type") or "").strip().lower()
        ref = str(value.get("ref") or value.get("path") or value.get("id") or "").strip()
        if not kind or not ref:
            return None
        mode_raw = str(value.get("inject_mode") or value.get("mode") or "preload").strip().lower()
        if mode_raw not in {"preload", "on_demand", "search_only"}:
            mode_raw = "preload" if kind in _PRELOAD_KINDS else "on_demand"
        return cls(
            kind=kind,  # type: ignore[arg-type]
            ref=ref,
            title=str(value.get("title") or "")[:200],
            inject_mode=mode_raw,  # type: ignore[arg-type]
            max_chars=max(500, int(value.get("max_chars") or 6000)),
            source_id=str(value.get("source_id") or "")[:300],
        )


@dataclass
class SubtaskMemoryScope:
    """Planner-declared memory envelope for one execute subtask."""

    baseline: tuple[str, ...] = field(default_factory=lambda: DEFAULT_EXECUTE_BASELINE)
    assets: tuple[SubtaskMemoryAsset, ...] = ()
    palace_search_queries: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": list(self.baseline),
            "assets": [asdict(asset) for asset in self.assets],
            "palace_search_queries": list(self.palace_search_queries),
            "notes": self.notes,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "SubtaskMemoryScope | None":
        if not isinstance(value, dict):
            return None
        baseline_raw = value.get("baseline")
        baseline = DEFAULT_EXECUTE_BASELINE
        if isinstance(baseline_raw, list):
            cleaned = [str(item).strip() for item in baseline_raw if str(item).strip()]
            if cleaned:
                baseline = tuple(cleaned)
        assets: list[SubtaskMemoryAsset] = []
        for item in value.get("assets") or ():
            asset = SubtaskMemoryAsset.from_mapping(item)
            if asset is not None:
                assets.append(asset)
        queries = tuple(
            str(item).strip()
            for item in (value.get("palace_search_queries") or value.get("search_queries") or ())
            if str(item).strip()
        )
        return cls(
            baseline=baseline,
            assets=tuple(assets),
            palace_search_queries=queries,
            notes=str(value.get("notes") or "")[:2000],
        )


@dataclass(frozen=True)
class ResolvedSubtaskMemoryChunk:
    kind: str
    ref: str
    title: str
    inject_mode: MemoryInjectMode
    text: str
    loaded: bool
    reason: str = ""


def infer_memory_scope_from_subtask(
    subtask: dict[str, Any],
    *,
    drive_root: Path | None = None,
) -> SubtaskMemoryScope:
    """Build a default scope when the planner omitted ``memory_scope``."""
    explicit = SubtaskMemoryScope.from_mapping(subtask.get("memory_scope"))
    assets: list[SubtaskMemoryAsset] = list(explicit.assets) if explicit else []

    class _CatalogCtx:
        pass

    catalog_ctx = _CatalogCtx()
    if drive_root is not None:
        catalog_ctx.drive_root = drive_root

    for ref in subtask.get("codeptr_refs") or ():
        text = str(ref or "").strip()
        if not text:
            continue
        kind: MemoryAssetKind = (
            "knowledge_md" if "knowledge" in text.lower() or text.endswith(".md") else "codeptr"
        )
        ek_ref = text
        if drive_root is not None:
            from umbrella.discovery.external_catalog import find_by_storage_ref

            found = find_by_storage_ref(catalog_ctx, text)
            if found:
                ek_ref = found
        if any(a.ref in {ek_ref, text} for a in assets):
            continue
        assets.append(
            SubtaskMemoryAsset(
                kind=kind,
                ref=ek_ref,
                title=text.rsplit("/", 1)[-1],
                inject_mode="preload",
            )
        )
    for ref in subtask.get("external_asset_refs") or ():
        text = str(ref or "").strip()
        if text:
            assets.append(
                SubtaskMemoryAsset(
                    kind="knowledge_md",
                    ref=text,
                    inject_mode="preload",
                )
            )
    for ref in subtask.get("mcp_refs") or ():
        text = str(ref or "").strip()
        if text:
            assets.append(
                SubtaskMemoryAsset(
                    kind="mcp_server",
                    ref=text,
                    inject_mode="on_demand",
                )
            )
    for key in ("files_to_create", "files_to_change", "files_affected"):
        raw = subtask.get(key)
        paths: list[str] = []
        if isinstance(raw, str) and raw.strip():
            paths.append(raw.strip())
        elif isinstance(raw, (list, tuple)):
            paths.extend(str(item).strip() for item in raw if str(item).strip())
        for path in paths:
            assets.append(
                SubtaskMemoryAsset(
                    kind="workspace_file",
                    ref=path.replace("\\", "/").lstrip("/"),
                    inject_mode="on_demand",
                )
            )
    goal = str(subtask.get("goal") or subtask.get("title") or "").strip()
    queries = (goal[:240],) if goal else ()
    if explicit is not None:
        seen = {e.ref for e in explicit.assets}
        merged = list(explicit.assets) + [a for a in assets if a.ref not in seen]
        return SubtaskMemoryScope(
            baseline=explicit.baseline,
            assets=tuple(merged),
            palace_search_queries=explicit.palace_search_queries or queries,
            notes=explicit.notes,
        )
    return SubtaskMemoryScope(assets=tuple(assets), palace_search_queries=queries)


def _candidate_paths(ref: str, workspace_root: Path, drive_root: Path | None) -> list[Path]:
    norm = ref.replace("\\", "/").lstrip("/")
    roots = [workspace_root]
    if drive_root is not None:
        roots.append(drive_root)
        roots.append(drive_root.parent.parent)
    out: list[Path] = []
    for root in roots:
        out.append(root / norm)
        if norm.startswith(".memory/"):
            out.append(root.parent.parent / norm)
        if "knowledge/" in norm:
            out.append(root / ".memory" / "drive" / norm.split("/drive/", 1)[-1])
    return out


def _read_text_file(path: Path, max_chars: int) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > max_chars:
        return text[: max_chars - 24].rstrip() + "\n...[asset truncated]"
    return text


def _resolve_palace_snippet(
    ref: str,
    *,
    repo_root: Path,
    workspace_id: str,
    max_chars: int,
) -> str:
    try:
        from umbrella.memory.palace.facade import MemPalace
    except Exception:
        return ""
    palace = MemPalace(repo_root, workspace_id or None)
    try:
        hits = palace.search(ref, n=3)
        if not hits:
            return ""
        node = hits[0]
        content = str(getattr(node, "content", None) or getattr(node, "text", "") or "")
        if len(content) > max_chars:
            content = content[: max_chars - 24].rstrip() + "\n...[palace truncated]"
        return content
    except Exception:
        log.debug("palace search failed for subtask asset %s", ref, exc_info=True)
        return ""
    finally:
        palace.close()


def _terminal_tail(drive_root: Path | None, *, max_chars: int = 1200) -> str:
    if drive_root is None:
        return ""
    tools_path = drive_root / "logs" / "tools.jsonl"
    if not tools_path.is_file():
        return ""
    try:
        lines = tools_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    tail = lines[-8:]
    text = "\n".join(tail)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def _asset_storage_ref(
    asset: SubtaskMemoryAsset,
    *,
    drive_root: Path | None,
) -> str:
    ref = asset.ref.strip()
    if not ref.startswith("ek:") or drive_root is None:
        return ref
    from umbrella.discovery.external_catalog import resolve_ref

    ctx = type("_CatalogCtx", (), {"drive_root": drive_root})()
    card = resolve_ref(ctx, ref)
    if card:
        return str(card.get("storage_ref") or ref)
    return ref


def resolve_subtask_memory_chunks(
    scope: SubtaskMemoryScope,
    *,
    repo_root: Path,
    workspace_root: Path,
    workspace_id: str,
    drive_root: Path | None,
    subtask: dict[str, Any] | None = None,
) -> list[ResolvedSubtaskMemoryChunk]:
    chunks: list[ResolvedSubtaskMemoryChunk] = []
    preloaded_bodies = 0
    for asset in scope.assets:
        text = ""
        loaded = False
        reason = ""
        storage_ref = _asset_storage_ref(asset, drive_root=drive_root)
        if asset.inject_mode == "search_only":
            reason = "search_only — use palace_search in this subtask"
        elif asset.kind in _PRELOAD_KINDS or asset.inject_mode == "preload":
            skip_body = (
                asset.inject_mode == "preload"
                and preloaded_bodies >= _PRELOAD_BODY_CAP
            )
            if skip_body:
                reason = "on_demand — preload cap reached; use read_file"
            for candidate in _candidate_paths(storage_ref, workspace_root, drive_root):
                text = _read_text_file(candidate, asset.max_chars)
                if text.strip() and not skip_body:
                    loaded = True
                    preloaded_bodies += 1
                    reason = f"loaded from {candidate}"
                    break
            if not text.strip() and asset.kind in {"codeptr", "palace_finding"}:
                text = _resolve_palace_snippet(
                    asset.ref,
                    repo_root=repo_root,
                    workspace_id=workspace_id,
                    max_chars=asset.max_chars,
                )
                if text.strip():
                    loaded = True
                    reason = "loaded from palace search"
            if not text.strip():
                reason = "missing — run read_file or palace_search before write"
        else:
            reason = "on_demand — not preloaded; read when implementing"
        chunks.append(
            ResolvedSubtaskMemoryChunk(
                kind=asset.kind,
                ref=asset.ref,
                title=asset.title or asset.ref,
                inject_mode=asset.inject_mode,
                text=text,
                loaded=loaded,
                reason=reason,
            )
        )
    if "terminal_tail" in scope.baseline:
        tail = _terminal_tail(drive_root)
        chunks.append(
            ResolvedSubtaskMemoryChunk(
                kind="terminal_tail",
                ref="drive/logs/tools.jsonl",
                title="Recent tool log tail",
                inject_mode="preload",
                text=tail,
                loaded=bool(tail.strip()),
                reason="last tool log lines" if tail.strip() else "no tools.jsonl yet",
            )
        )
    return chunks


def render_subtask_memory_scope_markdown(
    scope: SubtaskMemoryScope,
    chunks: list[ResolvedSubtaskMemoryChunk],
    *,
    subtask_id: str,
) -> str:
    lines = [
        "## Subtask memory scope (planner contract)",
        f"Active subtask: `{subtask_id}`",
        "",
        "### Always-on baseline for this subtask",
    ]
    for item in scope.baseline:
        lines.append(f"- `{item}` — loaded via Umbrella execute envelope (not optional)")
    lines.append("")
    lines.append("### Declared memory assets")
    lines.append(
        "| Kind | Ref | Mode | Preloaded | Notes |"
    )
    lines.append("|------|-----|------|-----------|-------|")
    for chunk in chunks:
        if chunk.kind == "terminal_tail" and chunk.ref == "drive/logs/tools.jsonl":
            continue
        loaded = "yes" if chunk.loaded else "no"
        lines.append(
            f"| {chunk.kind} | `{chunk.ref[:80]}` | {chunk.inject_mode} | {loaded} | {chunk.reason[:120]} |"
        )
    if scope.palace_search_queries:
        lines.append("")
        lines.append("### Suggested palace_search queries (supplemental)")
        for query in scope.palace_search_queries[:6]:
            lines.append(f"- `{query}`")
    if scope.notes.strip():
        lines.append("")
        lines.append("### Planner notes")
        lines.append(scope.notes.strip())
    preloaded = [c for c in chunks if c.loaded and c.text.strip()]
    if preloaded:
        lines.append("")
        lines.append(
            "### Preloaded asset excerpts "
            f"(max {_PRELOAD_BODY_CAP}; verify; authoritative artifacts win on conflict)"
        )
        for chunk in preloaded[:_PRELOAD_BODY_CAP]:
            lines.append(f"#### {chunk.title} (`{chunk.kind}`)")
            lines.append("```")
            lines.append(chunk.text.strip())
            lines.append("```")
    lines.append("")
    lines.append(
        "Use `palace_search` / `read_file` for `on_demand` rows before workspace writes. "
        "Do not treat preloaded excerpts as permission to skip fresh reads on files you will edit."
    )
    return "\n".join(lines)
