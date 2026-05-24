"""Umbrella-owned skill handlers for deep-agent adapters."""

import json
import logging
import tomllib
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)

_WORKSPACE_TOML_KNOWN_SKILLS: tuple[str, ...] = ("multi_agent_gmas",)


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _umbrella_tools():
    """Import the broader bridge lazily to avoid registry-load cycles."""

    from ouroboros.tools import umbrella_tools  # noqa: PLC0415

    return umbrella_tools


def _manifest_allowed_skills(ctx: Any) -> set[str] | None:
    overlays = getattr(ctx, "context_overlays", None)
    manifest = overlays.get("phase_manifest") if isinstance(overlays, dict) else None
    if not isinstance(manifest, dict) or "allowed_skills" not in manifest:
        return None
    raw = manifest.get("allowed_skills") or []
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def load_skill(
    ctx: Any,
    slug: str,
    max_chars: int = 40000,
) -> str:
    """Load full procedural skill pack text by slug."""

    try:
        from umbrella.skills.loader import load_skill_text

        requested_slug = slug.strip()
        allowed = _manifest_allowed_skills(ctx)
        if allowed is not None and requested_slug not in allowed:
            return _json(
                {
                    "status": "blocked",
                    "reason": "skill_not_allowed_for_phase",
                    "slug": requested_slug,
                    "allowed_skills": sorted(allowed),
                }
            )
        repo_root = _umbrella_tools()._resolve_umbrella_repo_root(ctx)
        text = load_skill_text(repo_root, requested_slug)
        if not text:
            return _json(
                {
                    "status": "not_found",
                    "slug": requested_slug,
                    "hint": "Skill not found in umbrella/skills/library/<slug>/SKILL.md",
                }
            )
        limited = text[: max(1000, int(max_chars))]
        return _json(
            {
                "status": "ok",
                "slug": requested_slug,
                "truncated": len(limited) < len(text),
                "content": limited,
            }
        )
    except Exception as exc:
        log.error("Skill load failed: %s", exc, exc_info=True)
        return f"WARNING: load_skill error: {exc}"


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _upsert_workspace_toml_skill(toml_text: str, skill_id: str, enabled: bool) -> str:
    """Add/update ``[skills] <skill_id> = <enabled>`` in TOML text.

    Hand-rolled because ``tomllib`` is read-only and a full TOML writer would
    add dependency weight just for one boolean. The line edit preserves
    surrounding formatting and comments.
    """

    lines = toml_text.splitlines()
    in_skills = False
    skills_start: int | None = None
    skills_end: int | None = None
    rendered_value = _format_toml_value(enabled)

    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            if in_skills and skills_end is None:
                skills_end = idx
            if section.lower() == "skills":
                in_skills = True
                skills_start = idx
            else:
                in_skills = False
            continue
        if in_skills:
            key_part = stripped.split("=", 1)[0].strip()
            if key_part == skill_id:
                lines[idx] = f"{skill_id} = {rendered_value}"
                return "\n".join(lines) + ("\n" if toml_text.endswith("\n") else "")

    if skills_start is not None:
        insert_at = skills_end if skills_end is not None else len(lines)
        while insert_at > skills_start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"{skill_id} = {rendered_value}")
        return "\n".join(lines) + ("\n" if toml_text.endswith("\n") else "")

    block = ["", "[skills]", f"{skill_id} = {rendered_value}"]
    suffix = "\n" if toml_text.endswith("\n") or not toml_text else ""
    if not toml_text.strip():
        return "[skills]\n" + f"{skill_id} = {rendered_value}\n"
    return toml_text.rstrip("\n") + "\n" + "\n".join(block).lstrip("\n") + suffix


def _invalidate_skill_cache(repo_root: Path, workspace_id: str) -> None:
    """Drop ``active_skills.json`` so the next attempt re-detects skills."""

    try:
        from umbrella.integration.ouroboros_bridge import workspace_drive_root  # type: ignore
    except Exception:
        try:
            from umbrella.integration.ouroboros_bridge import (  # type: ignore
                _drive_root_for as workspace_drive_root,
            )
        except Exception:
            return
    try:
        drive_root = workspace_drive_root(repo_root, workspace_id)
        cache = Path(drive_root) / "state" / "active_skills.json"
        if cache.exists():
            cache.unlink()
    except Exception:
        log.debug("skill cache invalidation failed", exc_info=True)


def configure_workspace_skills(
    ctx: Any,
    workspace_id: str,
    skill_id: str,
    enabled: bool,
    reason: str = "",
) -> str:
    """Override a named workspace skill via ``workspace.toml``."""

    try:
        skill_id = (skill_id or "").strip().lower()
        if not skill_id:
            return _json({"status": "blocked", "reason": "skill_id_required"})
        if skill_id not in _WORKSPACE_TOML_KNOWN_SKILLS:
            return _json(
                {
                    "status": "blocked",
                    "reason": "unknown_skill",
                    "skill_id": skill_id,
                    "known": list(_WORKSPACE_TOML_KNOWN_SKILLS),
                    "hint": (
                        "This tool only knows skills that participate in "
                        "verification gates. For ad-hoc workspace.toml "
                        "edits use update_workspace_seed."
                    ),
                }
            )

        bridge = _umbrella_tools()
        repo_root = bridge._resolve_umbrella_repo_root(ctx)
        seed_path = bridge._workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := bridge._stop_requested_block(
            ctx, tool_name="configure_workspace_skills", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not seed_path.exists():
            return _json(
                {
                    "status": "blocked",
                    "reason": "workspace_not_found",
                    "workspace_id": workspace_id,
                }
            )

        toml_path = seed_path / "workspace.toml"
        original = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""

        try:
            current = tomllib.loads(original) if original.strip() else {}
        except Exception as exc:
            return _json(
                {
                    "status": "blocked",
                    "reason": "workspace_toml_unparseable",
                    "error": str(exc),
                    "next_step": (
                        "Read workspace.toml, fix the syntax with "
                        "update_workspace_seed, then call this tool again."
                    ),
                }
            )

        skills = current.get("skills") if isinstance(current, dict) else None
        existing_value = skills.get(skill_id) if isinstance(skills, dict) else None
        if existing_value is bool(enabled):
            _invalidate_skill_cache(repo_root, workspace_id)
            return _json(
                {
                    "status": "noop",
                    "skill_id": skill_id,
                    "enabled": bool(enabled),
                    "workspace_toml": str(toml_path.relative_to(repo_root)),
                    "note": "value already set; refreshed skill cache anyway",
                }
            )

        updated = _upsert_workspace_toml_skill(original, skill_id, bool(enabled))
        toml_path.write_text(updated, encoding="utf-8")
        _invalidate_skill_cache(repo_root, workspace_id)

        try:
            bridge.record_workspace_event(
                ctx,
                workspace_id=workspace_id,
                event_type="change",
                summary=f"workspace.toml: skills.{skill_id} = {bool(enabled)}",
                details=(reason or "no reason supplied"),
                severity="info",
                tags="change,workspace_toml,skill_opt",
            )
        except Exception:
            log.debug("record_workspace_event failed", exc_info=True)

        return _json(
            {
                "status": "ok",
                "skill_id": skill_id,
                "enabled": bool(enabled),
                "workspace_toml": str(toml_path.relative_to(repo_root)),
                "previous_value": existing_value,
                "reason": reason,
                "note": (
                    "Skill cache invalidated; the next attempt re-runs "
                    "detection with the new policy. GMAS verification "
                    "gates are removed - this only controls whether the "
                    "GMAS context artifact is built and whether the skill "
                    "shows up in the detected-skills banner."
                ),
            }
        )
    except Exception as exc:
        log.error("configure_workspace_skills failed: %s", exc, exc_info=True)
        return _json({"status": "error", "error": str(exc)})


