"""Shared research-source provenance contract.

This module is intentionally small and policy-shaped: prompts, schemas,
memory writes, and research-summary repair hints should render this same
source contract instead of carrying parallel text/regex decisions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


GMAS_SOURCE_TOOLS = {"get_gmas_context", "search_gmas_knowledge"}
RESULT_BEARING_SOURCE_TOOLS = {
    "deep_search",
    "github_project_search",
    "mcp_discover",
    "web_search",
}
EXACT_SOURCE_TOOL_IDS = {
    "apply_workspace_patch",
    "deep_search",
    "env_check",
    "get_gmas_context",
    "github_project_search",
    "mcp_discover",
    "read_file",
    "read_workspace_charter",
    "run_real_e2e",
    "run_unit_tests",
    "run_workspace_command",
    "run_workspace_verify",
    "search_gmas_knowledge",
    "web_search",
}
GMAS_VERIFIED_FINDING_MIN_CONFIDENCE = 0.25
SCARCE_COVERAGE_STATUSES = {
    "source_scarce",
    "low_evidence",
    "scarce_evidence",
    "limited_evidence",
}
_GITHUB_URL_RE = re.compile(
    r"(?i)github\.com[/:](?P<owner>[a-z0-9_.-]+)/(?P<repo>[a-z0-9_.-]+)"
)
_RAW_URL_RE = re.compile(r"https?://[^\s\"'<>),\]]+")
_RAW_JSON_LABEL_RE = re.compile(
    r'(?is)"(?:title|name)"\s*:\s*"(?P<label>[^"]{4,180})"'
)
_RAW_ANSWER_ITEM_RE = re.compile(r"(?m)^\s*\[\d+\]\s+(?P<label>[^\r\n]{4,180})")

SOURCE_ID_DESCRIPTION = (
    "For counted research_finding memory, cite current evidence: a concrete "
    "github:owner/repo returned by github_project_search, a tool-qualified id "
    "such as github_project_search:<exact-query>, mcp_discover:<exact-query>, "
    "web_search:<exact-query>, or deep_search:<intent-or-query> only when that "
    "result has non-empty results, or get_gmas_context/search_gmas_knowledge:"
    "<query> after non-fallback, sufficiently confident GMAS discovery. Do "
    "not use bare result-bearing tool ids, palace_search recall, palace_add/run "
    "ids, TASK_MAIN.md, or preflight-only calls as counted-finding provenance."
)

PROMPT_RULES = (
    "Counted research_finding source_id values must come from current "
    "research-phase evidence. Result-bearing discovery tools need a concrete "
    "namespace or tool-qualified handle with non-empty results. GMAS handles "
    "must be non-fallback and sufficiently confident; if result_preview is "
    "truncated, raw fallback/confidence markers still count. Empty discovery, "
    "palace_search recall, preflight-only calls, summaries, and progress notes "
    "are observations."
)

_SUMMARY_EXPLICIT_SOURCE_PREFIXES = (
    "deep_search:",
    "get_gmas_context:",
    "github:",
    "github_project_search:",
    "gmas:",
    "mcp:",
    "mcp_discover:",
    "search_gmas_knowledge:",
    "web_search:",
)
_SUMMARY_SOURCE_LABEL_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?sources?(?:\*\*)?\s*:\s*(?P<value>.+?)\s*$"
)
_SUMMARY_SOURCE_CLAIM_SPECS: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    (
        "GitHub",
        re.compile(
            r"(?i)\bgithub\b.{0,80}\b(?:results?|repos?|repositories|projects?|"
            r"examples?|prior\s+art|inform(?:s|ed)?|ground(?:s|ed)?|"
            r"found|identified|returned|validated|discovery|executed|"
            r"see\s+finding)\b|"
            r"\b(?:results?|repos?|repositories|projects?|examples?|prior\s+art|"
            r"inform(?:s|ed)?|ground(?:s|ed)?|found|identified|returned|"
            r"validated|discovery|executed|see\s+finding)\b.{0,80}\bgithub\b"
        ),
        ("github:", "github_project_search:"),
    ),
    (
        "MCP",
        re.compile(
            r"(?i)\bmcp\b.{0,80}\b(?:results?|servers?|discovery|available|"
            r"returned|found|identified|validated|inform(?:s|ed)?)\b|"
            r"\b(?:results?|servers?|discovery|available|returned|found|"
            r"identified|validated|inform(?:s|ed)?)\b.{0,80}\bmcp\b"
        ),
        ("mcp:", "mcp_discover:"),
    ),
    (
        "web search",
        re.compile(
            r"(?i)\b(?:web_search|web\s+search|internet\s+search|external\s+web)\b"
            r".{0,80}\b(?:results?|returned|found|identified|validated|"
            r"inform(?:s|ed)?|ground(?:s|ed)?)\b"
        ),
        ("web_search:", "deep_search:", "http://", "https://"),
    ),
)
_SUMMARY_SOURCE_CLAIM_NEGATIVE_RE = re.compile(
    r"(?i)\b(?:no|none|empty|zero|0|without|unavailable|failed|failure|"
    r"did\s+not|didn't|not\s+available|not\s+found|no_results|"
    r"provider_unavailable)\b"
)


@dataclass(frozen=True)
class ResearchProvenanceIssue:
    code: str
    message: str


def result_entries(payload: dict[str, Any]) -> list[Any]:
    for key in ("results", "sources"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def tool_result_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("result_preview") or row.get("result") or {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def tool_result_text(row: dict[str, Any]) -> str:
    raw = row.get("result_preview") or row.get("result") or ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except Exception:
        return str(raw or "")


def mapping_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def tool_row_provenance_payloads(row: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    args_payload = mapping_payload(row.get("args"))
    if args_payload:
        payloads.append(args_payload)
    result_payload = tool_result_payload(row)
    if result_payload:
        payloads.append(result_payload)
    return payloads


def _normalise_source_handle(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*(?:`+|\*\*)", "", text)
    text = re.sub(r"(?:`+|\*\*)\s*$", "", text)
    text = text.strip(" \t\r\n.,;")
    return re.sub(r"\s+", " ", text)


def explicit_summary_source_handles(notes: str) -> list[str]:
    """Return explicit `Source: tool:query` handles from summary notes.

    This intentionally parses only source labels, not arbitrary prose. The
    summary handoff may be freeform, but when it declares a machine-readable
    source handle, that handle must stay bound to the cited finding ledger.
    """

    handles: list[str] = []
    seen: set[str] = set()
    for match in _SUMMARY_SOURCE_LABEL_RE.finditer(str(notes or "")):
        value = match.group("value")
        for prefix in _SUMMARY_EXPLICIT_SOURCE_PREFIXES:
            prefix_re = re.escape(prefix)
            source_match = re.search(
                rf"(?i)(?P<source>{prefix_re}[^\n\r`]+)", value
            )
            if not source_match:
                continue
            source = _normalise_source_handle(source_match.group("source"))
            source_l = source.lower()
            if source and source_l not in seen:
                seen.add(source_l)
                handles.append(source)
            break
    return handles


def palace_add_source_paths_by_id(
    rows: list[dict[str, Any]], finding_ids: set[str]
) -> dict[str, str]:
    sources: dict[str, str] = {}
    for row in rows:
        if str(row.get("tool") or "") != "palace_add":
            continue
        preview = tool_result_payload(row)
        ids: list[str] = [
            str(preview.get(key) or "").strip()
            for key in ("id", "memory_id", "artifact_id")
            if str(preview.get(key) or "").strip()
        ]
        legacy = preview.get("legacy")
        if isinstance(legacy, dict):
            legacy_id = str(legacy.get("id") or "").strip()
            if legacy_id:
                ids.append(legacy_id)
        matching = [item for item in ids if item in finding_ids]
        if not matching:
            continue
        source = str(preview.get("source_path") or "").strip()
        if not source:
            args = mapping_payload(row.get("args") or {})
            source = str(args.get("source_id") or "").strip()
        if source:
            for item in matching:
                sources[item] = source
    return sources


def research_summary_source_claim_issue(
    rows: list[dict[str, Any]], *, finding_ids: set[str], notes: str
) -> str:
    source_paths = palace_add_source_paths_by_id(rows, finding_ids)
    lower_sources = [source.lower() for source in source_paths.values()]
    text = str(notes or "")

    explicit_handles = explicit_summary_source_handles(text)
    unbacked = [
        handle
        for handle in explicit_handles
        if handle.lower() not in lower_sources
    ]
    if unbacked:
        backed = ", ".join(f"`{source}`" for source in source_paths.values()) or "none"
        return (
            "ERROR: research summary notes cite source label(s) not backed by "
            "the cited accepted findings: "
            f"{', '.join(f'`{item}`' for item in unbacked[:6])}. "
            "Use the exact `source_path` returned by the cited palace_add "
            f"finding(s), or remove the source label. Backed sources: {backed}."
        )

    for label, pattern, prefixes in _SUMMARY_SOURCE_CLAIM_SPECS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 80)
            window = text[start:end]
            if _SUMMARY_SOURCE_CLAIM_NEGATIVE_RE.search(window):
                continue
            if any(
                source.startswith(tuple(prefix.lower() for prefix in prefixes))
                for source in lower_sources
            ):
                break
            return (
                "ERROR: research summary notes claim positive "
                f"{label} discovery evidence, but none of the cited accepted "
                "research findings is grounded in a usable current "
                f"{label} source. Save the claim as an observation, remove it, "
                "or first create a verified palace_add finding with matching "
                "source provenance."
            )
    return ""


def tool_row_has_usable_result(row: dict[str, Any]) -> bool:
    preview = str(row.get("result_preview") or row.get("result") or "")
    if "TOOL_ARG_ERROR" in preview or preview.strip().startswith("ERROR:"):
        return False
    payload = tool_result_payload(row)
    tool = str(row.get("tool") or "").strip().lower()
    if not payload:
        if tool == "github_project_search":
            return bool(github_source_handles_from_row(row))
        if tool in RESULT_BEARING_SOURCE_TOOLS:
            return bool(raw_result_anchor_strings(row))
        return bool(preview.strip())
    status = str(payload.get("status") or "").strip().lower()
    if status and status not in {"ok", "success"}:
        return False
    if tool in RESULT_BEARING_SOURCE_TOOLS:
        if not result_entries(payload):
            return bool(raw_result_anchor_strings(row))
    if int(payload.get("exit_code") or 0) != 0 and "exit_code" in payload:
        return False
    return "error" not in payload and "reason" not in payload


def _attempt_query(row: dict[str, Any], payload: dict[str, Any]) -> str:
    args = mapping_payload(row.get("args") or {})
    for value in (args.get("query"), payload.get("query"), args.get("intent"), payload.get("intent")):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _source_handle_for_attempt(row: dict[str, Any], payload: dict[str, Any]) -> str:
    tool = str(row.get("tool") or "").strip()
    if not tool:
        return ""
    qualifier = source_qualifier(row, payload)
    if qualifier:
        return f"{tool}:{qualifier}"
    return tool


def _attempt_status(row: dict[str, Any], payload: dict[str, Any]) -> str:
    raw = tool_result_text(row)
    if "TOOL_ARG_ERROR" in raw or "TOOL_PREFLIGHT_ERROR" in raw:
        return "tool_arg_error"
    if raw.strip().startswith("ERROR:"):
        return "tool_error"
    tool = str(row.get("tool") or "").strip().lower()
    if not payload and raw.strip():
        if tool == "github_project_search" and github_source_handles_from_row(row):
            return "usable"
        if tool in RESULT_BEARING_SOURCE_TOOLS and raw_result_anchor_strings(row):
            return "usable"
        return "unstructured_result"
    status = str(payload.get("status") or "").strip().lower()
    if "error" in payload:
        return status or "provider_error"
    if tool in GMAS_SOURCE_TOOLS:
        return "usable" if gmas_row_is_strong_verified_source(row) else "fallback_or_low_confidence"
    if status in {"provider_unavailable", "provider_error", "no_results", "budget_exhausted"}:
        return status
    if (
        tool in RESULT_BEARING_SOURCE_TOOLS
        and not result_entries(payload)
        and not raw_result_anchor_strings(row)
    ):
        return status or "empty_results"
    if tool_row_has_usable_result(row):
        return "usable"
    return status or "unusable"


def _normalise_github_repo(value: str) -> str:
    text = str(value or "").strip().strip("`'\" <>.,;)")
    if not text:
        return ""
    url_match = _GITHUB_URL_RE.search(text)
    if url_match:
        text = f"{url_match.group('owner')}/{url_match.group('repo')}"
    text = re.sub(r"\.git$", "", text.strip("/"), flags=re.I)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
        return ""
    return text


def github_source_handles_from_row(row: dict[str, Any]) -> list[str]:
    """Extract github:owner/repo handles from parsed or raw search output."""

    if str(row.get("tool") or "").strip().lower() != "github_project_search":
        return []
    handles: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        repo = _normalise_github_repo(value)
        key = repo.lower()
        if repo and key not in seen:
            seen.add(key)
            handles.append(f"github:{repo}")

    payload = tool_result_payload(row)
    for entry in result_entries(payload):
        if not isinstance(entry, dict):
            continue
        for key in ("full_name", "html_url", "url"):
            value = entry.get(key)
            if isinstance(value, str):
                add(value)

    raw = tool_result_text(row)
    for match in _GITHUB_URL_RE.finditer(raw):
        add(f"{match.group('owner')}/{match.group('repo')}")
    for match in re.finditer(r'(?i)"full_name"\s*:\s*"(?P<repo>[^"]+)"', raw):
        add(match.group("repo"))
    return handles


def raw_result_anchor_strings(row: dict[str, Any]) -> list[str]:
    """Extract concrete result anchors from raw/truncated discovery previews."""

    tool = str(row.get("tool") or "").strip().lower()
    if tool not in RESULT_BEARING_SOURCE_TOOLS:
        return []
    raw = tool_result_text(row)
    if not raw or raw.strip().startswith("ERROR:"):
        return []
    anchors: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        text = str(value or "").strip().strip("`'\" <>.,;)")
        if len(text) < 4:
            return
        key = _normalise_anchor_text(text)
        if not key or key in seen:
            return
        seen.add(key)
        anchors.append(text)

    for handle in github_source_handles_from_row(row):
        add(handle.split(":", 1)[1] if ":" in handle else handle)
    for match in _RAW_URL_RE.finditer(raw):
        url = match.group(0)
        add(url)
        github_match = _GITHUB_URL_RE.search(url)
        if github_match:
            add(f"{github_match.group('owner')}/{github_match.group('repo')}")
    for match in _RAW_JSON_LABEL_RE.finditer(raw):
        add(match.group("label"))
    for match in _RAW_ANSWER_ITEM_RE.finditer(raw):
        add(match.group("label"))
    return anchors


def research_source_attempts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for row in rows:
        tool = str(row.get("tool") or "").strip()
        if tool not in RESULT_BEARING_SOURCE_TOOLS and tool not in GMAS_SOURCE_TOOLS:
            continue
        payload = tool_result_payload(row)
        entries = result_entries(payload)
        status = _attempt_status(row, payload)
        usable = status == "usable"
        result_count = len(entries)
        if tool in RESULT_BEARING_SOURCE_TOOLS and not result_count:
            result_count = len(raw_result_anchor_strings(row))
        attempt = {
            "tool": tool,
            "query": _attempt_query(row, payload),
            "status": status,
            "usable": usable,
            "result_count": result_count,
            "source_id": _source_handle_for_attempt(row, payload),
        }
        provider = str(payload.get("provider") or "").strip()
        if provider:
            attempt["provider"] = provider
        reason = str(payload.get("reason") or payload.get("error") or "").strip()
        if reason:
            attempt["reason"] = reason[:500]
        attempts.append(attempt)
    return attempts


def usable_research_source_handles(rows: list[dict[str, Any]]) -> list[str]:
    handles: list[str] = []
    seen: set[str] = set()
    for row in rows:
        tool = str(row.get("tool") or "").strip()
        if tool not in RESULT_BEARING_SOURCE_TOOLS and tool not in GMAS_SOURCE_TOOLS:
            continue
        payload = tool_result_payload(row)
        if _attempt_status(row, payload) != "usable":
            continue
        row_handles: list[str] = []
        if tool == "github_project_search":
            row_handles.extend(github_source_handles_from_row(row))
        if not row_handles:
            row_handles.append(_source_handle_for_attempt(row, payload))
        for handle in row_handles:
            handle = str(handle or "").strip()
            key = handle.lower()
            if handle and key not in seen:
                seen.add(key)
                handles.append(handle)
    return handles


def research_source_coverage_report(
    rows: list[dict[str, Any]], *, accepted_count: int = 0, min_findings: int = 1
) -> dict[str, Any]:
    attempts = research_source_attempts(rows)
    channels = {
        "github": {"attempted": False, "usable": False},
        "internet": {"attempted": False, "usable": False},
        "mcp": {"attempted": False, "usable": False},
        "gmas": {"attempted": False, "usable": False},
    }
    for attempt in attempts:
        tool = str(attempt.get("tool") or "")
        channel = ""
        if tool == "github_project_search":
            channel = "github"
        elif tool in {"deep_search", "web_search"}:
            channel = "internet"
        elif tool == "mcp_discover":
            channel = "mcp"
        elif tool in GMAS_SOURCE_TOOLS:
            channel = "gmas"
        if not channel:
            continue
        channels[channel]["attempted"] = True
        channels[channel]["usable"] = bool(channels[channel]["usable"] or attempt.get("usable"))
    usable_handles = usable_research_source_handles(rows)
    return {
        "status": "sufficient" if accepted_count >= min_findings else "source_scarce",
        "accepted_finding_count": max(0, int(accepted_count or 0)),
        "required_finding_count": max(1, int(min_findings or 1)),
        "usable_source_count": len(usable_handles),
        "usable_source_handles": usable_handles,
        "attempt_count": len(attempts),
        "channels": channels,
        "attempts": attempts[-20:],
    }


def research_scarcity_handoff_issue(
    rows: list[dict[str, Any]],
    *,
    accepted_count: int,
    min_findings: int,
    coverage_status: str = "",
) -> str:
    if accepted_count >= min_findings:
        return ""
    status = str(coverage_status or "").strip().lower()
    if status not in SCARCE_COVERAGE_STATUSES:
        return (
            "If all current discovery channels are exhausted, do not invent or "
            "duplicate findings. Resubmit with `coverage_status=\"source_scarce\"` "
            "and explain the failed/empty/fallback source attempts in notes."
        )
    if accepted_count < 1:
        return (
            "ERROR: source-scarce research handoff still requires at least one "
            "accepted `palace_add(kind=\"research_finding\")` id. If there is no "
            "positive evidence at all, loop back or request operator review "
            "instead of submitting a planning handoff."
        )
    report = research_source_coverage_report(
        rows,
        accepted_count=accepted_count,
        min_findings=min_findings,
    )
    missing = [
        label
        for label in ("github", "internet", "mcp")
        if not bool((report.get("channels") or {}).get(label, {}).get("attempted"))
    ]
    if missing:
        return (
            "ERROR: source-scarce research handoff cannot be used before "
            f"attempting required discovery channel(s): {', '.join(missing)}. "
            "Run the available discovery tools with task-specific queries, then "
            "resubmit with exact accepted finding ids only."
        )
    attempts = research_source_attempts(rows)
    web_failed = [
        attempt
        for attempt in attempts
        if str(attempt.get("tool") or "") == "web_search"
        and str(attempt.get("status") or "").lower()
        in {"provider_error", "provider_unavailable", "tool_error"}
    ]
    deep_attempted = any(
        str(attempt.get("tool") or "") == "deep_search" for attempt in attempts
    )
    if web_failed and not deep_attempted:
        return (
            "ERROR: source-scarce research handoff cannot be used after a "
            "failed `web_search` provider/tool attempt without trying "
            '`deep_search(intent="planner_research")` once for the same '
            "research need. Generic internet access is provider-independent; "
            "do not claim scarcity from one web_search failure until the deep "
            "search channel is also attempted and recorded."
        )
    usable_count = int(report.get("usable_source_count") or 0)
    expected_from_sources = min(max(1, usable_count), max(1, int(min_findings or 1)))
    if accepted_count < expected_from_sources:
        return (
            "ERROR: source-scarce research handoff still has unharvested usable "
            f"source evidence: {accepted_count}/{expected_from_sources} accepted "
            "finding(s) for the currently usable discovery rows. Save concrete "
            "findings for usable source handles before claiming scarcity. "
            "Usable handles: "
            + ", ".join(f"`{item}`" for item in (report.get("usable_source_handles") or [])[:6])
        )
    return ""


def metadata_bool_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value is True
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def payload_has_fallback_metadata(value: Any) -> bool:
    if isinstance(value, dict):
        metadata = value.get("metadata")
        if isinstance(metadata, dict) and metadata_bool_true(metadata.get("fallback")):
            return True
        return any(payload_has_fallback_metadata(item) for item in value.values())
    if isinstance(value, list):
        return any(payload_has_fallback_metadata(item) for item in value)
    return False


def text_has_fallback_metadata(text: str) -> bool:
    raw = str(text or "")
    if not raw:
        return False
    return (
        re.search(
            r'(?is)"metadata"\s*:\s*\{[^{}]*"fallback"\s*:\s*(?:true|1|"true"|"yes")',
            raw,
        )
        is not None
    )


def payload_confidences(value: Any) -> list[float]:
    confidences: list[float] = []
    if isinstance(value, dict):
        raw = value.get("confidence")
        if raw not in (None, ""):
            try:
                confidences.append(float(raw))
            except (TypeError, ValueError):
                pass
        for item in value.values():
            confidences.extend(payload_confidences(item))
    elif isinstance(value, list):
        for item in value:
            confidences.extend(payload_confidences(item))
    return confidences


def text_confidences(text: str) -> list[float]:
    confidences: list[float] = []
    for match in re.finditer(
        r'(?i)"confidence"\s*:\s*(-?\d+(?:\.\d+)?)', str(text or "")
    ):
        try:
            confidences.append(float(match.group(1)))
        except (TypeError, ValueError):
            continue
    return confidences


def gmas_row_is_strong_verified_source(row: dict[str, Any]) -> bool:
    if not tool_row_has_usable_result(row):
        return False
    payload = tool_result_payload(row)
    result_text = tool_result_text(row)
    if payload_has_fallback_metadata(payload) or text_has_fallback_metadata(result_text):
        return False
    confidences = [*payload_confidences(payload), *text_confidences(result_text)]
    if confidences and max(confidences) < GMAS_VERIFIED_FINDING_MIN_CONFIDENCE:
        return False
    return True


def github_namespace_source_seen(rows: list[dict[str, Any]], target: str) -> bool:
    wanted = str(target or "").strip().strip("/").lower()
    if not wanted:
        return False
    for row in rows:
        if str(row.get("tool") or "") != "github_project_search":
            continue
        if not tool_row_has_usable_result(row):
            continue
        for handle in github_source_handles_from_row(row):
            if handle.lower() == f"github:{wanted}":
                return True
    return False


def tool_row_matches_qualifier(row: dict[str, Any], qualifier: str) -> bool:
    needle = str(qualifier or "").strip().lower()
    if not needle:
        return True
    haystack_parts: list[str] = []
    for payload in tool_row_provenance_payloads(row):
        for key in ("intent", "query", "url", "title", "source_id"):
            value = payload.get(key)
            if isinstance(value, str):
                haystack_parts.append(value)
        results = payload.get("results")
        if isinstance(results, list):
            for result in results[:20]:
                if not isinstance(result, dict):
                    continue
                for key in ("full_name", "html_url", "url", "title", "name"):
                    value = result.get(key)
                    if isinstance(value, str):
                        haystack_parts.append(value)
    return needle in "\n".join(haystack_parts).lower()


def _normalise_anchor_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"\.git\b", "", text)
    text = re.sub(r"[^a-z0-9/_@.+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" /")


def _result_entry_anchor_strings(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return []
    anchors: list[str] = []
    for key in (
        "full_name",
        "name",
        "title",
        "html_url",
        "url",
        "source",
        "id",
    ):
        value = entry.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        anchors.append(text)
        if key in {"html_url", "url"}:
            path = re.sub(r"^https?://(?:www\.)?[^/]+/", "", text).strip("/")
            if path:
                anchors.append(path)
            parts = [part for part in path.split("/") if part]
            if len(parts) >= 2:
                anchors.append("/".join(parts[:2]))
    return anchors


def tool_result_content_grounding_issue(
    rows: list[dict[str, Any]],
    *,
    source_id: str = "",
    content: str = "",
) -> str:
    """Ensure tool-qualified findings cite concrete rows from the source output."""

    source = str(source_id or "").strip()
    if ":" not in source:
        return ""
    tool_name, qualifier = source.split(":", 1)
    tool_l = str(tool_name or "").strip().lower()
    if tool_l not in RESULT_BEARING_SOURCE_TOOLS:
        return ""
    content_norm = _normalise_anchor_text(content)
    if not content_norm:
        return ""
    matching_rows = [
        row
        for row in rows
        if str(row.get("tool") or "").strip().lower() == tool_l
        and tool_row_has_usable_result(row)
        and tool_row_matches_qualifier(row, qualifier)
    ]
    if not matching_rows:
        return ""
    anchors: list[str] = []
    seen: set[str] = set()
    for row in matching_rows:
        payload = tool_result_payload(row)
        for entry in result_entries(payload):
            for anchor in _result_entry_anchor_strings(entry):
                norm = _normalise_anchor_text(anchor)
                if len(norm) < 4 or norm in seen:
                    continue
                seen.add(norm)
                anchors.append(anchor)
                if norm in content_norm:
                    return ""
        if tool_l == "github_project_search":
            for handle in github_source_handles_from_row(row):
                anchor = handle.split(":", 1)[1] if ":" in handle else handle
                norm = _normalise_anchor_text(anchor)
                if len(norm) < 4 or norm in seen:
                    continue
                seen.add(norm)
                anchors.append(anchor)
                if norm in content_norm:
                    return ""
        for anchor in raw_result_anchor_strings(row):
            norm = _normalise_anchor_text(anchor)
            if len(norm) < 4 or norm in seen:
                continue
            seen.add(norm)
            anchors.append(anchor)
            if norm in content_norm:
                return ""
    if not anchors:
        return ""
    examples = ", ".join(f"`{anchor}`" for anchor in anchors[:4])
    return (
        f"ERROR: palace_add research_finding source_id `{source}` is current, "
        "but the finding content does not mention any concrete result item "
        f"from that tool output. Ground the finding in one of: {examples}. "
        "If the note is a synthesis, scarcity note, or unsupported inference, "
        "save it as `kind=observation` instead of `research_finding`."
    )


def tool_qualified_source_seen(
    rows: list[dict[str, Any]], *, tool_name: str, qualifier: str
) -> bool:
    tool_l = str(tool_name or "").strip().lower()
    needle = str(qualifier or "").strip().lower()
    if not tool_l or tool_l not in EXACT_SOURCE_TOOL_IDS:
        return False
    for row in rows:
        if str(row.get("tool") or "").strip().lower() != tool_l:
            continue
        if tool_l in GMAS_SOURCE_TOOLS:
            if not gmas_row_is_strong_verified_source(row):
                continue
        elif not tool_row_has_usable_result(row):
            continue
        if not needle:
            return True
        if tool_row_matches_qualifier(row, needle):
            return True
    return False


def research_finding_source_provenance_issue(
    rows: list[dict[str, Any]], *, source_id: str = ""
) -> str:
    source = str(source_id or "").strip()
    source_l = source.lower()
    if not source:
        return (
            "ERROR: palace_add research_finding requires a source_id tied to "
            "current research-phase evidence. Use a concrete namespace source "
            "like `github:owner/repo`, a tool-qualified result source such as "
            "`github_project_search:<exact query>`, `mcp_discover:<exact "
            "query>`, `web_search:<exact query>`, or "
            "`deep_search:<intent-or-query>` when that logged result has "
            "non-empty results, or a non-fallback GMAS source such as "
            "`get_gmas_context:<query>` / `search_gmas_knowledge:<query>` / "
            "`gmas:topic`. Save summaries, empty-result discovery, progress "
            "notes, and self-referential memory as `kind=observation`."
        )
    if source_l in {"palace_add", "tool:palace_add", "self", "memory"} or source_l.startswith(
        "tool:palace_add"
    ):
        return (
            "ERROR: palace_add research_finding cannot use palace_add itself "
            "as provenance. Cite the current discovery tool/result that "
            "supports the finding, or save this as `kind=observation`."
        )
    if (
        source_l in {"palace_search", "tool:palace_search"}
        or source_l.startswith("palace_search:")
        or source_l.startswith("tool:palace_search:")
    ):
        return (
            f"ERROR: palace_add research_finding source_id `{source}` is memory "
            "recall, not current source provenance. Use palace_search results "
            "as leads only; verify the claim against a concrete current "
            "discovery source such as `github:owner/repo`, a usable "
            "web/deep/MCP result, or a non-fallback GMAS source before saving a "
            "counted `research_finding`. Otherwise save this as "
            "`kind=observation`."
        )
    if source_l in EXACT_SOURCE_TOOL_IDS:
        if source_l in RESULT_BEARING_SOURCE_TOOLS:
            return (
                f"ERROR: palace_add research_finding source_id `{source}` is "
                "too broad for result-bearing discovery. Cite a concrete "
                "namespace source such as `github:owner/repo` when available, "
                "or use a tool-qualified source such as "
                "`github_project_search:<exact query>`, "
                "`mcp_discover:<exact query>`, `web_search:<exact query>`, or "
                "`deep_search:<intent-or-query>` for a query-level finding. "
                "The cited tool result must contain non-empty results. Save "
                "empty-result or broad discovery bookkeeping as "
                "`kind=observation`."
            )
        if source_l in GMAS_SOURCE_TOOLS:
            if any(
                str(row.get("tool") or "").strip().lower() == source_l
                and gmas_row_is_strong_verified_source(row)
                for row in rows
            ):
                return ""
            if any(
                str(row.get("tool") or "").strip().lower() == source_l
                and tool_row_has_usable_result(row)
                for row in rows
            ):
                return (
                    f"ERROR: palace_add research_finding source_id `{source}` "
                    "is backed only by fallback or low-confidence GMAS "
                    "retrieval. Save it as `kind=observation`, or cite a "
                    "stronger current `search_gmas_knowledge:<query>` / "
                    "`get_gmas_context:<query>` result before promoting it as "
                    "a verified research finding."
                )
        if any(
            str(row.get("tool") or "").strip().lower() == source_l
            and tool_row_has_usable_result(row)
            for row in rows
        ):
            return ""
        return (
            f"ERROR: palace_add research_finding source_id `{source}` has no "
            "usable logged result in this research task. Run/cite the discovery "
            "tool first, or save this note as `kind=observation`."
        )
    if source_l.startswith("github:"):
        target = source.split(":", 1)[1]
        if github_namespace_source_seen(rows, target):
            return ""
        return (
            f"ERROR: palace_add research_finding source_id `{source}` does not "
            "match any repository returned by `github_project_search` in this "
            "research task. Use a returned `github:owner/repo` value, the exact "
            "`github_project_search` tool source, or save this as "
            "`kind=observation`."
        )
    if source_l.startswith("gmas:"):
        if any(
            str(row.get("tool") or "").strip().lower() in GMAS_SOURCE_TOOLS
            and gmas_row_is_strong_verified_source(row)
            for row in rows
        ):
            return ""
        if any(
            str(row.get("tool") or "").strip().lower() in GMAS_SOURCE_TOOLS
            and tool_row_has_usable_result(row)
            for row in rows
        ):
            return (
                f"ERROR: palace_add research_finding source_id `{source}` is "
                "backed only by fallback or low-confidence GMAS retrieval. "
                "Save it as `kind=observation`, or cite a stronger current "
                "`search_gmas_knowledge:<query>` / `get_gmas_context:<query>` "
                "result before promoting it as a verified research finding."
            )
        return (
            f"ERROR: palace_add research_finding source_id `{source}` requires "
            "a successful current `get_gmas_context` or `search_gmas_knowledge` "
            "call. Run/cite GMAS discovery first, or save this as "
            "`kind=observation`."
        )
    if ":" in source:
        tool_name, qualifier = source.split(":", 1)
        tool_l = str(tool_name or "").strip().lower()
        qualifier_l = str(qualifier or "").strip().lower()
        if tool_l in GMAS_SOURCE_TOOLS:
            matching_rows = [
                row
                for row in rows
                if str(row.get("tool") or "").strip().lower() == tool_l
                and tool_row_matches_qualifier(row, qualifier_l)
            ]
            if any(gmas_row_is_strong_verified_source(row) for row in matching_rows):
                return ""
            if any(tool_row_has_usable_result(row) for row in matching_rows):
                return (
                    f"ERROR: palace_add research_finding source_id `{source}` "
                    "is backed only by fallback or low-confidence GMAS "
                    "retrieval. Save it as `kind=observation`, or cite a "
                    "stronger current `search_gmas_knowledge:<query>` / "
                    "`get_gmas_context:<query>` result before promoting it as "
                    "a verified research finding."
                )
        if tool_qualified_source_seen(rows, tool_name=tool_name, qualifier=qualifier):
            return ""
    return (
        f"ERROR: palace_add research_finding source_id `{source}` is not a "
        "verifiable current discovery source. Use a concrete namespace source "
        "like `github:owner/repo`, a tool-qualified source "
        "`tool:<exact-query-or-intent>` backed by a usable current result, or "
        "a non-fallback GMAS source such as `gmas:topic`; keep local summaries "
        "and memory bookkeeping as `kind=observation`."
    )


def source_qualifier(row: dict[str, Any], payload: dict[str, Any]) -> str:
    args = mapping_payload(row.get("args") or {})
    candidates: list[Any] = []
    if isinstance(args, dict):
        candidates.extend(args.get(key) for key in ("intent", "query", "url", "title"))
    candidates.extend(payload.get(key) for key in ("intent", "query", "url", "title"))
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def next_finding_source_hint(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        tool = str(row.get("tool") or "").strip()
        raw = tool_result_text(row)
        if not tool or "TOOL_ARG_ERROR" in raw or raw.strip().startswith("ERROR:"):
            continue
        payload = tool_result_payload(row)
        status = str(payload.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure", "no_results", "provider_unavailable"}:
            continue
        source_id = ""
        if tool == "github_project_search":
            handles = github_source_handles_from_row(row)
            if not handles:
                continue
            source_id = handles[0]
        elif tool in GMAS_SOURCE_TOOLS:
            if not gmas_row_is_strong_verified_source(row):
                continue
            qualifier = source_qualifier(row, payload)
            if qualifier:
                source_id = f"{tool}:{qualifier}"
        elif tool in RESULT_BEARING_SOURCE_TOOLS:
            results = payload.get("results")
            sources = payload.get("sources")
            raw_anchors = raw_result_anchor_strings(row)
            if (
                (not isinstance(results, list) or not results)
                and (not isinstance(sources, list) or not sources)
                and not raw_anchors
            ):
                continue
            qualifier = source_qualifier(row, payload)
            if qualifier:
                source_id = f"{tool}:{qualifier}"
        if source_id:
            return (
                " Recent usable discovery source candidate: "
                f"`{source_id}`. Before retrying `submit_research_summary`, "
                "call `palace_add` with `kind=\"research_finding\"`, concrete "
                "content grounded in that tool result, and that `source_id`; "
                "then cite the primary id returned by palace_add exactly once."
            )
    return (
        " If no current source can support another finding, run an allowed "
        "discovery tool such as `search_gmas_knowledge`, `get_gmas_context`, "
        "`mcp_discover`, `deep_search`, or `github_project_search` before "
        "retrying `palace_add`."
    )


__all__ = [
    "EXACT_SOURCE_TOOL_IDS",
    "GMAS_SOURCE_TOOLS",
    "GMAS_VERIFIED_FINDING_MIN_CONFIDENCE",
    "PROMPT_RULES",
    "RESULT_BEARING_SOURCE_TOOLS",
    "ResearchProvenanceIssue",
    "SCARCE_COVERAGE_STATUSES",
    "SOURCE_ID_DESCRIPTION",
    "explicit_summary_source_handles",
    "github_namespace_source_seen",
    "github_source_handles_from_row",
    "gmas_row_is_strong_verified_source",
    "mapping_payload",
    "next_finding_source_hint",
    "palace_add_source_paths_by_id",
    "payload_confidences",
    "payload_has_fallback_metadata",
    "research_finding_source_provenance_issue",
    "research_scarcity_handoff_issue",
    "research_source_attempts",
    "research_source_coverage_report",
    "research_summary_source_claim_issue",
    "result_entries",
    "source_qualifier",
    "text_confidences",
    "text_has_fallback_metadata",
    "tool_qualified_source_seen",
    "tool_result_content_grounding_issue",
    "tool_result_payload",
    "tool_result_text",
    "tool_row_has_usable_result",
    "tool_row_matches_qualifier",
    "tool_row_provenance_payloads",
    "usable_research_source_handles",
]
