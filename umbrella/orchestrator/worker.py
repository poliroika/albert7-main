import dataclasses
import json
import logging
import os
import pathlib
import re
from typing import Any, Callable

log = logging.getLogger(__name__)

from umbrella.phases.base import PhaseManifest, PhaseNode, RequiredPalaceWrite
from umbrella.phases.base import _json_ready
from umbrella.memory.palace.facade import MemPalace
from umbrella.memory.proactive.compiler import ProactiveMemoryCompiler
from umbrella.memory.proactive.models import ProactiveMemoryOverlay
from umbrella.deep_agent_tools.workspace_gmas import _subtask_requires_gmas_context


_PHASE_PROMPT_MAX_CHARS = 80_000
_DOMAIN_POLICY_MAX_CHARS = 20_000
_CONDITIONAL_EXECUTE_TOOLS = frozenset(
    {
        "get_gmas_context",
        "search_gmas_knowledge",
        "palace_search",
        "palace_add",
        "palace_link",
        "request_extra_subtask",
        "loop_back_to",
    }
)
_GMAS_EXECUTE_TOOLS = frozenset({"get_gmas_context", "search_gmas_knowledge"})
_PALACE_WRITE_TOOLS = frozenset({"palace_add", "palace_link"})
_CONDITIONAL_EXECUTE_SKILLS = frozenset({"gmas-overview"})


def _read_text_artifact(path: pathlib.Path, *, max_chars: int = 120000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars].rstrip()
        + "\n...[artifact truncated; call read_file on the path above for the full content]"
    )


def _repo_root_from_drive_root(drive_root: pathlib.Path | None) -> pathlib.Path:
    if drive_root is not None:
        try:
            path = pathlib.Path(drive_root).resolve()
            if path.name == "drive" and path.parent.name == ".memory":
                workspace_root = path.parent.parent
                if workspace_root.parent.name == "workspaces":
                    return workspace_root.parent.parent
        except Exception:
            pass
    return pathlib.Path.cwd()


def _read_repo_prompt_file(
    repo_root: pathlib.Path,
    rel_path: str,
    *,
    max_chars: int,
) -> dict[str, str]:
    rel = str(rel_path or "").replace("\\", "/").strip()
    if not rel:
        return {"path": rel, "content": "MISSING: empty prompt path."}
    root_for_check = repo_root.resolve()
    path = (repo_root / rel).resolve()
    if not path.is_file():
        package_root = pathlib.Path(__file__).resolve().parents[2]
        fallback = (package_root / rel).resolve()
        if fallback.is_file():
            path = fallback
            root_for_check = package_root.resolve()
    try:
        if not path.is_relative_to(root_for_check):
            return {
                "path": rel,
                "content": "MISSING: prompt path resolves outside the repository.",
            }
    except AttributeError:  # pragma: no cover - Python <3.9 compatibility
        root_text = str(root_for_check)
        if not str(path).startswith(root_text):
            return {
                "path": rel,
                "content": "MISSING: prompt path resolves outside the repository.",
            }
    return {
        "path": rel,
        "content": _read_text_artifact(path, max_chars=max_chars)
        or "MISSING: prompt file could not be read.",
    }


def phase_prompt_sections_for_manifest(
    manifest: PhaseManifest,
    *,
    repo_root: pathlib.Path,
) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for kind, paths in (
        ("system", manifest.prompt_files.system),
        ("user_overlay", manifest.prompt_files.user_overlay),
        ("charter_block", manifest.prompt_files.charter_blocks),
    ):
        for rel_path in paths:
            section = _read_repo_prompt_file(
                repo_root,
                rel_path,
                max_chars=_PHASE_PROMPT_MAX_CHARS,
            )
            section["kind"] = kind
            section["title"] = f"{kind}: {rel_path}"
            sections.append(section)
    return sections


def _llm_agent_domain_policy_sections(
    *,
    repo_root: pathlib.Path,
    detected_domains: set[str],
    gmas_prewrite_required: bool,
    manifest_id: str = "",
) -> list[dict[str, str]]:
    lower_domains = {domain.lower() for domain in detected_domains}
    if manifest_id == "execute":
        applies = gmas_prewrite_required
    else:
        applies = "multi_agent_gmas" in lower_domains or gmas_prewrite_required
    if not applies:
        return []
    section = _read_repo_prompt_file(
        repo_root,
        "umbrella/prompts/policies/llm_agent_runtime.md",
        max_chars=_DOMAIN_POLICY_MAX_CHARS,
    )
    section["kind"] = "domain_policy"
    section["title"] = "LLM/agent runtime policy"
    return [section]


def _artifact_matches_run(content: str, run_id: str | None) -> bool:
    if not run_id or not content.strip():
        return True
    try:
        data = json.loads(content)
    except Exception:
        return True
    artifact_run_id = str(data.get("run_id") or "")
    if artifact_run_id:
        return artifact_run_id == run_id
    task_id = str(data.get("task_id") or "")
    return not task_id or task_id.startswith(f"{run_id}:")


def _phase_plan_json_matches_run(content: str, run_id: str | None) -> bool:
    if not run_id or not content.strip():
        return True
    try:
        data = json.loads(content)
    except Exception:
        return True
    artifact_run_id = str(data.get("run_id") or "")
    if artifact_run_id:
        return artifact_run_id == run_id
    task_id = str(data.get("task_id") or "")
    return bool(task_id and task_id.startswith(f"{run_id}:"))


def _artifact_read_placeholder(path: str, *, required: bool = True) -> str:
    action = "READ REQUIRED" if required else "READ ON DEMAND"
    return (
        f"{action}: call `read_file(file_path=\"{path}\")` in this phase. "
        "Umbrella does not inline this review handoff artifact, so the file "
        "read is the single authoritative source and prompt context stays "
        "bounded."
    )


def _palace_write_tool_for_rule(manifest: PhaseManifest, rule: Any) -> str:
    if (
        getattr(rule, "store", "") == "palace.durable"
        and "promote_to_durable" in manifest.allowed_tools
        and "promote_to_durable" not in manifest.forbidden_tools
    ):
        return "promote_to_durable"
    return "palace_add"


def _palace_write_tools_for_rule(manifest: PhaseManifest, rule: Any) -> list[str]:
    allowed = set(manifest.allowed_tools or ())
    forbidden = set(manifest.forbidden_tools or ())
    store = str(getattr(rule, "store", "") or "")
    tools: list[str] = []
    if "palace_add" in allowed and "palace_add" not in forbidden:
        tools.append("palace_add")
    if (
        store == "palace.run"
        and "propose_phase_plan" in allowed
        and "propose_phase_plan" not in forbidden
    ):
        tools.append("propose_phase_plan")
    if (
        store == "palace.durable"
        and "promote_to_durable" in allowed
        and "promote_to_durable" not in forbidden
    ):
        tools.append("promote_to_durable")
    return tools or [_palace_write_tool_for_rule(manifest, rule)]


def authoritative_artifacts_for_phase(
    *,
    manifest_id: str,
    drive_root: pathlib.Path | None,
    run_id: str | None = None,
) -> list[dict[str, str]]:
    if drive_root is None:
        return []
    artifacts: list[dict[str, str]] = []
    if manifest_id in {
        "execute",
        "final_review",
        "verify",
        "subtask_review",
        "reflexion",
    }:
        path = drive_root / "state" / "phase_plan.json"
        content = _read_text_artifact(path)
        if not _phase_plan_json_matches_run(content, run_id):
            content = ""
        artifacts.append(
            {
                "title": "Current phase plan state",
                "path": ".memory/drive/state/phase_plan.json",
                "content": content
                or "MISSING: no current phase plan state was found at this path.",
                "format": "json",
            }
        )
    if manifest_id in {"plan_review", "final_review", "verify"}:
        rel_path = ".memory/drive/state/phase_plan_submitted_latest.json"
        if manifest_id == "plan_review":
            content = _artifact_read_placeholder(rel_path)
        else:
            path = drive_root / "state" / "phase_plan_submitted_latest.json"
            content = _read_text_artifact(path)
            if not _artifact_matches_run(content, run_id):
                content = ""
        artifacts.append(
            {
                "title": "Submitted phase plan contract",
                "path": rel_path,
                "content": content
                or "MISSING: no submitted phase plan artifact was found at this path.",
                "format": "json",
            }
        )
    if manifest_id in {"research_review", "plan_review"}:
        rel_path = ".memory/drive/state/research_summary_latest.json"
        content = _artifact_read_placeholder(
            rel_path,
            required=manifest_id == "research_review",
        )
        artifacts.append(
            {
                "title": "Latest research summary artifact",
                "path": rel_path,
                "content": content
                or "MISSING: no latest research summary artifact was found for this run.",
                "format": "json",
            }
        )
    return artifacts


def render_phase_user_prompt(
    manifest: PhaseManifest,
    recall_bundle: Any,
    authoritative_artifacts: list[dict[str, str]] | None = None,
    phase_prompt_sections: list[dict[str, str]] | None = None,
    domain_policy_sections: list[dict[str, str]] | None = None,
    workspace_id: str = "",
    phase_node: PhaseNode | None = None,
    gmas_prewrite_required: bool = False,
    research_depth: str = "",
    proactive_overlay: ProactiveMemoryOverlay | None = None,
    subtask_memory_markdown: str = "",
    external_catalog_markdown: str = "",
    harness_contract_markdown: str = "",
    allowed_tools: list[str] | None = None,
    allowed_skills: list[str] | None = None,
) -> str:
    bundle = recall_bundle
    effective_allowed_tools = (
        sorted(set(allowed_tools))
        if allowed_tools is not None
        else sorted(manifest.allowed_tools)
    )
    effective_allowed_skills = (
        sorted(set(allowed_skills))
        if allowed_skills is not None
        else sorted(manifest.allowed_skills)
    )
    lines: list[str] = [f"# Phase: {manifest.id}", f"## Goal", manifest.description, ""]
    if proactive_overlay is not None and proactive_overlay.sections:
        lines.append(proactive_overlay.render_markdown())
        lines.append("")
    if external_catalog_markdown.strip():
        lines.append(external_catalog_markdown.strip())
        lines.append("")
    if harness_contract_markdown.strip():
        lines.append("## Umbrella harness contract")
        lines.append(
            "This compact contract is selected by Umbrella for planning or for "
            "the active subtask. It constrains proof/tool/memory shape without "
            "choosing the implementation for you."
        )
        lines.append(harness_contract_markdown.strip())
        lines.append("")
    if manifest.id == "research" and research_depth:
        lines.append("## Research depth")
        lines.append(
            f"Umbrella selected `{research_depth}` for this phase. Follow the "
            "phase prompt's depth rules before deciding whether external "
            "GitHub/web/deep-search/MCP discovery is mandatory."
        )
        lines.append("")
    if domain_policy_sections:
        lines.append("## Umbrella domain policy capsules")
        lines.append(
            "These policy capsules are selected by Umbrella from detected "
            "workspace domains and apply before phase-specific tactics."
        )
        for section in domain_policy_sections:
            title = section.get("title") or "Policy"
            path = section.get("path") or ""
            content = section.get("content") or ""
            lines.append(f"### {title}")
            if path:
                lines.append(f"Path: `{path}`")
            lines.append("```md")
            lines.append(content)
            lines.append("```")
        lines.append("")
    if phase_prompt_sections:
        lines.append("## Phase instructions loaded from manifest")
        lines.append(
            "These files are the active phase prompt contract from the "
            "PhaseManifest, loaded by Umbrella into this task."
        )
        for section in phase_prompt_sections:
            title = section.get("title") or "Prompt"
            path = section.get("path") or ""
            content = section.get("content") or ""
            lines.append(f"### {title}")
            if path:
                lines.append(f"Path: `{path}`")
            lines.append("```md")
            lines.append(content)
            lines.append("```")
        lines.append("")
    if authoritative_artifacts:
        heading = (
            "## Authoritative review artifacts"
            if manifest.id.endswith("_review")
            else "## Authoritative phase artifacts"
        )
        lines.append(heading)
        lines.append(
            "Use these artifacts as the source of truth for this phase. Palace "
            "or hot-context snippets can include older drafts or truncated "
            "copies; when they conflict, the artifact below wins."
        )
        for artifact in authoritative_artifacts:
            title = artifact.get("title") or "Artifact"
            path = artifact.get("path") or ""
            content = artifact.get("content") or ""
            fence = artifact.get("format") or ""
            lines.append(f"### {title}")
            if path:
                lines.append(f"Path: `{path}`")
            lines.append(f"```{fence}")
            lines.append(content)
            lines.append("```")
        lines.append("")
    if phase_node and isinstance(phase_node.overlay, dict):
        watcher_lesson = str(phase_node.overlay.get("watcher_lesson") or "").strip()
        retry_reason = str(phase_node.overlay.get("retry_reason") or "").strip()
        revision_contract = phase_node.overlay.get("revision_contract")
        retry_context = phase_node.overlay.get("retry_context")
        if watcher_lesson:
            lines.append("## Watcher correction (required)")
            lines.append(
                "Umbrella Watcher detected repeated semantic tool failures in the "
                "previous attempt. Follow this correction before continuing; do not "
                "repeat the same failing tool pattern."
            )
            lines.append(watcher_lesson)
            lines.append("")
        if retry_reason or revision_contract or retry_context:
            lines.append("## Active retry/revision contract")
            lines.append(
                "This phase is being retried after an Umbrella control-plane "
                "gate. Treat these revisions or retry facts as required "
                "acceptance criteria for the new attempt, not as optional "
                "notes."
            )
            if isinstance(revision_contract, dict):
                lines.append("```json")
                lines.append(json.dumps(revision_contract, ensure_ascii=False, indent=2))
                lines.append("```")
            elif isinstance(retry_context, dict):
                lines.append("```json")
                lines.append(json.dumps(retry_context, ensure_ascii=False, indent=2))
                lines.append("```")
            else:
                lines.append(retry_reason)
            lines.append("")
    if manifest.id == "execute" and phase_node and phase_node.subtasks:
        pending = [card for card in phase_node.subtasks if card.status != "done"]
        lines.append("## Current execute subtask queue")
        lines.append(
            "The accepted phase plan has been projected into executable subtask "
            "cards. Work on one pending card only in this phase run."
        )
        if pending:
            current = pending[0]
            lines.append(
                f"Current subtask: `{current.id}` — {current.title}"
            )
            if current.goal:
                lines.append(f"Goal: {current.goal}")
            if current.proof is not None:
                lines.append(
                    "Proof contract:"
                )
                lines.append("```json")
                lines.append(json.dumps(_json_ready(dataclasses.asdict(current.proof)), ensure_ascii=False, indent=2))
                lines.append("```")
            if gmas_prewrite_required:
                lines.append(
                    "GMAS/LLM pre-write gate: this current subtask implements "
                    "LLM/GMAS agent, bot, judge, tool, or memory behavior. "
                    "Call `get_gmas_context` or "
                    "`search_gmas_knowledge` with a concrete implementation "
                    "query before the first `apply_workspace_patch`, "
                    "`update_workspace_seed`, `repo_write_commit`, or other "
                    "workspace write. Prefetched context from research/plan is "
                    "useful background, not a substitute. Use a specific query "
                    "for the API shape you need; do not invent GMAS imports, "
                    "constructors, or tool names from memory."
                )
            lines.append(
                "Completion call: `mark_subtask_complete(completion_contract={...})` "
                f"after verifier-backed evidence exists for `{current.id}`."
            )
        else:
            lines.append(
                "All plan subtasks are marked done. If this execute phase was "
                "reopened by a review/verification failure, repair only the "
                "reported integration gap and then call `mark_subtask_complete` "
                "with phase-level evidence."
            )
        lines.append("Subtask statuses:")
        for card in phase_node.subtasks:
            proof_kind = (
                f" — proof: {card.proof.execution.kind}"
                if card.proof is not None
                else ""
            )
            lines.append(f"- `{card.id}` [{card.status}] {card.title}{proof_kind}")
        lines.append("")
        if subtask_memory_markdown.strip():
            lines.append(subtask_memory_markdown.strip())
            lines.append("")
    _supplemental_notice = (
        "These snippets are evidence/archive hints, not behavioral rules. "
        "If they conflict with [ALWAYS-LOADED MEMORY] or authoritative artifacts, ignore them."
    )
    if bundle.always_on:
        lines.append(
            "## Supplemental evidence memory — configured archive recall (NON-DIRECTIVE)"
        )
        lines.append(_supplemental_notice)
        for node in bundle.always_on[:5]:
            lines.append(f"- {node.get('content', '')[:300]}")
        lines.append("")
    if bundle.hot:
        lines.append(
            "## Supplemental evidence memory — current run recall (NON-DIRECTIVE; verify against artifacts)"
        )
        lines.append(_supplemental_notice)
        for node in bundle.hot[:5]:
            content = str(node.get("content", "") or "")
            limit = 6000 if manifest.id.endswith("_review") else 300
            if len(content) > limit:
                content = content[:limit].rstrip() + "\n...[hot context truncated]"
            lines.append(f"- {content}")
        lines.append("")
    if bundle.warm:
        lines.append(
            "## Supplemental evidence memory — cross-run search (NON-DIRECTIVE; verify before use)"
        )
        lines.append(_supplemental_notice)
        for node in bundle.warm[:5]:
            content = str(node.get("content", "") or "")
            if len(content) > 500:
                content = content[:500].rstrip() + "\n...[warm context truncated]"
            lines.append(f"- {content}")
        lines.append("")
    lines.append("## Your allowed tools for this phase")
    lines.extend(f"- {t}" for t in effective_allowed_tools)
    if effective_allowed_skills:
        lines.append(
            "\n## Recommended skills (load with `load_skill`, not `enable_tools`)"
        )
        lines.append(
            "These are skill slugs, not tool names. When one is relevant, call "
            "`load_skill(slug=\"<slug>\")`. `enable_tools` accepts only tool names "
            "from the allowed tool list or `list_available_tools`."
        )
        lines.extend(f"- {s}" for s in effective_allowed_skills)
    if manifest.exit_criteria.required_calls:
        lines.append("\n## Required phase-completion tool calls")
        lines.extend(f"- {t}" for t in sorted(manifest.exit_criteria.required_calls))
        lines.append(
            "The Umbrella phase is not complete until these calls are accepted."
        )
    if manifest.exit_criteria.required_prior_calls:
        lines.append("\n## Required tool checks before completion")
        lines.extend(
            f"- {t}" for t in sorted(manifest.exit_criteria.required_prior_calls)
        )
        lines.append(
            "Do not call the phase-completion tool until each required check "
            "has returned a successful result in this phase."
        )
    palace_rules = _effective_palace_write_rules(
        manifest,
        research_depth=research_depth,
    )
    if palace_rules:
        lines.append("\n## Required palace writes before completion")
        required_write_tools: set[str] = set()
        for rule in palace_rules:
            tag_hint = f" with tag `{rule.tag}`" if rule.tag else ""
            tool_name = _palace_write_tool_for_rule(manifest, rule)
            required_write_tools.add(tool_name)
            lines.append(
                f"- Call `{tool_name}` at least {max(1, int(rule.n or 1))} "
                f"time(s) for `{rule.store}`{tag_hint}."
            )
        phase_tags: list[str] = []
        for name, rule in manifest.memory.write_rules.items():
            if getattr(rule, "store", "") in {r.store for r in palace_rules}:
                phase_tags.append(name)
        if phase_tags:
            lines.append(
                "Use concrete `content`; include one of these phase memory tags "
                f"when applicable: {', '.join(f'`{tag}`' for tag in sorted(phase_tags))}."
            )
        if workspace_id:
            lines.append(
                "If you provide `palace_path`, prefer a run-scoped workspace path "
                f"such as `workspaces/{workspace_id}/{manifest.id}`; the logical "
                "store is inferred from the phase manifest."
            )
        tool_list = ", ".join(f"`{tool}`" for tool in sorted(required_write_tools))
        lines.append(
            f"Do not call the completion tool until the required {tool_list} "
            "calls have returned accepted/saved results."
        )
    return "\n".join(lines)


def _load_detected_domains_from_drive(drive_root: pathlib.Path | None) -> set[str]:
    if drive_root is None:
        return set()
    try:
        workspace_root = pathlib.Path(drive_root).parent.parent
        from umbrella.contracts.platform_context import overlay_hints_from_declaration

        hints = overlay_hints_from_declaration(drive_root, workspace_root)
        declared = hints.get("detected_domains") or []
        if declared:
            return {str(value) for value in declared if str(value).strip()}
    except Exception:
        pass
    return set()


def _phase_node_needs_gmas_prewrite(
    phase_node: PhaseNode, detected_domains: set[str]
) -> bool:
    if phase_node.manifest_id != "execute":
        return "multi_agent_gmas" in {domain.lower() for domain in detected_domains}
    for card in phase_node.subtasks or []:
        if card.status == "done":
            continue
        subtask = {
            "id": card.id,
            "title": card.title,
            "goal": card.goal,
            "proof": _json_ready(dataclasses.asdict(card.proof)) if card.proof else {},
            "files_to_create": list(card.files_to_create or []),
            "files_to_change": list(card.files_to_change or []),
            "files_affected": list(card.files_affected or []),
        }
        return _subtask_requires_gmas_context(subtask)
    return False


def _string_values(value: Any) -> set[str]:
    if isinstance(value, str):
        text = value.strip()
        return {text} if text else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _active_subtask_values(active_subtask: dict[str, Any] | None, key: str) -> set[str]:
    if not isinstance(active_subtask, dict):
        return set()
    return _string_values(active_subtask.get(key))


def _subtask_memory_requests_palace_search(
    active_subtask: dict[str, Any] | None,
    scope_payload: dict[str, Any] | None,
) -> bool:
    scope = (
        active_subtask.get("memory_scope")
        if isinstance(active_subtask, dict)
        and isinstance(active_subtask.get("memory_scope"), dict)
        else {}
    )
    if isinstance(scope, dict) and (
        scope.get("palace_search_queries") or scope.get("search_queries")
    ):
        return True
    assets = scope.get("assets") if isinstance(scope, dict) else []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        kind = str(asset.get("kind") or asset.get("type") or "").strip().lower()
        mode = str(asset.get("inject_mode") or asset.get("mode") or "").strip().lower()
        if mode == "search_only" or kind in {"palace_finding", "gmas_context"}:
            return True
    chunks = (
        scope_payload.get("chunks")
        if isinstance(scope_payload, dict) and isinstance(scope_payload.get("chunks"), list)
        else []
    )
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        kind = str(chunk.get("kind") or "").strip().lower()
        mode = str(chunk.get("inject_mode") or "").strip().lower()
        reason = str(chunk.get("reason") or "").strip().lower()
        loaded = bool(chunk.get("loaded"))
        if mode == "search_only" or kind in {"palace_finding", "gmas_context"}:
            return True
        if not loaded and "palace_search" in reason:
            return True
    return False


def _effective_phase_allowed_tools(
    manifest: PhaseManifest,
    *,
    active_subtask: dict[str, Any] | None,
    gmas_prewrite_required: bool,
    research_depth: str = "",
    subtask_memory_scope_payload: dict[str, Any] | None,
    palace_rules: list[RequiredPalaceWrite],
) -> list[str]:
    allowed = set(manifest.allowed_tools or ())
    denied = set(manifest.forbidden_tools or ())
    if (
        manifest.id in {"research", "research_review", "plan", "plan_review"}
        and not gmas_prewrite_required
        and str(research_depth or "").strip().lower() != "full"
    ):
        allowed -= _GMAS_EXECUTE_TOOLS
    if manifest.id == "execute":
        allowed -= _CONDITIONAL_EXECUTE_TOOLS
        subtask_tools = _active_subtask_values(active_subtask, "allowed_tools")
        allowed |= subtask_tools
        if gmas_prewrite_required:
            allowed |= _GMAS_EXECUTE_TOOLS
        if _subtask_memory_requests_palace_search(
            active_subtask,
            subtask_memory_scope_payload,
        ):
            allowed.add("palace_search")
        if palace_rules:
            allowed |= _PALACE_WRITE_TOOLS
    return sorted(allowed - denied)


def _effective_phase_allowed_skills(
    manifest: PhaseManifest,
    *,
    active_subtask: dict[str, Any] | None,
    gmas_prewrite_required: bool,
    research_depth: str = "",
) -> list[str]:
    allowed = set(manifest.allowed_skills or ())
    if (
        manifest.id in {"research", "research_review", "plan"}
        and not gmas_prewrite_required
        and str(research_depth or "").strip().lower() != "full"
    ):
        allowed -= {"gmas-overview", "gmas-pattern-author"}
    if manifest.id == "execute":
        allowed -= _CONDITIONAL_EXECUTE_SKILLS
        allowed |= _active_subtask_values(active_subtask, "allowed_skills")
        if gmas_prewrite_required:
            allowed.add("gmas-overview")
    return sorted(allowed)


def _research_depth_for_phase(
    phase_node: PhaseNode,
    *,
    detected_domains: set[str],
    query_seed: str,
    drive_root: pathlib.Path | None = None,
    run_id: str = "",
) -> str:
    del detected_domains, query_seed
    if isinstance(phase_node.overlay, dict):
        requested = str(phase_node.overlay.get("research_depth") or "").strip().lower()
        if requested in {"none", "light", "full"}:
            return requested
    if phase_node.manifest_id not in {"research", "research_review", "plan", "plan_review"}:
        return ""
    from umbrella.orchestrator.preflight_depth import read_preflight_research_depth

    preflight_depth = read_preflight_research_depth(drive_root, run_id=run_id)
    if preflight_depth in {"none", "light", "full"}:
        return preflight_depth
    return "light"


def _research_depth_min_write_count(depth: str, configured: int) -> int:
    value = str(depth or "").strip().lower()
    if value == "none":
        return 0
    if value == "light":
        return 1 if configured > 0 else 0
    return configured


def _effective_palace_write_rules(
    manifest: PhaseManifest,
    *,
    research_depth: str = "",
) -> list[RequiredPalaceWrite]:
    rules: list[RequiredPalaceWrite] = list(
        manifest.exit_criteria.required_palace_writes
    )
    for rule in manifest.exit_criteria.min_palace_writes:
        try:
            configured = max(1, int(rule.n or 1))
        except (TypeError, ValueError):
            configured = 1
        effective_n = configured
        if manifest.id == "research":
            effective_n = _research_depth_min_write_count(research_depth, configured)
        if effective_n <= 0:
            continue
        if effective_n != configured:
            rules.append(dataclasses.replace(rule, n=effective_n))
        else:
            rules.append(rule)
    return rules


def _phase_recall_query_seed(
    *, manifest: PhaseManifest, phase_node: PhaseNode, drive_root: pathlib.Path | None
) -> str:
    parts: list[str] = [manifest.id, manifest.description]
    if phase_node.id:
        parts.append(phase_node.id)
    if isinstance(phase_node.overlay, dict):
        keys = (
            ("revision_contract",)
            if phase_node.overlay.get("revision_contract")
            else ("retry_context", "retry_reason")
        )
        for key in keys:
            value = phase_node.overlay.get(key)
            if value:
                parts.append(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)
    for card in phase_node.subtasks or []:
        if card.status == "done":
            continue
        parts.extend(
            str(part or "")
            for part in (
                card.id,
                card.title,
                card.goal,
                json.dumps(_json_ready(dataclasses.asdict(card.proof)), ensure_ascii=False)
                if card.proof is not None
                else "",
            )
        )
        break
    if drive_root is not None:
        try:
            workspace_root = pathlib.Path(drive_root).parent.parent
            task_text = (workspace_root / "TASK_MAIN.md").read_text(
                encoding="utf-8", errors="replace"
            )
            if task_text.strip():
                parts.append(task_text.strip())
        except OSError:
            pass
    return "\n".join(part.strip() for part in parts if part and part.strip())[:4000]


def _phase_task_id(*, run_id: str, phase_node: PhaseNode) -> str:
    base = f"{run_id}:{phase_node.id}"
    started_at = phase_node.started_at
    if started_at is None:
        return base
    try:
        attempt_ms = int(float(started_at) * 1000)
    except (TypeError, ValueError, OverflowError):
        return base
    return f"{base}:{attempt_ms}"


def build_phase_task(
    *,
    phase_node: PhaseNode,
    manifest: PhaseManifest,
    workspace_id: str,
    run_id: str,
    palace: MemPalace,
    drive_root: pathlib.Path | None = None,
    repo_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    resolved_repo_root = pathlib.Path(repo_root) if repo_root is not None else (
        _repo_root_from_drive_root(drive_root)
    )
    workspace_root_early = resolved_repo_root / "workspaces" / workspace_id
    from umbrella.contracts.platform_context import overlay_hints_from_declaration

    hint_overlay = overlay_hints_from_declaration(drive_root, workspace_root_early)
    detected_domains = set(hint_overlay.get("detected_domains") or [])
    gmas_prewrite_required = _phase_node_needs_gmas_prewrite(
        phase_node, detected_domains
    )
    phase_prompt_sections = phase_prompt_sections_for_manifest(
        manifest,
        repo_root=resolved_repo_root,
    )
    domain_policy_sections = _llm_agent_domain_policy_sections(
        repo_root=resolved_repo_root,
        detected_domains=detected_domains,
        gmas_prewrite_required=gmas_prewrite_required,
        manifest_id=manifest.id,
    )
    query_seed = _phase_recall_query_seed(
        manifest=manifest,
        phase_node=phase_node,
        drive_root=drive_root,
    )
    research_depth = _research_depth_for_phase(
        phase_node,
        detected_domains=detected_domains,
        query_seed=query_seed,
        drive_root=drive_root,
        run_id=run_id,
    )
    active_subtask_id: str | None = None
    if manifest.id == "execute" and phase_node.subtasks:
        pending_cards = [c for c in phase_node.subtasks if c.status != "done"]
        if pending_cards:
            active_subtask_id = pending_cards[0].id
    proactive_overlay = ProactiveMemoryCompiler().build_overlay(
        repo_root=resolved_repo_root,
        workspace_id=workspace_id,
        run_id=run_id,
        phase_id=phase_node.manifest_id,
        subtask_id=active_subtask_id,
        task_brief=query_seed,
        manifest=manifest,
        drive_root=drive_root,
    )
    import hashlib

    proactive_markdown = proactive_overlay.render_markdown()
    proactive_overlay_hash = hashlib.sha256(
        proactive_markdown.encode("utf-8")
    ).hexdigest()
    directive_sections = [
        str(section.get("name") or "")
        for section in proactive_overlay.to_payload().get("sections", [])
        if isinstance(section, dict) and section.get("name")
    ]
    graph_policy = getattr(manifest.memory, "graph", None)
    recall = palace.recall(
        phase_node.manifest_id,
        run_id=run_id,
        always_on_rules=manifest.memory.always_on,
        hot_rules=manifest.memory.hot,
        warm_search_rules=manifest.memory.warm_search,
        query_seed=query_seed,
        graph_policy=graph_policy,
    )
    authoritative_artifacts = authoritative_artifacts_for_phase(
        manifest_id=manifest.id,
        drive_root=drive_root,
        run_id=run_id,
    )
    active_subtask: dict[str, Any] | None = None
    subtask_memory_markdown = ""
    subtask_memory_scope_payload: dict[str, Any] | None = None
    workspace_root = pathlib.Path(repo_root) / "workspaces" / workspace_id if workspace_id else pathlib.Path(repo_root)
    if manifest.id == "execute" and phase_node.subtasks:
        pending = [card for card in phase_node.subtasks if card.status != "done"]
        if pending:
            active_subtask = _json_ready(dataclasses.asdict(pending[0]))
            from umbrella.context.subtask_memory import (
                infer_memory_scope_from_subtask,
                render_subtask_memory_scope_markdown,
                resolve_subtask_memory_chunks,
            )

            scope = infer_memory_scope_from_subtask(
                active_subtask, drive_root=drive_root
            )
            chunks = resolve_subtask_memory_chunks(
                scope,
                repo_root=resolved_repo_root,
                workspace_root=workspace_root,
                workspace_id=workspace_id,
                drive_root=drive_root,
                subtask=active_subtask,
            )
            subtask_id = str(active_subtask.get("id") or "")
            subtask_memory_markdown = render_subtask_memory_scope_markdown(
                scope, chunks, subtask_id=subtask_id
            )
            subtask_memory_scope_payload = {
                "subtask_id": subtask_id,
                "scope": scope.to_dict(),
                "chunks": [
                    {
                        "kind": c.kind,
                        "ref": c.ref,
                        "title": c.title,
                        "inject_mode": c.inject_mode,
                        "loaded": c.loaded,
                        "reason": c.reason,
                        "text": (c.text or "")[:4000],
                    }
                    for c in chunks
                ],
            }
            if drive_root is not None:
                scope_path = drive_root / "state" / f"subtask_memory_scope_{subtask_id}.json"
                try:
                    scope_path.parent.mkdir(parents=True, exist_ok=True)
                    scope_path.write_text(
                        json.dumps(subtask_memory_scope_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except OSError:
                    log.debug("Failed to persist subtask memory scope", exc_info=True)
    palace_rules = _effective_palace_write_rules(
        manifest,
        research_depth=research_depth,
    )
    effective_allowed_tools = _effective_phase_allowed_tools(
        manifest,
        active_subtask=active_subtask,
        gmas_prewrite_required=gmas_prewrite_required,
        research_depth=research_depth,
        subtask_memory_scope_payload=subtask_memory_scope_payload,
        palace_rules=palace_rules,
    )
    effective_allowed_skills = _effective_phase_allowed_skills(
        manifest,
        active_subtask=active_subtask,
        gmas_prewrite_required=gmas_prewrite_required,
        research_depth=research_depth,
    )
    effective_forbidden_tools = [
        tool
        for tool in (manifest.forbidden_tools or ())
        if str(tool or "") != "enable_tools"
    ]
    tool_filter = {
        "allow": effective_allowed_tools,
        "deny": effective_forbidden_tools,
        "required": list(manifest.exit_criteria.required_calls),
        "completion_prerequisites": {
            "required_tools": list(manifest.exit_criteria.required_prior_calls),
            "palace_writes": [
                {
                    "store": rule.store,
                    "tag": rule.tag or "",
                    "n": max(1, int(rule.n or 1)),
                    "tools": _palace_write_tools_for_rule(manifest, rule),
                }
                for rule in palace_rules
            ],
        },
    }
    workspace_root = resolved_repo_root / "workspaces" / workspace_id
    from umbrella.contracts.capability_declaration import (
        ensure_probe_backed_declaration,
        load_capability_declaration,
    )
    from umbrella.contracts.phase_contract_builder import build_phase_contract
    from umbrella.contracts.runtime_probes import (
        effective_runtime_capabilities,
        load_runtime_capabilities,
        persist_runtime_capabilities,
        probe_runtime_capabilities,
    )

    runtime_caps = load_runtime_capabilities(drive_root)
    if manifest.id in {"preflight", "research", "plan", "plan_review"} and drive_root is not None:
        runtime_caps = probe_runtime_capabilities(workspace_root)
        persist_runtime_capabilities(drive_root, runtime_caps)
        ensure_probe_backed_declaration(
            drive_root,
            workspace_root,
            run_id=run_id,
            workspace_id=workspace_id,
            actor="harness",
        )
    capability_declaration = (
        load_capability_declaration(drive_root).to_dict()
        if drive_root is not None and load_capability_declaration(drive_root) is not None
        else None
    )
    effective_caps = (
        effective_runtime_capabilities(drive_root)
        if drive_root is not None
        else runtime_caps
    )
    phase_contract = build_phase_contract(
        manifest=manifest,
        phase_id=manifest.id,
        workspace_policy={},
        runtime_capabilities=effective_caps,
        active_subtask=active_subtask,
    )
    if not phase_contract.ok:
        policy_conflict_payload = {
            "conflicts": phase_contract.conflicts,
            "diagnostic": phase_contract.diagnostic,
        }
    else:
        policy_conflict_payload = None
    typed_gate = None
    if isinstance(phase_node.overlay, dict):
        typed_gate = phase_node.overlay.get("typed_action_gate")
    if typed_gate and isinstance(typed_gate, dict):
        blocked = typed_gate.get("blocked_tools") or []
        allowed_next = typed_gate.get("allowed_next_tools") or []
        if blocked:
            tool_filter["deny"] = sorted(set(tool_filter["deny"]) | set(blocked))
        if allowed_next:
            tool_filter["allow"] = sorted(
                set(tool_filter["allow"]) & set(allowed_next)
                if allowed_next
                else set(tool_filter["allow"])
            )
    overlays: dict[str, Any] = {
        "phase_manifest": manifest.to_payload(),
        "phase_node": _json_ready(dataclasses.asdict(phase_node)),
        "recall_bundle": recall.to_payload(),
        "proactive_memory": proactive_overlay.to_payload(),
        "memory_backend": {
            "canonical": {"ok": True, "source_of_truth": True},
            "hindsight": {
                "enabled": os.environ.get("UMBRELLA_HINDSIGHT_ENABLED", "0")
                .strip()
                .lower()
                in {"1", "true", "yes", "on"},
                "mode": os.environ.get(
                    "UMBRELLA_MEMORY_DURABLE_BACKEND", "canonical"
                ),
                "reflect_enabled": os.environ.get(
                    "UMBRELLA_HINDSIGHT_REFLECT_ENABLED", "0"
                )
                .strip()
                .lower()
                in {"1", "true", "yes", "on"},
            },
        },
        "detected_domains": sorted(detected_domains),
        **({"research_depth": research_depth} if research_depth else {}),
        "gmas_prewrite_required": gmas_prewrite_required,
        "phase_prompt_files_loaded": [
            section.get("path", "") for section in phase_prompt_sections
        ],
        "domain_policy_files_loaded": [
            section.get("path", "") for section in domain_policy_sections
        ],
        "effective_allowed_tools": list(tool_filter.get("allow") or []),
        "effective_allowed_skills": effective_allowed_skills,
    }
    if policy_conflict_payload is not None:
        overlays["policy_conflict"] = policy_conflict_payload
    harness_contract_markdown = ""
    try:
        from umbrella.context.compiler import compile_phase_context
        from umbrella.context.render import bundle_to_overlay_dict, persist_llm_input_bundle

        task_id = _phase_task_id(run_id=run_id, phase_node=phase_node)
        capability_envelope = phase_contract.capability_envelope
        if capability_declaration is not None:
            capability_envelope["capability_declaration"] = capability_declaration
        from umbrella.contracts.platform_context import build_platform_context_envelope

        capability_envelope["platform_context"] = build_platform_context_envelope(
            drive_root=drive_root,
            workspace_root=workspace_root,
        )
        bundle = compile_phase_context(
            workspace_root=workspace_root,
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            manifest=manifest,
            phase_node=phase_node,
            tool_filter=tool_filter,
            capability_envelope=capability_envelope,
            active_subtask=active_subtask,
            phase_prompt_sections=phase_prompt_sections,
            authoritative_artifacts=authoritative_artifacts,
            recall_bundle=recall.to_payload(),
            proactive_memory=proactive_overlay.to_payload(),
            drive_root=drive_root,
            subtask_memory_chunks=(
                subtask_memory_scope_payload.get("chunks")
                if subtask_memory_scope_payload
                else None
            ),
        )
        if drive_root is not None:
            persist_llm_input_bundle(bundle, drive_root)
            from umbrella.context.render import persist_memory_injection_report

            injection_audit = proactive_overlay.to_payload().get("injection_audit") or {}
            skipped_bkb = (
                injection_audit.get("skipped_bkb")
                if isinstance(injection_audit, dict)
                else []
            )
            persist_memory_injection_report(
                bundle,
                drive_root,
                proactive_overlay_hash=proactive_overlay_hash,
                skipped_items=list(skipped_bkb) if isinstance(skipped_bkb, list) else [],
            )
        bundle_overlay = bundle_to_overlay_dict(bundle)
        overlays["llm_input_bundle"] = bundle_overlay
        overlays["harness_contract"] = bundle_overlay.get("harness_contract", {})
        overlays["llm_input_bundle_hash"] = bundle.input_hash
        harness_contract_markdown = "\n\n".join(
            item.text
            for item in bundle.contract_items
            if item.role == "harness_contract" and item.text.strip()
        )
    except Exception as exc:
        log.warning(
            "LLM input bundle compile failed for %s:%s: %s",
            run_id,
            phase_node.id,
            exc,
            exc_info=True,
        )
        overlays["llm_input_bundle_warning"] = (
            f"LLM input bundle compile failed: {exc}"
        )
    overlays["memory_injection_contract"] = {
        "owner": "umbrella.phase_runner",
        "mode": "umbrella_owned",
        "proactive_overlay_injected": True,
        "proactive_overlay_hash": proactive_overlay_hash,
        "retrieval_is_supplemental_only": True,
        "directive_surface": "task.input.proactive_memory",
        "directive_sections": directive_sections or ["proactive_memory"],
        "workspace_id": workspace_id,
        "run_id": run_id,
        "phase_id": phase_node.manifest_id,
    }
    overlays["umbrella_managed"] = True
    overlays["memory_overlay_origin"] = "umbrella.orchestrator.worker.build_phase_task"
    overlays["proactive_memory_rendered_in_task_input"] = True
    overlays["phase_prompt_rendered_by_umbrella"] = True
    overlays["prevent_ouroboros_auto_core_overlay"] = True
    overlays["memory_directive_surface"] = "task.input.proactive_memory"
    if subtask_memory_scope_payload is not None:
        overlays["subtask_memory_scope"] = subtask_memory_scope_payload
    catalog_markdown = ""
    if manifest.id in {"plan", "research"} and drive_root is not None:
        from types import SimpleNamespace

        from umbrella.discovery.external_catalog import catalog_summary_for_prompt

        catalog_markdown = catalog_summary_for_prompt(
            SimpleNamespace(drive_root=drive_root)
        )
    return {
        "id": _phase_task_id(run_id=run_id, phase_node=phase_node),
        "type": "phase_run",
        "umbrella_managed": True,
        "input": render_phase_user_prompt(
            manifest,
            recall,
            authoritative_artifacts=authoritative_artifacts,
            phase_prompt_sections=phase_prompt_sections,
            domain_policy_sections=domain_policy_sections,
            workspace_id=workspace_id,
            phase_node=phase_node,
            gmas_prewrite_required=gmas_prewrite_required,
            research_depth=research_depth,
            proactive_overlay=proactive_overlay,
            subtask_memory_markdown=subtask_memory_markdown,
            external_catalog_markdown=catalog_markdown,
            harness_contract_markdown=harness_contract_markdown,
            allowed_tools=list(tool_filter.get("allow") or effective_allowed_tools),
            allowed_skills=effective_allowed_skills,
        ),
        "workspace_id": workspace_id,
        "context_overlays": overlays,
        "tool_filter": tool_filter,
        "budgets": dataclasses.asdict(manifest.budgets),
        "role": "worker",
    }
