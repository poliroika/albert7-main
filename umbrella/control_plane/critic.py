"""Independent critic gate for completed Ouroboros workspace runs."""

import json
import logging
import os
from pathlib import Path
from typing import Any

from umbrella.verification.source_policy import scan_changed_files_for_mock_scaffold

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a strict independent code-delivery critic. Review the task, changed files, "
    "runtime verification report, subtask evidence, and scratchpad. Output strict JSON only, "
    "single object, with keys: "
    'verdict ("pass"|"fail"), rationale (string), risks (array of strings), '
    "missing_checks (array of strings), evidence_citations (array of objects, each "
    '{ "path": string, "claim": string }, citing repo-relative paths you relied on). '
    "If the payload is too thin to justify pass (e.g. empty changed_files, no behavioral "
    "detail, or final_message contradicts verification), verdict must be fail. "
    "Pass only when evidence proves the user task was actually completed. Fail shallow "
    "verification, mock/fallback implementations, missing behavioral checks, no-op changes, "
    "or summaries not backed by files/commands."
)


def critic_review(
    *,
    repo_root: Path,
    workspace_id: str,
    task_id: str,
    task_description: str,
    changed_files: list[str],
    verification_report: dict[str, Any] | None,
    final_message: str,
    drive_root: Path | None = None,
) -> dict[str, Any]:
    """Return a pass/fail critic verdict; fail closed on missing evidence."""
    if not verification_report or not verification_report.get("passed"):
        return {
            "verdict": "fail",
            "rationale": "Runtime verification did not pass.",
            "risks": ["verification_not_passed"],
            "missing_checks": [],
            "evidence_citations": [],
        }

    payload = {
        "workspace_id": workspace_id,
        "repo_root": str(repo_root),
        "task_id": task_id,
        "task_main": _read_task_main(repo_root, workspace_id),
        "task_description": task_description[:6000],
        "changed_files": changed_files[:80],
        "verification_report": verification_report,
        "final_message": final_message[:4000],
        "scratchpad": _read_scratchpad(drive_root)[:4000] if drive_root else "",
    }
    fallback = _heuristic_review(payload)
    if fallback["verdict"] == "fail":
        return fallback

    try:
        from umbrella.control_plane.code_analyzer import get_llm_client

        client = get_llm_client()
    except Exception:
        client = None
    critic_flag = os.environ.get("UMBRELLA_ENABLE_CRITIC_LLM", "1").strip().lower()
    if client is None or critic_flag in {"0", "false", "no", "off"}:
        return fallback

    try:
        response, _meta = client.chat(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ]
        )
        parsed = _parse_json(
            _extract_text(response if isinstance(response, dict) else {})
        )
        if isinstance(parsed, dict) and parsed.get("verdict") in {"pass", "fail"}:
            parsed.setdefault("rationale", "")
            parsed.setdefault("risks", [])
            parsed.setdefault("missing_checks", [])
            parsed.setdefault("evidence_citations", [])
            return parsed
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "critic LLM review failed, using heuristic verdict: %s", exc, exc_info=True
        )
    return fallback


def _heuristic_review(payload: dict[str, Any]) -> dict[str, Any]:
    report = payload.get("verification_report") or {}
    results = report.get("results") or []
    kinds = {str(r.get("kind") or "") for r in results if isinstance(r, dict)}
    behavioral = {"behavioral_http", "input_sensitivity_check", "pptx_diff"}
    if not kinds.intersection(behavioral):
        return {
            "verdict": "fail",
            "rationale": "Verification report has no behavioral step.",
            "risks": ["shallow_verification"],
            "missing_checks": ["behavioral_http_or_equivalent"],
            "evidence_citations": [],
        }
    if not payload.get("changed_files"):
        return {
            "verdict": "fail",
            "rationale": "No changed files were captured.",
            "risks": ["no_changes"],
            "missing_checks": [],
            "evidence_citations": [],
        }
    if not _has_diverse_behavioral_evidence(results):
        return {
            "verdict": "fail",
            "rationale": "Behavioral evidence does not show diverse inputs.",
            "risks": ["shallow_verification"],
            "missing_checks": ["diverse_inputs"],
            "evidence_citations": [],
        }
    mock_hits = _scan_changed_files_for_mock_scaffold(payload)
    if mock_hits:
        return {
            "verdict": "fail",
            "rationale": "Changed files contain mock or placeholder scaffold.",
            "risks": ["mock_scaffold"],
            "missing_checks": [],
            "mock_hits": mock_hits[:20],
            "evidence_citations": [],
        }
    return {
        "verdict": "pass",
        "rationale": "Runtime verification passed and includes behavioral evidence.",
        "risks": [],
        "missing_checks": [],
        "evidence_citations": [],
    }


def _has_diverse_behavioral_evidence(results: list[Any]) -> bool:
    for result in results:
        if not isinstance(result, dict):
            continue
        kind = str(result.get("kind") or "")
        if (
            kind == "input_sensitivity_check"
            and str(result.get("status") or "") == "passed"
        ):
            return True
        if kind == "behavioral_http" and str(result.get("status") or "") == "passed":
            count = int(result.get("request_payload_count") or 0)
            stdout = str(result.get("stdout_tail") or "")
            if count >= 2 or (
                "--- response A ---" in stdout and "--- response B ---" in stdout
            ):
                return True
    return False


def _scan_changed_files_for_mock_scaffold(payload: dict[str, Any]) -> list[str]:
    repo_root = Path(str(payload.get("repo_root") or ""))
    workspace_id = (
        str(payload.get("workspace_id") or "").strip().replace("\\", "/").strip("/")
    )
    if not repo_root or not workspace_id or ".." in Path(workspace_id).parts:
        return []
    workspace_root = repo_root / "workspaces" / workspace_id
    return scan_changed_files_for_mock_scaffold(
        repo_root=repo_root,
        workspace_path=workspace_root,
        changed_files=[str(p) for p in payload.get("changed_files") or []],
    )


def _read_task_main(repo_root: Path, workspace_id: str) -> str:
    try:
        return (repo_root / "workspaces" / workspace_id / "TASK_MAIN.md").read_text(
            encoding="utf-8",
            errors="replace",
        )[:8000]
    except OSError:
        return ""


def _read_scratchpad(drive_root: Path | None) -> str:
    if drive_root is None:
        return ""
    try:
        return (drive_root / "memory" / "scratchpad.md").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""


def _extract_text(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict)
        )
    return ""


def _parse_json(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None
    return None
