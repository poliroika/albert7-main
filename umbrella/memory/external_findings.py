"""Persistence for findings returned by external discovery tools."""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def mirror_external_finding_to_memory(
    ctx: Any,
    *,
    kind: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
    palace_room: str = "external_research",
    palace_subpath: str = "",
    workspace_id: str = "",
    metadata_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an external discovery result to hierarchical + semantic memory."""

    try:
        from umbrella.memory.hierarchical import HierarchicalMemory
        from umbrella.memory.palace_backend import get_palace_backend
        from umbrella.memory.paths import palace_path_for

        repo_root = _resolve_repo_root(ctx)
        ws = workspace_id or _current_workspace_id_from_drive(ctx)
        if not ws:
            return {"mirrored": False, "reason": "workspace_id_missing"}
        root = _workspace_memory_root(repo_root, ws, ctx)
        root.mkdir(parents=True, exist_ok=True)

        body_norm = str(body or "").strip()
        title_norm = str(title or "").strip() or f"{kind} finding"
        if not body_norm:
            return {"mirrored": False, "reason": "empty_body"}

        tag_list = _external_finding_tags(kind, tags)
        subpath = (palace_subpath or kind).strip("/")
        hier_path = f"workspaces/{ws}/external/{subpath}"

        hm = HierarchicalMemory(root)
        record = hm.add(
            palace_path=hier_path,
            title=title_norm[:200],
            content=body_norm,
            kind=kind,
            workspace_id=ws,
            task_id=str(getattr(ctx, "task_id", "") or ""),
            tags=tag_list,
            metadata={
                "source": "external_discovery_tool",
                "ts": datetime.now(timezone.utc).isoformat(),
                "evidence_kind": "verified_outcome",
                **(metadata_extra or {}),
            },
        )

        palace_result: dict[str, Any] = {}
        try:
            palace_result = get_palace_backend(palace_path_for(repo_root, ws)).add(
                workspace_id=ws,
                event_type=kind,
                room=palace_room or "external_research",
                title=title_norm[:200],
                content=body_norm,
                kind=kind,
                tags=tag_list,
                task_id=str(getattr(ctx, "task_id", "") or ""),
                metadata_extra={
                    "hierarchical_id": record.id,
                    "palace_path": hier_path,
                    "evidence_kind": "verified_outcome",
                    **(metadata_extra or {}),
                },
            )
        except Exception:
            log.debug("mirror_external_finding semantic mirror skipped", exc_info=True)

        return {
            "mirrored": True,
            "workspace_id": ws,
            "hierarchical_id": record.id,
            "palace_path": hier_path,
            "mirrored_to_semantic": bool(palace_result),
        }
    except Exception:
        log.warning("mirror_external_finding_to_memory failed", exc_info=True)
        return {"mirrored": False, "reason": "exception"}


def _external_finding_tags(kind: str, tags: list[str] | None) -> list[str]:
    tag_list = list(tags or [])
    for extra in ("external_research", kind, "evidence:verified_outcome"):
        if extra and extra not in tag_list:
            tag_list.append(extra)
    return tag_list


def _resolve_repo_root(ctx: Any) -> Path:
    candidates: list[Path] = []
    host_repo_root = getattr(ctx, "host_repo_root", None)
    if host_repo_root:
        candidates.append(Path(host_repo_root))
    repo_dir = Path(getattr(ctx, "repo_dir", Path.cwd()))
    candidates.extend([repo_dir.parent, repo_dir, Path.cwd()])
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "umbrella").exists() and (resolved / "workspaces").exists():
            return resolved
    return (Path(host_repo_root) if host_repo_root else Path.cwd()).resolve()


def _current_workspace_id_from_drive(ctx: Any) -> str:
    drive_root = Path(getattr(ctx, "drive_root", "") or "")
    parts = list(drive_root.parts)
    if "workspaces" in parts:
        idx = parts.index("workspaces")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


def _workspace_memory_root(repo_root: Path, workspace_id: str, ctx: Any) -> Path:
    override = getattr(ctx, "workspace_root_overrides", {}) or {}
    if isinstance(override, dict) and workspace_id in override:
        return Path(str(override[workspace_id])) / ".memory"
    return repo_root / "workspaces" / workspace_id / ".memory"
