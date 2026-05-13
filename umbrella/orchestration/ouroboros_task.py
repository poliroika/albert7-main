"""Prompt construction for the Ouroboros-first Umbrella app."""

import json
import logging
import re
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - fallback for <3.11
    import tomli as _toml  # type: ignore[no-redef]

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


_NON_GMAS_IMPL_RE = re.compile(
    r"\b(fastapi|flask|httpx|aiohttp|uvicorn|django|"
    r"requests\.get|requests\.post|requests\.session)\b",
    re.IGNORECASE,
)
_GMAS_IMPL_RE = re.compile(
    r"\b(gmas|rolegraph|macprunner|agentprofile|build_property_graph|"
    r"graphbuilder|autographbuilder)\b",
    re.IGNORECASE,
)
_LOW_SIGNAL_MEMORY_RE = re.compile(
    r"(seed_backup_|^updated\s+\S+|^backup:|^args:\s*workspace_id=|^result:\s*$|^success:\s*true$)",
    re.IGNORECASE,
)


def _drive_root_for(repo_root: Path) -> Path:
    return repo_root / ".umbrella" / "ouroboros_drive"


def _workspace_drive_root_for(repo_root: Path, workspace_id: str) -> Path:
    clean = str(workspace_id or "").strip().replace("\\", "/").strip("/")
    if not clean or ".." in Path(clean).parts:
        return _drive_root_for(repo_root)
    return repo_root / "workspaces" / clean / ".memory" / "drive"


def load_detected_domains(repo_root: Path, workspace_id: str) -> set[str]:
    """Read detected task domains from the skill layer's cache.

    Returns the set of domain ids (e.g. ``{"multi_agent_gmas"}``) that
    Umbrella's skill detector recorded for ``workspace_id``. Empty set on
    any error or when the cache belongs to a different workspace.
    """
    path = (
        _workspace_drive_root_for(repo_root, workspace_id)
        / "state"
        / "active_skills.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    entry = data.get("entry") if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return set()
    if entry.get("workspace_id") and entry.get("workspace_id") != workspace_id:
        return set()
    raw = entry.get("domains") or []
    domains = {str(d) for d in raw if isinstance(d, str) and d}
    if _workspace_declares_gmas_disabled(repo_root, workspace_id):
        domains.discard("multi_agent_gmas")
    return domains


def _workspace_declares_gmas_disabled(repo_root: Path, workspace_id: str) -> bool:
    """Return True when workspace.toml explicitly disables GMAS skill use."""

    path = repo_root / "workspaces" / workspace_id / "workspace.toml"
    try:
        with path.open("rb") as fh:
            data: dict[str, Any] = _toml.load(fh)
    except Exception:
        return False

    skills = data.get("skills")
    if isinstance(skills, dict) and skills.get("multi_agent_gmas") is False:
        return True

    gmas = data.get("gmas")
    if isinstance(gmas, dict) and gmas.get("enabled") is False:
        return True

    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        if workspace.get("requires_gmas") is False:
            return True
        if workspace.get("multi_agent_gmas") is False:
            return True
    return False


def _looks_like_non_gmas_attempt(content: str) -> bool:
    """True if a memory entry describes a non-GMAS implementation path.

    Only used when ``multi_agent_gmas`` is in detected domains so we can
    flag previous attempts that solved the task with raw FastAPI / httpx
    / Flask. Such entries get demoted into a ``review CRITICALLY`` block
    so the agent doesn't blindly repeat the non-GMAS recipe.
    """
    if not content:
        return False
    has_non_gmas = bool(_NON_GMAS_IMPL_RE.search(content))
    has_gmas = bool(_GMAS_IMPL_RE.search(content))
    return has_non_gmas and not has_gmas


def _looks_low_signal_memory(content: str) -> bool:
    """Hide auto-generated backup/update chatter from the prompt."""
    normalized = " ".join(str(content or "").split())
    if not normalized:
        return True
    return bool(_LOW_SIGNAL_MEMORY_RE.search(normalized))


def _read_skill_artifact(drive_root: Path, name: str, *, max_chars: int) -> str:
    path = drive_root / "memory" / "knowledge" / name
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = (
            text[:max_chars].rstrip() + f"\n\n[...truncated; full {name} on disk at "
            f"`{path.relative_to(drive_root.parent.parent)}`]"
        )
    return text


def _fit_text_to_skill_budget(
    text: str,
    max_tokens: int,
    drive_root: Path,
    artifact_name: str,
) -> str:
    """Shrink skill artifact to a token budget; optional LLM summarize via env."""
    from umbrella.llm_budget import estimate_tokens

    if not text.strip():
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    import os

    path = drive_root / "memory" / "knowledge" / artifact_name
    rel_note = ""
    try:
        rel_note = f"`{path.relative_to(drive_root.parent.parent)}`"
    except ValueError:
        rel_note = str(artifact_name)

    if os.environ.get("UMBRELLA_PRIOR_KNOWLEDGE_LLM", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        try:
            from umbrella.retrieval.gmas_summarizer import summarize_chunk

            out = summarize_chunk(text, max_tokens, path)
            if out and estimate_tokens(out) <= max_tokens * 1.2:
                return out.strip()
        except Exception:
            log.debug("Prior knowledge LLM shrink failed (non-fatal)", exc_info=True)

    max_chars = max(400, int(max_tokens) * 4)
    return (
        text[:max_chars].rstrip()
        + f"\n\n[...truncated; full {artifact_name} on disk at {rel_note}]"
    )


def _lazy_detect_domains(repo_root: Path, workspace_id: str) -> set[str]:
    detected_domains = load_detected_domains(repo_root, workspace_id)
    if detected_domains:
        return detected_domains

    try:
        from umbrella.integration.ouroboros_bridge import (
            prepare_active_skills_for_workspace,
        )

        prepare_active_skills_for_workspace(repo_root, workspace_id)
        return load_detected_domains(repo_root, workspace_id)
    except Exception:
        log.debug(
            "lazy skill detection failed for %s",
            workspace_id,
            exc_info=True,
        )
        return set()


def _read_workspace_task_text(repo_root: Path, workspace_id: str) -> str:
    workspace_task_path = repo_root / "workspaces" / workspace_id / "TASK_MAIN.md"
    try:
        return workspace_task_path.read_text(encoding="utf-8")
    except OSError:
        return f"{workspace_id} workspace usage patterns"


def _trim_lines_to_token_budget(
    lines: list[str], budget: int, estimate_tokens: Any
) -> list[str]:
    out = list(lines)
    while out and estimate_tokens("\n".join(out)) > budget:
        out.pop()
    return out


def _render_skill_banner(
    *,
    drive_root: Path,
    detected_domains: set[str],
    gmas_prefetch_tokens: int,
) -> list[str]:
    chunks: list[str] = [
        "### Detected skills (auto-detected from TASK_MAIN.md by Umbrella skill layer)",
        f"Active domains: `{', '.join(sorted(detected_domains))}`",
        "",
    ]
    if "multi_agent_gmas" not in detected_domains:
        return chunks

    chunks.append(
        "**This workspace is classified as a multi-agent system task. "
        "The host repo's `gmas/` library is the intended foundation. "
        "Before your first `update_workspace_seed`, you MUST call "
        "`get_gmas_context` with a specific query (graph construction, "
        "RoleGraph + AgentProfile, MACPRunner usage, agent tools, "
        "streaming, memory) and build the implementation on real "
        "`gmas.*` APIs. Do not invent `gmas` APIs from memory and "
        "do not fall back to a plain FastAPI/httpx solution unless "
        "you have first proved with a recorded blocker that GMAS "
        "cannot deliver this task.**"
    )
    chunks.append("")
    raw_gmas = _read_skill_artifact(
        drive_root,
        "gmas_active_context.md",
        max_chars=200_000,
    )
    gmas_ctx = _fit_text_to_skill_budget(
        raw_gmas,
        gmas_prefetch_tokens,
        drive_root,
        "gmas_active_context.md",
    )
    if gmas_ctx:
        chunks.append("### GMAS active context (pre-fetched for this task)")
        chunks.append(gmas_ctx)
        chunks.append("")
    return chunks


def _render_skill_library_cards(
    *,
    repo_root: Path,
    detected_domains: set[str],
    workspace_task_text: str,
    skill_tokens: int,
    estimate_tokens: Any,
) -> list[str]:
    try:
        from umbrella.skills import (
            discover_skills,
            filter_by_domain,
            match_for_task,
            render_l1,
            render_l2,
            skill_library_root,
        )

        all_skills = discover_skills(skill_library_root(repo_root))
        active_skills = filter_by_domain(
            all_skills,
            detected_domains,
            status="active",
        )
        l1_budget = max(80, int(skill_tokens * 0.20))
        l2_budget = max(220, int(skill_tokens * 0.40))

        chunks: list[str] = []
        l1_lines = [render_l1(skill) for skill in active_skills]
        l1_lines = _trim_lines_to_token_budget(l1_lines, l1_budget, estimate_tokens)
        if l1_lines:
            chunks.append("### Skill index (relevant to this task)")
            chunks.extend(l1_lines)
            chunks.append("")

        top_skills = match_for_task(
            workspace_task_text,
            all_skills,
            domains=detected_domains,
            status="active",
            limit=2,
        )
        if top_skills:
            cards = [render_l2(skill, max_tokens=180) for skill in top_skills]
            while cards and estimate_tokens("\n\n".join(cards)) > l2_budget:
                cards.pop()
            if cards:
                chunks.append("### Top skills for this task")
                chunks.append("\n\n".join(cards))
                chunks.append("")
        return chunks
    except Exception:
        log.debug("Prior knowledge: skill library render failed", exc_info=True)
        return []


def _collect_palace_entries(
    *,
    repo_root: Path,
    workspace_id: str,
    workspace_task_text: str,
    detected_domains: set[str],
    palace_tokens: int,
    estimate_tokens: Any,
) -> tuple[list[str], list[str]]:
    try:
        from umbrella.memory.palace_backend import get_palace_backend
        from umbrella.memory.recall import summarized_palace_for_prompt
        from umbrella.memory.paths import palace_path_for

        palace = get_palace_backend(palace_path_for(repo_root, workspace_id))
        require_gmas = "multi_agent_gmas" in detected_domains
        recall_bundle = summarized_palace_for_prompt(
            palace=palace,
            query=workspace_task_text[:3000],
            workspace_id=workspace_id,
            token_budget=palace_tokens,
            require_gmas=require_gmas,
        )
        palace_entries = list(recall_bundle.entries)
        flagged_palace = list(recall_bundle.flagged_non_gmas)
    except Exception:
        log.debug("Prior knowledge: palace recent failed", exc_info=True)
        palace_entries = []
        flagged_palace = []

    return (
        _trim_lines_to_token_budget(palace_entries, palace_tokens, estimate_tokens),
        _trim_lines_to_token_budget(
            flagged_palace, max(200, palace_tokens // 2), estimate_tokens
        ),
    )


def _render_palace_chunks(
    palace_entries: list[str], flagged_palace: list[str]
) -> list[str]:
    chunks: list[str] = []
    if palace_entries:
        chunks.append("### Recent Umbrella memory")
        chunks.extend(palace_entries)
        chunks.append("")

    if flagged_palace:
        chunks.append(
            "### Previous attempts (review CRITICALLY — these used a non-GMAS stack)"
        )
        chunks.append(
            "These memories were recorded BEFORE the multi-agent skill was active. "
            "Do not blindly repeat the FastAPI/httpx recipe; the current task "
            "requires a GMAS-based implementation. Use them only to learn from "
            "concrete bugs, not as architectural templates."
        )
        chunks.extend(flagged_palace)
        chunks.append("")
    return chunks


def _collect_retrieval_hits(
    *,
    repo_root: Path,
    workspace_id: str,
    retr_tokens: int,
    estimate_tokens: Any,
) -> list[str]:
    try:
        from umbrella.integration.services import UmbrellaServices

        svc = UmbrellaServices(repo_root=repo_root)
        if not svc.retrieval:
            return []
        card = svc.retrieval.search(
            f"{workspace_id} workspace usage patterns",
            max_results=5,
            build_card=False,
        )
        retrieval_entries: list[str] = []
        for hit in getattr(card, "hits", [])[:5]:
            title = getattr(hit, "title", "") or ""
            body = (getattr(hit, "content", "") or "")[:450]
            if body.strip():
                retrieval_entries.append(f"- (retrieval) {title}: {body}")
        retrieval_entries = _trim_lines_to_token_budget(
            retrieval_entries,
            retr_tokens,
            estimate_tokens,
        )
        if not retrieval_entries:
            return []
        return ["### Retrieval hits", *retrieval_entries, ""]
    except Exception:
        log.debug("Prior knowledge: retrieval search failed", exc_info=True)
        return []


def _render_cold_start_discovery_banner() -> str:
    return (
        "### [EMPTY PRIOR KNOWLEDGE — EXTERNAL DISCOVERY REQUIRED]\n"
        "\n"
        "This workspace has **no Umbrella memory, no detected skills, and "
        "no retrieval hits**. You are starting cold. Before you call "
        "`propose_task_plan`, you MUST do at least one external "
        "discovery call so the plan is grounded in real artefacts, not "
        "imagined APIs:\n"
        "\n"
        '- `deep_search(intent="prior_art", query="<concrete topic>")` '
        "for design patterns, library choices, or fresh bug reports.\n"
        "- `github_project_search` + `github_extract_snippets` when you "
        "need to copy a known-good integration shape.\n"
        "- `mcp_discover` when the task involves an external service or "
        "tool the agent might not know about.\n"
        "- `web_fetch` for primary documentation or a specific URL.\n"
        "\n"
        "These calls are **cached and cheap** — running them once early "
        "is far cheaper than a full remediation cycle. The planner gate "
        "will refuse `propose_task_plan` until at least one external "
        "discovery call lands in this run.\n"
        "\n"
        "Persist anything useful via `record_idea(evidence_kind="
        '"observation_from_log", title=..., body=..., tags=[...])` or '
        "`save_umbrella_lesson` so the next run in this workspace recalls "
        "it from memory instead of repeating the lookup. If the task "
        "looks multi-agent in nature, also call `get_gmas_context` "
        "before the first `update_workspace_seed` write."
    )


def build_prior_knowledge_section(
    repo_root: Path,
    workspace_id: str,
    *,
    max_chars: int | None = None,
    token_budget: int | None = None,
) -> str:
    """Inject skill artifacts + MemPalace + retrieval hints into the prompt.

    Priority order (top → bottom):

    1. **Detected skills banner** + per-skill auto-fetched context
       (``gmas_active_context.md`` etc.) -- this is what Umbrella's skill
       layer already prepared on the agent's behalf.
    2. **Recent Umbrella memory** from MemPalace, with entries that look
       like non-GMAS attempts demoted into a ``review CRITICALLY``
       block when ``multi_agent_gmas`` is active.
    3. **Retrieval hits** for workspace usage patterns.

    Token budgets default from ``OUROBOROS_PRIOR_KNOWLEDGE_TOKENS`` and are
    split ~50% skill artifacts / 30% palace / 20% retrieval. If ``max_chars``
    is set (legacy tests), the final string is also capped to that length.
    """

    from umbrella.llm_budget import estimate_tokens, get_prior_knowledge_tokens

    tb = int(token_budget) if token_budget is not None else get_prior_knowledge_tokens()
    tb = max(500, tb)
    skill_tokens = int(tb * 0.50)
    palace_tokens = int(tb * 0.30)
    retr_tokens = int(tb * 0.20)
    # The skill banner should bootstrap the agent, not consume the whole
    # prompt window. Richer GMAS retrieval is still available via the
    # explicit `get_gmas_context` tool call.
    gmas_prefetch_tokens = min(skill_tokens, max(1200, tb // 5))

    drive_root = _workspace_drive_root_for(repo_root, workspace_id)
    detected_domains = _lazy_detect_domains(repo_root, workspace_id)
    chunks: list[str] = []
    workspace_task_text = _read_workspace_task_text(repo_root, workspace_id)

    if detected_domains:
        chunks.extend(
            _render_skill_banner(
                drive_root=drive_root,
                detected_domains=detected_domains,
                gmas_prefetch_tokens=gmas_prefetch_tokens,
            )
        )
        chunks.extend(
            _render_skill_library_cards(
                repo_root=repo_root,
                detected_domains=detected_domains,
                workspace_task_text=workspace_task_text,
                skill_tokens=skill_tokens,
                estimate_tokens=estimate_tokens,
            )
        )

    palace_entries, flagged_palace = _collect_palace_entries(
        repo_root=repo_root,
        workspace_id=workspace_id,
        workspace_task_text=workspace_task_text,
        detected_domains=detected_domains,
        palace_tokens=palace_tokens,
        estimate_tokens=estimate_tokens,
    )
    chunks.extend(_render_palace_chunks(palace_entries, flagged_palace))
    chunks.extend(
        _collect_retrieval_hits(
            repo_root=repo_root,
            workspace_id=workspace_id,
            retr_tokens=retr_tokens,
            estimate_tokens=estimate_tokens,
        )
    )

    text = "\n".join(chunks).strip()
    if not text:
        return _render_cold_start_discovery_banner()
    final_cap = max_chars if max_chars is not None else tb * 4
    if len(text) > final_cap:
        text = text[:final_cap].rstrip() + "\n\n[prior knowledge truncated]"
    return text


def read_workspace_task(workspace_path: Path) -> str:
    task_file = workspace_path / "TASK_MAIN.md"
    if task_file.exists():
        return task_file.read_text(encoding="utf-8")
    return f"Improve and validate the {workspace_path.name} workspace."


def _compact_environment_snapshot(snapshot: str, *, max_lines: int = 18) -> str:
    text = str(snapshot or "").strip()
    if not text:
        return ""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    keep = lines[:max_lines]
    keep.append("")
    keep.append(
        f"... ({len(lines) - max_lines} more lines omitted; inspect with workspace tools if needed)"
    )
    return "\n".join(keep)


def render_workspace_prompt(
    *,
    repo_root: Path,
    workspace_id: str,
    task_text: str,
    quality_threshold: float,
    include_environment_snapshot: bool = False,
    include_prior_knowledge: bool = False,
    retry_context: str = "",
) -> str:
    template = (PROMPT_DIR / "ouroboros_workspace_task.md").read_text(encoding="utf-8")

    environment_snapshot = ""
    if include_environment_snapshot:
        try:
            from umbrella.meta_harness.bootstrap import (
                render_environment_snapshot_section,
            )

            workspace_path = repo_root / "workspaces" / workspace_id
            environment_snapshot = render_environment_snapshot_section(
                repo_root,
                workspace_path if workspace_path.exists() else None,
            )
            environment_snapshot = _compact_environment_snapshot(environment_snapshot)
        except Exception:
            log.debug(
                "Environment snapshot generation failed (non-fatal)", exc_info=True
            )
            environment_snapshot = "_Environment snapshot unavailable._"

    prior_knowledge = ""
    if include_prior_knowledge:
        try:
            prior_knowledge = build_prior_knowledge_section(
                repo_root,
                workspace_id,
                token_budget=6000,
            )
        except Exception:
            log.debug("Prior knowledge generation failed (non-fatal)", exc_info=True)
            prior_knowledge = "_Prior knowledge unavailable._"
        if prior_knowledge.strip():
            prior_knowledge = f"## Prior Knowledge\n\n{prior_knowledge.strip()}"

    return template.format(
        repo_root=str(repo_root),
        workspace_id=workspace_id,
        task_text=task_text.strip(),
        quality_threshold=quality_threshold,
        environment_snapshot=environment_snapshot,
        prior_knowledge=prior_knowledge,
        retry_context=retry_context.strip(),
    )


def render_retry_prompt(
    *,
    attempt: int,
    max_attempts: int,
    previous_status: str,
    verification_report: dict[str, Any] | None,
    critic_review: dict[str, Any] | None = None,
    previous_final_message: str = "",
    limit_chars: int = 6000,
) -> str:
    """Render the ``retry_context`` block shown at the top of a retry prompt.

    The first attempt returns an empty string so the template renders clean.
    Subsequent attempts include the failing verification steps verbatim so
    the agent has a fully-detailed target to fix.
    """
    del critic_review
    if attempt <= 1:
        return ""

    lines: list[str] = []
    title = "## Previous Verification Failure"
    lines.append(f"{title} (attempt {attempt - 1}/{max_attempts})")
    lines.append(
        "The last run reported completion but the runtime verification gate failed. "
        "You must fix every failing step before declaring done again."
    )
    lines.append("")

    status_line = f"Previous run status: `{previous_status or 'unknown'}`"
    lines.append(status_line)
    lines.append("")
    lines.append("### Strict Retry Mode")
    lines.append(
        "This retry is fix-focused. Do only what is necessary to make failing "
        "verification/critic checks pass."
    )
    lines.append("- Do not start new features or side quests.")
    lines.append("- Change only files relevant to the failing checks.")
    lines.append(
        "- After each fix: run the exact failing command again and capture exit code."
    )
    lines.append("- If a check passes, move to the next failing check immediately.")
    lines.append("")

    summary = ""
    if isinstance(verification_report, dict):
        summary = str(verification_report.get("summary") or "").strip()
        if not summary:
            try:
                passed = bool(verification_report.get("passed"))
                pass_rate = verification_report.get("pass_rate")
                summary = f"Verification: {'PASS' if passed else 'FAIL'}" + (
                    f" (pass_rate={pass_rate})" if pass_rate is not None else ""
                )
            except Exception:  # noqa: BLE001
                summary = ""

    if summary:
        if len(summary) > 3500:
            summary = summary[:3500].rstrip() + "\n…[truncated]"
        lines.append("### Verification Report")
        lines.append(summary)
        lines.append("")
        lowered_summary = summary.lower()
        if (
            "verification spec is invalid" in lowered_summary
            or "invalid toml" in lowered_summary
        ):
            lines.append("### Mandatory TOML Repair Before Any Completion Claim")
            lines.append(
                "- The verification spec could not be parsed. Fix `workspace.toml` "
                "or `verification.toml` syntax before changing product code."
            )
            lines.append(
                "- For Windows paths inside TOML double-quoted strings, use forward "
                "slashes (`C:/Users/...`) or escape backslashes (`C:\\\\Users\\\\...`). "
                "Do not write raw `C:\\Users\\...` because `\\U` is a TOML escape."
            )
            lines.append(
                "- Use supported step declarations: `[[verification.steps]]` with "
                '`kind = "shell"` and `command = [...]`, `kind = "file_exists"` '
                'with `path = ...`, or `steps = ["python -m pytest tests -q"]`.'
            )
            lines.append(
                "- After repairing the spec, run `run_workspace_verify` before any "
                "completion claim."
            )
            lines.append("")
        if (
            "no verification steps declared or auto-detected" in lowered_summary
            or "skipped" in lowered_summary
        ):
            lines.append("### Mandatory Fix Before Any Completion Claim")
            lines.append(
                "- Verification was skipped/missing. You MUST create or fix "
                "`workspace.toml` with valid required `[[verification.steps]]` first."
            )
            lines.append(
                "- After that, run `run_workspace_verify` again and keep iterating "
                "until required steps pass."
            )
            lines.append("")

    if previous_final_message:
        excerpt = previous_final_message.strip()
        if len(excerpt) > 1500:
            excerpt = excerpt[:1500].rstrip()
        lines.append("### Your Previous Final Message")
        lines.append("```")
        lines.append(excerpt)
        lines.append("```")
        lines.append("")

    lines.append("### How to recover")
    lines.append(
        "1. Re-read `TASK_MAIN.md` and the failing steps above. "
        "Treat each failing step as an acceptance test you must make pass.\n"
        "2. Before stopping, run every failing command yourself via "
        "`run_workspace_command` and confirm the exit code / response is correct.\n"
        "3. Do NOT mark the task done until every required verification step is green.\n"
        "4. If you believe a verification step is wrong, record the evidence in memory "
        "and fix the step declaration in `workspace.toml`, do not silently ignore it.\n"
        "5. If implementation is partial (some modules/endpoints/tests missing), "
        "continue coding the missing pieces immediately, then rerun tests. "
        "A progress report is not a completion."
    )

    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = text[:limit_chars].rstrip()
    return text


def _recall_relevant_lessons_for_failures(
    *,
    workspace_memory_root: Path | None,
    failing: list[dict[str, Any]],
    max_lessons: int = 6,
    max_chars_per_lesson: int = 600,
) -> list[dict[str, str]]:
    """Pull recent lessons from ``ideas.jsonl`` that look relevant to the
    failing verification checks.

    The agent is supposed to call ``record_idea`` after each diagnosis
    so the next remediation cycle can reuse that knowledge — but in
    practice GLM/Claude rarely call ``recall_memory`` voluntarily. So
    we recall **for** them on the backend and inject the result into
    the prompt. The agent does not need to know we did this; it just
    sees a ``## Past Lessons`` section it can read.

    Ranking is intentionally simple (substring + freshness):

    - Build a set of lowercase keywords from each failing check's
      ``name`` and ``kind`` (e.g. ``source_policy:mock_scaffold_scan``,
      ``mock_scaffold``, ``source_policy``).
    - For each entry in ``ideas.jsonl`` (newest last), award one point
      per keyword found in ``content`` or ``tags``.
    - Keep the top ``max_lessons`` by score, breaking ties by recency.

    No vector store dependency: this works even when the palace
    backend is unavailable, and it reads the same file the agent's own
    ``record_idea`` writes to. Returns an ordered list of
    ``{"title", "snippet", "tags"}`` dicts.
    """
    if not workspace_memory_root or not failing:
        return []
    ideas_path = Path(workspace_memory_root) / "ideas.jsonl"
    if not ideas_path.is_file():
        return []
    keywords: set[str] = set()
    for item in failing:
        for key in ("name", "kind"):
            text = str(item.get(key) or "").lower().strip()
            if not text:
                continue
            for chunk in (
                text.replace(":", " ").replace("_", " ").replace("-", " ").split()
            ):
                if len(chunk) >= 3:
                    keywords.add(chunk)
    if not keywords:
        return []
    scored: list[tuple[int, int, dict[str, Any]]] = []
    try:
        with ideas_path.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                content = str(payload.get("content") or "")
                tags = payload.get("tags") or []
                tag_text = (
                    " ".join(str(t) for t in tags)
                    if isinstance(tags, list)
                    else str(tags)
                )
                haystack = (content + " " + tag_text).lower()
                hits = sum(1 for kw in keywords if kw in haystack)
                if hits == 0:
                    continue
                scored.append((hits, idx, payload))
    except OSError:
        return []
    scored.sort(key=lambda triple: (triple[0], triple[1]), reverse=True)
    out: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    for _hits, _idx, payload in scored:
        content = str(payload.get("content") or "").strip()
        title = (
            str(payload.get("title") or "").strip() or content.splitlines()[0][:120]
            if content
            else ""
        )
        title = title[:120]
        if title in seen_titles:
            continue
        seen_titles.add(title)
        snippet = content
        if len(snippet) > max_chars_per_lesson:
            snippet = snippet[:max_chars_per_lesson].rstrip() + "…"
        tags_value = payload.get("tags") or []
        if isinstance(tags_value, list):
            tag_str = ", ".join(str(t) for t in tags_value if t)
        else:
            tag_str = str(tags_value)
        out.append(
            {
                "title": title or "(untitled)",
                "snippet": snippet,
                "tags": tag_str,
            }
        )
        if len(out) >= max_lessons:
            break
    return out


def render_verification_remediation_prompt(
    *,
    original_task: str,
    verification_report: dict[str, Any],
    attempt: int,
    max_attempts: int,
    previous_final_message: str = "",
    failure_context_path: str = "",
    limit_chars: int = 12000,
    recalled_lessons: list[dict[str, str]] | None = None,
) -> str:
    """Prompt a continuation focused only on fixing verification failures.

    ``recalled_lessons`` (optional) is the list of past lessons relevant
    to the failing checks, prefetched on the backend by
    :func:`_recall_relevant_lessons_for_failures`. Injected verbatim
    under a ``## Past Lessons`` section so the agent can leverage prior
    fixes without remembering to call ``recall_memory`` itself.
    """

    summary = str(verification_report.get("summary") or "").strip()
    results = (
        verification_report.get("results")
        if isinstance(verification_report, dict)
        else []
    )
    failing: list[dict[str, Any]] = []
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict) or item.get("optional"):
                continue
            if str(item.get("status") or "").lower() != "passed":
                failing.append(item)

    lines: list[str] = [
        "# Verification Remediation Continuation (SAME RUN)",
        "",
        f"Verification cycle `{attempt}/{max_attempts}` of the SAME run.",
        "This is NOT a new task and NOT a restart. The run id and task id",
        "are unchanged — you are continuing exactly where you left off.",
        "",
        "The harness has already routed this attempt into a focused",
        "remediation phase: there is a single synthetic subtask covering",
        "exactly 'fix the failing checks below'. You will get a normal",
        "`[SUBTASK 1/1] FOCUS` block from the loop with the remediation",
        "tool schema active (full write/verify access + delete_workspace_file",
        "+ revise_remaining_plan + mark_subtask_complete/mark_remediation_complete).",
        "Do NOT call `propose_task_plan` — the plan already exists.",
        "",
        "## How to continue (mandatory order)",
        "1. **Diagnose the root cause first**. Read the failing checks below",
        "   and the structured failure context (path is given).",
        "2. **Say it out loud** — write a brief diagnosis (one or two lines per",
        "   failing check) explaining WHY it fails.",
        "3. **Persist the diagnosis to memory** with `record_idea(",
        '   evidence_kind="observation_from_log", title=...)`',
        "   while you are still diagnosing, but only when there is an actual",
        "   failing check or cleanup target. Once a fix is verified, upgrade",
        "   it to a verified lesson via `save_umbrella_lesson(verify_run_id=...,",
        "   failed_step_count=0)`. Do not create memory entries for no-op",
        "   remediation attempts with empty failures.",
        "4. **Fix only the failing checks**. Change only files needed to make",
        "   them pass. No broad refactors. No unrelated features.",
        "   If the failure points at workspace pollution (ad-hoc scripts,",
        "   placeholder docs, extracted raw artifacts) use",
        "   `delete_workspace_file(workspace_id=..., file_path=..., reason=...)`",
        "   — that is the sanctioned cleanup path; shell `rm`/`del` and",
        '   `python -c "...unlink()..."` are blocked on purpose.',
        "5. **Self-verify each fix** by running the exact failing command",
        "   (`run_workspace_command argv=[...]`) AND `run_workspace_verify`.",
        "   Do not declare a fix complete without seeing both pass.",
        "6. **Then submit**. Send the final message (or `mark_subtask_complete`",
        "   / `mark_remediation_complete`) ONLY when every previously failing",
        "   required check passes locally. The harness will then re-run",
        "   external verification.",
        "",
        "## Hard rules",
        "- Do not restart the implementation from scratch.",
        "- Do not call `propose_task_plan`; the synthetic remediation",
        "  subtask is already in place. Use `revise_remaining_plan` only",
        "  if you genuinely need to split remediation into multiple steps.",
        "- Do not add unrelated features or broad refactors.",
        "- Do not send a final completion message unless your own",
        "  acceptance commands pass for every previously failing check.",
        "- If verification keeps failing because of an external dependency,",
        "  error message, or library you do not recognise, you may call",
        '  `deep_search(intent="verification_repair", query=...)`,',
        "  `github_project_search`, `mcp_discover`, or `web_fetch` once per",
        "  attempt to fetch authoritative information; do not search for",
        "  trivia.",
        "",
    ]
    if failure_context_path:
        lines.extend(
            [
                "## Failure Context Artifact",
                f"The structured verification failure block was persisted at `{failure_context_path}`.",
                "",
            ]
        )
    if summary:
        lines.extend(["## Verification Summary", summary[:5000], ""])
    if failing:
        lines.append("## Failing Required Checks")
        for item in failing[:12]:
            name = str(item.get("name") or "unknown")
            kind = str(item.get("kind") or "")
            status = str(item.get("status") or "")
            lines.append(f"### `{name}` ({kind}) -> {status}")
            for key in ("summary", "error", "stdout_tail", "stderr_tail"):
                value = str(item.get(key) or "").strip()
                if not value:
                    continue
                if len(value) > 1800:
                    value = value[:1800].rstrip() + "\n...[truncated]"
                lines.append(f"**{key}:**")
                lines.append("```")
                lines.append(value)
                lines.append("```")
            lines.append("")
    else:
        lines.extend(
            [
                "## Failing Required Checks",
                "None listed in the verification payload. If this remediation was started,",
                "the blocker is probably in the structured failure context artifact,",
                "especially final_sweep hygiene cleanup targets. Read that file before",
                "writing or recording memory.",
                "",
            ]
        )
    # Auto-recall: inject the most relevant prior lessons so the agent
    # gets institutional knowledge for free, without having to remember
    # to call ``recall_memory``. Empirically GLM/Claude almost never
    # initiate recall during remediation; backend injection closes the
    # gap and turns the lesson log into an actually useful artifact
    # rather than write-only telemetry.
    if recalled_lessons:
        lines.append("## Past Lessons (auto-recalled, relevant to the failing checks)")
        lines.append(
            "Backend pre-loaded these from `ideas.jsonl` by matching the failing "
            "check names. Read first; if a past fix applies, reuse it instead of "
            "re-discovering the diagnosis."
        )
        lines.append("")
        for entry in recalled_lessons:
            title = entry.get("title") or "(untitled)"
            tags = entry.get("tags") or ""
            snippet = entry.get("snippet") or ""
            lines.append(f"### {title}")
            if tags:
                lines.append(f"- tags: `{tags}`")
            if snippet:
                lines.append("```")
                lines.append(snippet)
                lines.append("```")
            lines.append("")
    if previous_final_message:
        excerpt = previous_final_message.strip()
        if len(excerpt) > 1500:
            excerpt = excerpt[:1500].rstrip() + "\n...[truncated]"
        lines.extend(["## Previous Final Message", "```", excerpt, "```", ""])
    task_excerpt = original_task.strip()
    if len(task_excerpt) > 3000:
        task_excerpt = task_excerpt[:3000].rstrip() + "\n...[truncated]"
    lines.extend(["## Original Task Reference", task_excerpt, ""])

    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = text[:limit_chars].rstrip()
    return text


def render_self_review_prompt(
    *,
    original_task: str,
    verification_report: dict[str, Any],
    attempt: int,
    max_attempts: int,
    limit_chars: int = 14000,
) -> str:
    """Self-critique prompt fired AFTER verification passes.

    Goal (the operator asked for this directly): the agent must look
    at the actual end-to-end run output, decide whether it is
    satisfied, and either accept the result or queue concrete
    improvements — *in the same run*, no UI re-flow.

    Contract with the LLM, by design dead-simple to parse:

    * Reply starts with ``LGTM`` (case-insensitive) → backend accepts
      the run as ``verified`` and produces the human Russian summary.
    * Reply starts with ``NEEDS_FIX`` → backend treats the rest of
      the message as a list of improvements and runs another
      remediation cycle (same task_id, archived plan, fresh planner
      pass with full tool access).

    This is NOT a place for cosmetic polish. The agent should use it
    to catch real defects only visible from the run output: empty
    arrays where data should be, error tracebacks logged but ignored,
    a CLI that exits 0 but printed nothing, an LLM call that fell
    back to a stub because the API key was missing, etc.
    """
    results = (
        verification_report.get("results")
        if isinstance(verification_report, dict)
        else []
    )
    behavioural: list[dict[str, Any]] = []
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").lower()
            if kind in {
                "shell",
                "http_boot",
                "behavioral_http",
                "input_sensitivity",
                "pptx_diff",
            }:
                behavioural.append(item)

    lines: list[str] = [
        "# Self-Review of the Real Run (SAME RUN)",
        "",
        f"Self-review cycle `{attempt}/{max_attempts}` — verification has",
        "already PASSED, but the operator wants you to look at the actual",
        "end-to-end run output before declaring victory.",
        "",
        "## Your job (read carefully)",
        "1. Read the stdout/stderr blocks below. They are the REAL output",
        "   of the verified workspace, not a test mock.",
        "2. Decide whether the run actually does what the original task",
        "   asked for. Look for: empty results, fallback-to-stub paths,",
        "   silent exceptions, missing fields, hard-coded sample data,",
        "   API calls that returned errors but the program ignored them.",
        "3. Reply in EXACTLY ONE of these two shapes:",
        "",
        "   - `LGTM <one short sentence>` — accept the run as-is.",
        "     The backend will mark it ``verified`` and produce the final",
        "     summary. No further work happens.",
        "",
        "   - `NEEDS_FIX` followed by a numbered list of concrete fixes.",
        "     Example:",
        "     ```",
        "     NEEDS_FIX",
        '     1. CLI prints "Loaded 0 articles" — the parser is broken,',
        "        check selectors in `news/parser.py:42`.",
        "     2. The OpenAI call fell back to deterministic stub because",
        "        the API key env var is named OPENAI_KEY, not OPENAI_API_KEY.",
        "     ```",
        "     The backend will then start another remediation cycle in",
        "     the SAME run to apply your fixes.",
        "",
        "## Hard rules",
        "- This is NOT a code review. Cosmetic / style notes are noise.",
        "- Only flag issues you can SEE in the run output below or in the",
        "  workspace files. No speculation.",
        "- Treat cleanup as a product decision: classify suspicious files",
        "  as either required source/test/docs/config or removable artifacts.",
        "  If removal is needed, say `NEEDS_FIX` and name the exact files so",
        "  remediation can use `delete_workspace_file`; do not assume the",
        "  final sweep will delete them automatically.",
        "- If you are 50/50 → reply `LGTM` and let the operator iterate.",
        "  Spurious NEEDS_FIX wastes a remediation cycle and can make",
        "  the project worse.",
        "- Do not propose a refactor. Propose the smallest fix that",
        "  removes a real defect from the run output.",
        "",
        "## Reply format (STRICTLY ENFORCED)",
        "- Reply ONLY with plain text. **Do not call any tools.** This",
        "  phase has no tool schemas — any function call attempt is a",
        "  protocol violation and will be rejected.",
        "- Do not emit `<tool_call>`, `<arg_key>`, JSON wrappers, code",
        "  fences around the verdict, or any other markup. The very FIRST",
        "  non-whitespace token of your reply MUST be either `LGTM` or",
        "  `NEEDS_FIX`.",
        "- Examples of valid replies (no extra prose before the verdict):",
        "    `LGTM verification passed and the run does what the task asked.`",
        "    `NEEDS_FIX`",
        "    `1. ...`",
        "    `2. ...`",
        "",
    ]

    if behavioural:
        lines.append("## Real Run Output (behavioural verification steps)")
        for item in behavioural[:6]:
            name = str(item.get("name") or "step")
            kind = str(item.get("kind") or "")
            status = str(item.get("status") or "")
            lines.append(f"### `{name}` ({kind}) -> {status}")
            for key in ("summary", "stdout_tail", "stderr_tail"):
                value = str(item.get(key) or "").strip()
                if not value:
                    continue
                if len(value) > 1500:
                    value = value[:1500].rstrip() + "\n…[truncated]"
                lines.append(f"**{key}:**")
                lines.append("```")
                lines.append(value)
                lines.append("```")
            lines.append("")
    else:
        lines.append("## Real Run Output")
        lines.append(
            "(No behavioural step ran — verification only "
            "covered static checks like import_check / "
            "file_exists. Reply `LGTM` unless you can prove a "
            "defect by reading the workspace files directly.)"
        )
        lines.append("")

    task_excerpt = original_task.strip()
    if len(task_excerpt) > 2500:
        task_excerpt = task_excerpt[:2500].rstrip() + "\n…[truncated]"
    lines.extend(["## Original Task Reference", task_excerpt, ""])

    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = text[:limit_chars].rstrip()
    return text


_SELF_REVIEW_LGTM_RE = re.compile(r"^\s*LGTM\b", re.IGNORECASE | re.MULTILINE)
_SELF_REVIEW_NEEDS_FIX_RE = re.compile(r"^\s*NEEDS_FIX\b", re.IGNORECASE | re.MULTILINE)


def parse_self_review_response(text: str) -> tuple[str, str]:
    """Parse the agent's self-review reply into a verdict + body.

    Returns ``("lgtm", "")`` when the agent accepted the run.
    Returns ``("needs_fix", body)`` when the agent wants another
    remediation cycle. ``body`` is the everything-after-``NEEDS_FIX``
    fixlist, suitable for embedding into the next remediation prompt.

    Defaults to ``("needs_fix", body)`` for ambiguous / empty replies.
    Self-review is the final autonomous guard after green verification:
    if the model refuses the required LGTM/NEEDS_FIX contract, the run
    must fail closed into same-run remediation instead of claiming
    ``verified``.
    """
    if not text:
        return (
            "needs_fix",
            "Self-review returned an empty response instead of LGTM or NEEDS_FIX.",
        )
    raw = text.strip()
    if _SELF_REVIEW_NEEDS_FIX_RE.search(raw):
        match = _SELF_REVIEW_NEEDS_FIX_RE.search(raw)
        body = raw[match.end() :].strip() if match else ""
        return ("needs_fix", body)
    if _SELF_REVIEW_LGTM_RE.search(raw):
        return ("lgtm", "")
    return (
        "needs_fix",
        "Self-review did not start with LGTM or NEEDS_FIX. Treat this as "
        "a failed review contract and rerun review/remediation.\n\n"
        f"Original self-review response:\n{raw}",
    )


def polymarket_e2e_task() -> str:
    template = (PROMPT_DIR / "polymarket_e2e_task.md").read_text(encoding="utf-8")
    return template.strip()
