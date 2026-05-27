"""LLM-based reflection phase for verified complex iterations."""

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

from umbrella.memory import (
    get_workspace_store,
    record_competency_signal,
    record_workspace_lesson,
)
from umbrella.memory.models import SignalCategory
from umbrella.memory.palace_backend import get_palace_backend
from umbrella.memory.paths import palace_path_for

log = logging.getLogger(__name__)

_REFLECTION_SYSTEM = (
    "You are an autonomous reflection module for a coding workspace manager. "
    "Given run evidence, output strict JSON with schema: "
    '{"lesson":{"change_summary":"...","expected_effect":"...","observed_effect":"...",'
    '"conclusion":"...","repeat_tags":["..."],"avoid_tags":["..."]},'
    '"candidate_skill":{"name":"...","domains":["..."],"when_to_use":"...",'
    '"params":[{"name":"...","description":"..."}],"steps":["..."]}|null,'
    '"gap_signal":{"capability_area":"...","evidence_summary":"...","strength":-0.4}|null}. '
    "Always include lesson. Do not emit prose."
)


@dataclass(slots=True)
class ReflectionResult:
    status: str
    reason: str = ""
    lesson_id: str = ""
    skill_slug: str = ""
    signal_id: str = ""

    @classmethod
    def skipped(cls, reason: str) -> "ReflectionResult":
        return cls(status="skipped", reason=reason)


def run_reflection_phase(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    verification_report: dict[str, Any] | None,
    tool_call_count: int,
    final_message: str,
    changes_made: list[str] | None = None,
    critic_review: dict[str, Any] | None = None,
) -> ReflectionResult:
    """Reflect on a verified run; AVOID-lesson on anything not verified."""
    if not (verification_report and verification_report.get("passed")):
        _record_avoid_lesson(
            repo_root=repo_root,
            workspace_id=workspace_id,
            task_id=task_id,
            verification_report=verification_report,
            critic_review=critic_review,
            changes_made=changes_made or [],
        )
        return ReflectionResult.skipped("verification_not_passed")
    if int(tool_call_count) < 5:
        return ReflectionResult.skipped("not_complex_enough")

    payload = _reflect_with_llm(
        workspace_id=workspace_id,
        task_id=task_id,
        verification_report=verification_report,
        tool_call_count=tool_call_count,
        final_message=final_message,
        changes_made=changes_made or [],
    )

    store = get_workspace_store(repo_root, workspace_id)
    lesson_data = payload.get("lesson") if isinstance(payload, dict) else {}
    lesson = record_workspace_lesson(
        store=store,
        task_id=task_id,
        workspace_id=workspace_id,
        change_summary=str(lesson_data.get("change_summary") or "Iteration changes"),
        expected_effect=str(
            lesson_data.get("expected_effect") or "Improve task quality"
        ),
        observed_effect=str(
            lesson_data.get("observed_effect") or "Verified run completed"
        ),
        conclusion=str(
            lesson_data.get("conclusion") or "No concise conclusion provided"
        ),
        evidence_summary=f"tool_calls={tool_call_count}; changed={len(changes_made or [])}",
        repeat_tags=[str(tag) for tag in lesson_data.get("repeat_tags", [])][:8]
        if isinstance(lesson_data.get("repeat_tags"), list)
        else [],
        avoid_tags=[str(tag) for tag in lesson_data.get("avoid_tags", [])][:8]
        if isinstance(lesson_data.get("avoid_tags"), list)
        else [],
        priority=7,
        metadata={
            "source": "reflection_phase",
            "verified_at": time.time(),
            "critic_verdict": (critic_review or {}).get("verdict")
            if critic_review
            else None,
            "evidence_sha": _evidence_sha(verification_report, changes_made or []),
        },
    )
    try:
        palace = get_palace_backend(palace_path_for(repo_root, workspace_id))
        lesson_content = (
            f"{lesson.change_summary} | expected:{lesson.expected_effect}"
            f" | observed:{lesson.observed_effect}"
        )
        palace.add(
            workspace_id=workspace_id,
            event_type="reflection",
            room="lesson",
            title=f"verified reflection {task_id}",
            content=lesson_content,
            kind="lesson",
            tags=["lesson", "verified", "reflection"],
            task_id=task_id,
            metadata_extra={"verified": True, "phase": "verify"},
        )
    except Exception:
        log.debug("Reflection palace.lesson mirror failed", exc_info=True)

    skill_slug = ""
    candidate_skill = (
        payload.get("candidate_skill") if isinstance(payload, dict) else None
    )
    if isinstance(candidate_skill, dict):
        skill_slug = _write_candidate_skill(
            repo_root=repo_root, skill_payload=candidate_skill, run_task_id=task_id
        )

    signal_id = ""
    gap_signal = payload.get("gap_signal") if isinstance(payload, dict) else None
    if isinstance(gap_signal, dict):
        signal = record_competency_signal(
            store=store,
            category=SignalCategory.MISSING_CAPABILITY,
            capability_area=str(
                gap_signal.get("capability_area") or "unknown_capability"
            ),
            strength=float(gap_signal.get("strength") or -0.4),
            evidence_summary=str(
                gap_signal.get("evidence_summary")
                or "Reflection phase detected a capability gap"
            ),
            task_id=task_id,
            workspace_id=workspace_id,
            metadata={"source": "reflection_phase"},
        )
        signal_id = signal.id

    try:
        from umbrella.deep_agent_tools.memory import canonical_palace_add

        canonical_palace_add(
            repo_root,
            workspace_id=workspace_id,
            content=lesson.conclusion,
            title=f"reflection {task_id}",
            kind="insight",
            store="palace.idea",
            tags=["reflection", "lesson"],
            phase="reflection",
            source_path=f"reflection/{task_id}",
            extra={"room": "reflection", "skill_slug": skill_slug},
        )
    except Exception:
        log.debug("Reflection phase palace write failed", exc_info=True)

    return ReflectionResult(
        status="recorded",
        lesson_id=lesson.id,
        skill_slug=skill_slug,
        signal_id=signal_id,
    )


def _record_avoid_lesson(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    verification_report: dict[str, Any] | None,
    critic_review: dict[str, Any] | None,
    changes_made: list[str],
) -> None:
    try:
        store = get_workspace_store(repo_root, workspace_id)
        summary = (
            (verification_report or {}).get("summary")
            or (critic_review or {}).get("rationale")
            or "Run did not pass verification+critic gates"
        )
        record_workspace_lesson(
            store=store,
            task_id=task_id,
            workspace_id=workspace_id,
            change_summary="Avoid treating unverified Ouroboros run as success",
            expected_effect="Prevent false positive memory from contaminating future runs",
            observed_effect=str(summary)[:1000],
            conclusion="AVOID reusing this run as a positive lesson; require runtime verification and critic pass.",
            evidence_summary=f"changed={len(changes_made)}; critic={(critic_review or {}).get('verdict')}",
            repeat_tags=[],
            avoid_tags=["unverified_run", "false_positive_success"],
            priority=3,
            tags={"avoid", "unverified_lesson"},
            metadata={
                "source": "reflection_phase",
                "stale": True,
                "critic_verdict": (critic_review or {}).get("verdict"),
                "verification_passed": bool(
                    verification_report and verification_report.get("passed")
                ),
                "evidence_sha": _evidence_sha(verification_report, changes_made),
            },
        )
        try:
            palace = get_palace_backend(palace_path_for(repo_root, workspace_id))
            palace.add(
                workspace_id=workspace_id,
                event_type="reflection",
                room="lesson",
                title=f"avoid reflection {task_id}",
                content=f"AVOID unverified run | {str(summary)[:400]}",
                kind="lesson",
                tags=["lesson", "avoid", "unverified_lesson", "reflection"],
                task_id=task_id,
                metadata_extra={"verified": False, "phase": "verify"},
            )
        except Exception:
            log.debug("Reflection avoid-lesson palace mirror failed", exc_info=True)
    except Exception:
        log.debug("Failed to record AVOID lesson for unverified run", exc_info=True)


def _evidence_sha(
    verification_report: dict[str, Any] | None, changes_made: list[str]
) -> str:
    import hashlib

    blob = json.dumps(
        {"verification": verification_report or {}, "changes": changes_made},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def _reflect_with_llm(
    *,
    workspace_id: str,
    task_id: str,
    verification_report: dict[str, Any] | None,
    tool_call_count: int,
    final_message: str,
    changes_made: list[str],
) -> dict[str, Any]:
    fallback = {
        "lesson": {
            "change_summary": "Workspace iteration update",
            "expected_effect": "Higher reliability and quality",
            "observed_effect": "Verified status achieved",
            "conclusion": "Verified changes should be reused for similar tasks.",
            "repeat_tags": ["verified_workflow"],
            "avoid_tags": [],
        },
        "candidate_skill": None,
        "gap_signal": None,
    }
    try:
        from umbrella.control_plane.code_analyzer import get_llm_client
    except Exception:
        return fallback
    client = get_llm_client()
    if client is None:
        return fallback
    user_payload = {
        "workspace_id": workspace_id,
        "task_id": task_id,
        "tool_call_count": tool_call_count,
        "changes_made": changes_made[:30],
        "verification_summary": (verification_report or {}).get("summary", ""),
        "final_message": (final_message or "")[:2200],
    }
    try:
        response, _meta = client.chat(
            [
                {"role": "system", "content": _REFLECTION_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ]
        )
    except Exception:
        return fallback
    text = _extract_text(response if isinstance(response, dict) else {})
    parsed = _parse_first_json(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("lesson"), dict):
        return parsed
    return fallback


def _extract_text(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "\n".join(chunks)
    return ""


def _parse_first_json(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    for candidate in re.findall(r"\{.*\}", text, flags=re.DOTALL):
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _write_candidate_skill(
    repo_root: Path, skill_payload: dict[str, Any], run_task_id: str
) -> str:
    name = str(skill_payload.get("name") or "reflected-skill").strip()
    slug = _slugify(name)
    domains = (
        skill_payload.get("domains")
        if isinstance(skill_payload.get("domains"), list)
        else []
    )
    when_to_use = str(
        skill_payload.get("when_to_use") or "Use when this pattern appears."
    ).strip()
    params = (
        skill_payload.get("params")
        if isinstance(skill_payload.get("params"), list)
        else []
    )
    steps = (
        skill_payload.get("steps")
        if isinstance(skill_payload.get("steps"), list)
        else []
    )
    root = repo_root / "umbrella" / "skills" / "library" / slug
    root.mkdir(parents=True, exist_ok=True)
    skill_md = root / "SKILL.md"
    step_lines = [
        f"{idx + 1}. {str(step).strip()}"
        for idx, step in enumerate(steps[:12])
        if str(step).strip()
    ]
    if not step_lines:
        step_lines = ["1. Re-run the verified sequence from the reflection outcome."]
    text = (
        "---\n"
        f"name: {name}\n"
        "status: candidate\n"
        f"domains: {json.dumps([str(d).strip().lower() for d in domains if str(d).strip()])}\n"
        f"when_to_use: {json.dumps(when_to_use)}\n"
        f"params: {json.dumps(params, ensure_ascii=False)}\n"
        "created_by: reflection\n"
        f"source_run_id: {run_task_id}\n"
        "---\n\n"
        "## Steps\n" + "\n".join(step_lines) + "\n"
    )
    skill_md.write_text(text, encoding="utf-8")
    return slug


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return cleaned or "reflected-skill"
