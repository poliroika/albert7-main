import ast
import json
import logging
import pathlib
import re
import sys
import time
import uuid
from typing import Any, Callable, Iterator

from umbrella.phases.base import (
    PhasePlan,
    PhaseNode,
    PhaseResult,
    PlanEdit,
    SubtaskCard,
    SuccessTest,
)
from umbrella.phases.registry import get_registry
from umbrella.orchestrator.phase_plan import build_default_plan, save_plan, load_plan
from umbrella.orchestrator.watcher import WatcherPollLoop
from umbrella.orchestrator.worker import build_phase_task
from umbrella.memory.palace.facade import MemPalace
from umbrella.utils.result_envelope import ResultEnvelope, ErrorCode
from umbrella.utils.tool_logs import is_effective_write_tool_log_row
from umbrella.deep_agent_tools.evidence_graph import (
    phase_plan_pytest_target_availability_messages,
)
from umbrella.deep_agent_tools.domain_policy import unsupported_llm_env_alias_issues
from umbrella.deep_agent_tools.phase_contract_success import (
    _python_inline_docs_content_issue,
)

log = logging.getLogger(__name__)

_RESEARCH_SUMMARY_PLACEHOLDER_RE = re.compile(
    r"\b(pending completion|preparing palace writes|placeholder|todo|tbd|fix later)\b",
    re.IGNORECASE,
)

_SUCCESS_TEST_AUTOMATION_RE = re.compile(
    r"(?ix)"
    r"("
    r"\b(run_workspace_verify|run_unit_tests|harness_run|run_real_e2e|"
    r"run_workspace_command|shell)\b|"
    r"\b(http_boot|behavioral_http|playwright|selenium)\b|"
    r"\b(pytest|python|npm|pnpm|yarn|node|npx|uv|ruff|mypy|tsc|vite|curl|"
    r"powershell|pwsh|bash|sh|go|cargo|dotnet|mvn|gradle|java|make|cmake|"
    r"docker(?:\s+compose)?)\b|"
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+/[^\s]+|"
    r"https?://"
    r")"
)

_SUCCESS_TEST_VAGUE_RE = re.compile(
    r"(?ix)"
    r"("
    r"\b(document(?:ation|ed)?|memory\s+artifact|artifact\s+with|"
    r"notes?|analysis|clear\s+understanding|understand(?:ing)?|"
    r"evidence\s+recorded|summary|checklist)\b|"
    r"\b(all\s+tests\s+pass|tests\s+pass|no\s+errors?|works|complete|done)\b"
    r")"
)
_SUCCESS_TEST_WORKSPACE_CD_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*)cd\s+[\"']?(?:\.?[\\/])?workspaces[\\/][^;&|\"'\s]+"
)
_SUCCESS_TEST_FAILURE_MASK_RE = re.compile(
    r"(?ix)("
    r"\|\|\s*(?:true|:|exit\s+0|cmd\s*/c\s+exit\s+0)\b|"
    r"(?:^|[;&|]\s*)true\s*$"
    r")"
)

_GENERIC_SUCCESS_TEST_TOOLS = frozenset(
    {
        "run_workspace_verify",
        "run_unit_tests",
        "harness_run",
        "run_real_e2e",
        "http_boot",
        "behavioral_http",
    }
)

_GENERIC_SUCCESS_TOOL_WITH_ARGS_RE = re.compile(
    r"(?i)^\s*(?:run_workspace_verify|run_unit_tests|harness_run|run_real_e2e)"
    r"(?:\s+\S|[:(])"
)
_DESCRIPTIVE_SUCCESS_TEST_RE = re.compile(
    r"(?i)("
    r"\s+-\s+(?:must|should|verify|validate|checks?|assert|contains?)\b|"
    r"\bwith\s+(?:schema|http|behavioral)\s+verification\b|"
    r"\bmust\s+(?:instantiate|test|validate|run|start|assert|launch|create|verify)\b"
    r")"
)
_FILE_EXISTENCE_ONLY_SUCCESS_TEST_RE = re.compile(
    r"(?ix)("
    r"os\.path\.exists|"
    r"(?:pathlib\.)?Path\s*\([^)]*\)\.exists|"
    r"\.(?:exists|is_file|is_dir)\s*\(|"
    r"fs\.existsSync|"
    r"Test-Path"
    r")"
)
_BEHAVIORAL_SUCCESS_TEST_RE = re.compile(
    r"(?ix)\b("
    r"pytest|python\s+-m\s+pytest|npm\s+(?:run\s+)?(?:test|build)|"
    r"pnpm|yarn|npx\s+vitest|vitest\s+run|curl\s+-f|"
    r"run_workspace_verify|run_unit_tests|harness_run|http_boot|"
    r"behavioral_http|playwright"
    r")\b"
)
_DESCRIPTIVE_BROWSER_SUCCESS_TEST_RE = re.compile(
    r"(?ix)("
    r"\b(?:browser|page)\s+(?:opens?|loads?|shows?|displays?|navigates?)\b|"
    r"\b(?:open|load|visit|navigate)\s+(?:the\s+)?(?:browser|page|app|ui)\b|"
    r"\bhuman\s+player\b|"
    r"\bnetwork\s+inspector\b|"
    r"\bconsole\s+(?:has|shows|contains|reports)\s+(?:zero|no)\s+errors?\b|"
    r"\bwebsocket\s+messages?\s+(?:show|appear|visible)\b|"
    r"\bserver\s+starts?\s+cleanly\b"
    r")"
)
_CONCRETE_BROWSER_AUTOMATION_RE = re.compile(
    r"(?ix)\b("
    r"playwright|selenium|pytest|python\s+-m\s+pytest|npx\s+playwright|"
    r"npm\s+(?:run\s+)?(?:test|build)|pnpm|yarn|node|run_real_e2e|"
    r"run_workspace_verify|harness_run|http_boot|behavioral_http|curl\s+-f"
    r")\b"
)
_LLM_MOCK_SUCCESS_TEST_RE = re.compile(
    r"(?ix)("
    r"--mock\b|"
    r"--mock[-_]?llm\b|"
    r"\bmock[-_\s]?llm\b|"
    r"\bmocked\s+llm\b|"
    r"\bfake[-_\s]?llm\b|"
    r"\bdry[-_\s]?run[-_\s]?llm\b"
    r")"
)
_PLAN_LLM_TEST_DOUBLE_RE = re.compile(
    r"(?is)("
    r"\b(?:mock|fake|dry[-\s]?run|test\s+double)\b.{0,140}\b(?:llm|gmas|bot|agent|model)\b|"
    r"\b(?:llm|gmas|bot|agent|model)\b.{0,140}\b(?:mock|fake|dry[-\s]?run|test\s+double)\b"
    r")"
)
_LLM_WORK_ITEM_CONTEXT_RE = re.compile(r"(?i)\b(llm|gmas|agent|bot|model)\b")
_MOCKED_PROOF_WORK_ITEM_CONTEXT_RE = re.compile(
    r"(?i)\b("
    r"llm|gmas|agent|bot|model|"
    r"end[-\s]?to[-\s]?end|e2e|integration|acceptance|"
    r"live[-\s]?runtime|real[-\s]?runtime|real[-\s]?llm"
    r")\b"
)
_PLAN_GENERIC_FALLBACK_RE = re.compile(
    r"(?i)\b(?:fallback|fall[-\s]+back)\b(?:\s+(?:logic|handling|policy|"
    r"path|mode|strategy|rules?|behavior))?\b"
)
_PLAN_BAD_FALLBACK_REPLACEMENT_RE = re.compile(
    r"(?i)\b(heuristic|deterministic|static|hardcoded|mock|random|"
    r"rule[-\s]?based|default|valid\s+action|cached\s+decisions?|"
    r"cached\s+actions?|graceful\s+degradation)\b"
)
_PLAN_LLM_CACHED_DECISION_RE = re.compile(
    r"(?is)\b(?:decision|action|response)\s+caching\b|"
    r"\bcach(?:e|ed|ing)\s+(?:llm\s+|gmas\s+|ai\s+|bot\s+|agent\s+)?"
    r"(?:common\s+)?(?:decisions?|actions?|responses?|outputs?|reasoning)\b|"
    r"\breuse\s+cached\s+(?:decisions?|actions?|responses?|outputs?|reasoning)\b"
)
_PLAN_ENV_ALIAS_FALLBACK_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:fallback|fall[-\s]+back)\b.{0,160}"
    r"(?:llm_[a-z0-9_*]*|ouroboros_[a-z0-9_*]*|\baliases?\b|"
    r"\bruntime\s+aliases?\b|\benv(?:ironment)?(?:\s+vars?)?\b)"
    r"|"
    r"(?:llm_[a-z0-9_*]*|ouroboros_[a-z0-9_*]*|\baliases?\b|"
    r"\bruntime\s+aliases?\b|\benv(?:ironment)?(?:\s+vars?)?\b)"
    r".{0,160}\b(?:fallback|fall[-\s]+back)\b"
    r")"
)
_PLAN_SUCCESS_TEST_KEYS = frozenset(
    {
        "success_test",
        "success_check",
        "success_checks",
        "acceptance_command",
        "verification_command",
        "verification_commands",
        "verification",
        "test_strategy",
        "test",
    }
)
_SUCCESS_TEST_ALIAS_KEYS = (
    "success_check",
    "success_checks",
    "acceptance_command",
    "verification_command",
    "verification_commands",
    "verification",
    "test_strategy",
    "test",
)
_LLM_OUROBOROS_ENV_ALIASES = (
    "OUROBOROS_LLM_API_KEY",
    "OUROBOROS_LLM_BASE_URL",
    "OUROBOROS_MODEL",
)
_LLM_LEGACY_ENV_ALIASES = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
_LLM_ENV_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(alias) for alias in _LLM_OUROBOROS_ENV_ALIASES) + r")\b"
)
_LLM_ENV_CONTRACT_REQUIRED_RE = re.compile(
    r"(?is)\b("
    r"(?:real|live)\s+llm|llm[-\s]?powered|llm\s+(?:client|calls?|"
    r"integration|reasoning)|inherited\s+real\s+runtime\s+env"
    r")\b"
)
_LLM_ENV_OMISSION_REQUIRED_RE = re.compile(
    r"(?i)\b(provider|credentials?|api[-_\s]?keys?|base[-_\s]?url|"
    r"model\s+(?:selection|provider|name)|\.env|env(?:ironment)?\s+"
    r"(?:vars?|variables?))\b"
)
_LLM_LEGACY_ENV_RE = re.compile(
    r"\b(" + "|".join(re.escape(alias) for alias in _LLM_LEGACY_ENV_ALIASES) + r")\b"
)
_UNSUPPORTED_OUROBOROS_MODEL_ALIAS_RE = re.compile(r"\bOUROBOROS_LLM_MODEL\b")
_OPENAI_KEY_RE = re.compile(r"\bOPENAI_API_KEY\b")
_OPENAI_REQUIRED_RE = re.compile(
    r"(?is)\b(?:must|require[sd]?|need(?:s|ed)?|expects?|set|configure|"
    r"missing|validate|check)\b.{0,120}\bOPENAI_API_KEY\b|"
    r"\bOPENAI_API_KEY\b.{0,120}\b(?:must|required|needed|expected|set|"
    r"configured|missing|validat(?:e|ion)|check)\b"
)
_WEB_SEARCH_ONLY_CONTEXT_RE = re.compile(
    r"(?i)\b(web[_ -]?search|public web search|search provider)\b"
)
_FRONTEND_TEST_CWD_RE = re.compile(r"(?i)(?:^|[;&|]\s*)cd\s+frontend\b")
_JS_TEST_COMMAND_SEGMENT_RE = re.compile(
    r"(?i)\b(?:npm|pnpm|yarn|npx|vitest|jest)\b(?P<args>.*)$"
)
_FRONTEND_BUILD_COMMAND_RE = re.compile(
    r"(?is)(?:^|[;&|]\s*)cd\s+frontend\b[^;&|]*"
    r"(?:&&|;|\|\|)?[^;&|]*\b(?:npm|pnpm|yarn)\s+(?:run\s+)?build\b|"
    r"\b(?:vite|tsc)\s+(?:build\b|--build\b)"
)
_JS_TEST_FILE_TOKEN_RE = re.compile(
    r"(?:^|/)[^/\s]+\.(?:test|spec)\.(?:[cm]?[jt]sx?)$|"
    r"^[^/\s]+\.(?:test|spec)\.(?:[cm]?[jt]sx?)$",
    re.IGNORECASE,
)
_FRONTEND_SCRIPT_SOURCE_RE = re.compile(
    r"(?i)^frontend/src/.+\.(?:[cm]?[jt]sx?)$"
)
_FRONTEND_VITE_CONFIG_RE = re.compile(r"(?i)^frontend/vite\.config\.[cm]?[jt]s$")
_DIRECT_LOCALHOST_HTTP_RE = re.compile(
    r"(?i)\b(?:curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod)\b"
    r"(?=[^;&|\n]*\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b)"
)
_MANAGED_LOCALHOST_PROOF_RE = re.compile(
    r"(?i)\b(?:http_boot|behavioral_http|run_real_e2e|"
    r"playwright|selenium|python\s+-m\s+pytest|pytest|"
    r"npx\s+playwright|npm\s+(?:run\s+)?(?:e2e|test))\b"
)


def _missing_llm_runtime_aliases(text: str) -> list[str]:
    return [
        alias
        for alias in _LLM_LEGACY_ENV_ALIASES
        if not re.search(rf"\b{re.escape(alias)}\b", str(text or ""))
    ]


_LLM_PROVIDER_DEFAULT_PLAN_RE = re.compile(
    r"(?i)\b(?:openai/)?gpt-[a-z0-9_.:-]+\b|https://api\.openai\.com"
)
_EMPTY_TEST_SKELETON_RE = re.compile(
    r"(?is)("
    r"\b(?:empty|blank|import[-\s]?only|basic\s+imports?)\b.{0,120}"
    r"\b(?:test|tests|pytest|skeleton|shell|file|files)\b|"
    r"\b(?:test|tests|pytest|skeleton|shell|file|files)\b.{0,120}"
    r"\b(?:empty|blank|import[-\s]?only|basic\s+imports?)\b"
    r")"
)
_EMPTY_TEST_PROTECTIVE_RE = re.compile(
    r"(?is)\b(?:no|not|never|without|avoid|reject(?:s|ed)?|"
    r"forbid(?:s|den)?|disallow(?:s|ed)?|do\s+not|must\s+not|"
    r"should\s+not|cannot|can't)\b"
)
_EMPTY_TEST_BEHAVIORAL_PROOF_RE = re.compile(
    r"(?is)\b(?:executable\s+assertions?|assertions?|fixtures?|"
    r"can\s+fail|fail\s+for\s+real\s+behavior|real\s+behavior|"
    r"behavioral\s+(?:proof|evidence|test)|non[-\s]?empty)\b"
)
_EMPTY_TEST_DIRECT_PROTECTIVE_RE = re.compile(
    r"(?is)\b(?:no|not|never|without|avoid|reject(?:s|ed)?|"
    r"forbid(?:s|den)?|disallow(?:s|ed)?|do\s+not|must\s+not|"
    r"should\s+not|cannot|can't)\b"
    r"(?:\s+\w+){0,4}\s+"
    r"(?:empty|blank|import[-\s]?only|basic\s+imports?|"
    r"test|tests|pytest|skeleton|shell|file|files)\b"
)
_JS_EMPTY_TEST_BYPASS_RE = re.compile(
    r"(?i)(?:^|\s)--(?:passWithNoTests|allowEmpty|allowNoTests)\b"
)
_PYTEST_COLLECT_ONLY_SUCCESS_TEST_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*)(?:python\s+-m\s+)?pytest\b[^;&|]*--collect-only\b"
)
_PYTEST_CD_SRC_SUCCESS_TEST_RE = re.compile(
    r"(?ix)"
    r"(?:^|[;&|]\s*)"
    r"cd\s+[\"']?\.?[\\/]?src[\"']?\s*(?:&&|;)\s*"
    r"(?:(?:python|py)(?:\.exe)?\s+-m\s+)?pytest\b"
)
_PLAN_CODE_EXTENSIONS = {
    ".py",
    ".pyw",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".php",
    ".rb",
    ".swift",
}
_PLAN_FILE_FIELD_KEYS = {
    "file",
    "file_path",
    "filepath",
    "path",
    "target",
    "files",
    "paths",
    "files_to_create",
    "files_to_change",
    "files_to_modify",
    "files_to_update",
    "files_affected",
    "deliverables",
}
_PLAN_LEAF_FILE_KEYS = {
    "file_to_create",
    "files_to_create",
    "new_file",
    "new_files",
    "files_to_add",
    "file_to_change",
    "files_to_change",
    "files_to_modify",
    "files_to_update",
    "files_affected",
    "target_file",
    "target_files",
}
_PLAN_GREENFIELD_ALLOWED_ROOT_PY = {
    "asgi.py",
    "conftest.py",
    "manage.py",
    "setup.py",
    "wsgi.py",
}
_PLAN_NON_IMPL_ROOTS = {
    ".github",
    ".memory",
    ".umbrella",
    ".venv",
    "__pycache__",
    "assets",
    "build",
    "dist",
    "doc",
    "docs",
    "frontend",
    "node_modules",
    "public",
    "reports",
    "scripts",
    "test",
    "tests",
    "tmp",
    "venv",
}


class _LauncherHandle:
    """Adapter so callers can do `handle.wait()` against the legacy launcher."""

    def __init__(self, launcher: Any, task_id: str, timeout: float | None):
        self._launcher = launcher
        self._task_id = task_id
        self._timeout = timeout

    def wait(self) -> dict[str, Any]:
        result = self._launcher.wait_for_result(self._task_id, timeout=self._timeout)
        if result is None:
            return {"status": "error", "error": "launcher timeout", "task_id": self._task_id}
        return result


class _DefaultLauncher:
    """Thin wrapper around `OuroborosLauncher` that returns a wait()-able handle."""

    def __init__(self, repo_root: pathlib.Path, workspace_id: str):
        from umbrella.integration.ouroboros_launcher import OuroborosLauncher

        self._launcher = OuroborosLauncher(repo_root=repo_root, workspace_id=workspace_id)
        self._launcher.start()

    def submit_task(self, task: dict[str, Any], timeout: float | None = None) -> _LauncherHandle:
        task_id = self._launcher.submit_task(task)
        return _LauncherHandle(self._launcher, task_id, timeout)

    def stop(self) -> None:
        try:
            self._launcher.stop()
        except Exception:
            log.debug("Launcher stop failed", exc_info=True)


class PhaseRunner:
    """Orchestrates a task across phases. Each phase runs the Ouroboros agent via a launcher."""

    def __init__(
        self,
        *,
        repo_root: pathlib.Path,
        workspace_id: str,
        drive_root: pathlib.Path | None = None,
        launcher: Any = None,
        palace: MemPalace | None = None,
        phase_timeout_seconds: float | None = None,
        on_envelope: Callable[[ResultEnvelope], None] | None = None,
        candidates_per_phase: int = 1,
    ) -> None:
        self._repo_root = repo_root
        self._workspace_id = workspace_id
        self._drive_root = drive_root or (
            repo_root / "workspaces" / workspace_id / ".memory" / "drive"
        )
        self._launcher = launcher
        self._owns_launcher = False
        self._palace = palace or MemPalace(repo_root, workspace_id)
        self._registry = get_registry(repo_root / "umbrella" / "phases" / "manifests")
        self._watcher = WatcherPollLoop(self._drive_root)
        self._phase_timeout_seconds = phase_timeout_seconds
        self._on_envelope = on_envelope
        self._candidates_per_phase = max(1, int(candidates_per_phase))

    def _ensure_launcher(self) -> Any:
        if self._launcher is None:
            self._launcher = _DefaultLauncher(self._repo_root, self._workspace_id)
            self._owns_launcher = True
        return self._launcher

    def _emit(self, env: ResultEnvelope) -> ResultEnvelope:
        if self._on_envelope:
            try:
                self._on_envelope(env)
            except Exception:
                log.debug("on_envelope callback failed", exc_info=True)
        return env

    def _stop_requested(self) -> bool:
        """Check the canonical stop-file location."""
        stop_path = self._drive_root / "state" / "stop_requested.json"
        return stop_path.exists()

    def _clear_pending_phase_signal(self) -> None:
        try:
            (self._drive_root / "state" / "phase_control_signal.json").unlink(
                missing_ok=True
            )
        except OSError:
            log.debug("Failed to clear stale phase control signal", exc_info=True)

    @staticmethod
    def _tool_row_json_payload(row: dict[str, Any]) -> dict[str, Any]:
        raw = row.get("result_preview") or row.get("result") or {}
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _gmas_tool_row_successful_context(row: dict[str, Any]) -> bool:
        payload = PhaseRunner._tool_row_json_payload(row)
        status = str(payload.get("status") or "").strip().lower()
        if status and status != "ok":
            return False
        if payload.get("error"):
            return False
        return bool(
            status == "ok"
            or payload.get("recommended_pattern")
            or payload.get("key_files")
            or payload.get("retrieval_excerpt")
        )

    @staticmethod
    def _gmas_tool_row_subtask_id(row: dict[str, Any]) -> str:
        for source in (
            row.get("args") if isinstance(row.get("args"), dict) else {},
            PhaseRunner._tool_row_json_payload(row),
        ):
            if not isinstance(source, dict):
                continue
            for key in ("active_subtask_id", "subtask_id", "current_subtask_id"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _promote_to_durable_tool_row_is_valid(row: dict[str, Any]) -> bool:
        payload = PhaseRunner._tool_row_json_payload(row)
        return (
            payload.get("saved") is True
            and str(payload.get("durable_store") or "") == "palace.durable"
            and bool(str(payload.get("durable_node_id") or "").strip())
        )

    def _tool_log_has_tool(
        self,
        *,
        task_id: str,
        tool_names: set[str],
        active_subtask_id: str = "",
        require_successful_context: bool = False,
    ) -> bool:
        path = self._drive_root / "logs" / "tools.jsonl"
        if not task_id or not path.exists():
            return False
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                if str(row.get("tool") or "") in tool_names:
                    if (
                        require_successful_context
                        and not self._gmas_tool_row_successful_context(row)
                    ):
                        continue
                    if active_subtask_id and (
                        self._gmas_tool_row_subtask_id(row) != active_subtask_id
                    ):
                        continue
                    return True
        except OSError:
            log.debug("Failed to inspect tools log for %s", task_id, exc_info=True)
        return False

    @staticmethod
    def _gmas_subtask_requires_context(raw_card: dict[str, Any]) -> bool:
        parts: list[str] = []
        for key in (
            "id",
            "subtask_id",
            "title",
            "name",
            "goal",
            "description",
            "success_test",
            "files_to_create",
            "files_to_change",
            "files_affected",
        ):
            parts.append(str(raw_card.get(key) or ""))
        return bool(
            re.search(
                r"(?i)\b(?:gmas|llm|multi[-_\s]?agent|agent|agents|bot|bots|"
                r"ai[-_\s]?opponent|model[-_\s]?driven|judge)\b",
                "\n".join(parts),
            )
        )

    @staticmethod
    def _gmas_prelude_info_for_task(task: dict[str, Any]) -> dict[str, str | bool]:
        overlays = task.get("context_overlays")
        phase_node = overlays.get("phase_node") if isinstance(overlays, dict) else None
        if isinstance(phase_node, dict):
            for raw_card in phase_node.get("subtasks") or []:
                if not isinstance(raw_card, dict):
                    continue
                if str(raw_card.get("status") or "").lower() == "done":
                    continue
                parts = [
                    raw_card.get("id"),
                    raw_card.get("title"),
                    raw_card.get("goal"),
                ]
                success = raw_card.get("success_test")
                if isinstance(success, dict):
                    parts.append(success.get("value"))
                text = " ".join(str(part or "") for part in parts).strip()
                subtask_id = str(raw_card.get("id") or "").strip()
                if text:
                    return {
                        "query": "GMAS context before execute workspace write: "
                        + text[:500],
                        "subtask_id": subtask_id,
                        "requires_context": PhaseRunner._gmas_subtask_requires_context(
                            raw_card
                        ),
                    }
        return {
            "query": (
                "GMAS context before execute workspace write: LLM-backed "
                "multi-agent game bots using inherited runtime aliases"
            ),
            "subtask_id": "",
            "requires_context": True,
        }

    @staticmethod
    def _gmas_prelude_query_for_task(task: dict[str, Any]) -> str:
        return str(PhaseRunner._gmas_prelude_info_for_task(task).get("query") or "")

    def _inject_gmas_prewrite_context(self, task: dict[str, Any]) -> None:
        overlays = task.get("context_overlays")
        if not isinstance(overlays, dict):
            return
        if overlays.get("gmas_prewrite_required") is not True:
            return
        phase_node = overlays.get("phase_node")
        manifest_id = ""
        if isinstance(phase_node, dict):
            manifest_id = str(phase_node.get("manifest_id") or "")
        if manifest_id != "execute":
            return
        prelude_info = self._gmas_prelude_info_for_task(task)
        active_subtask_id = str(prelude_info.get("subtask_id") or "")
        if prelude_info.get("requires_context") is False:
            overlays["gmas_prewrite_context_injected"] = "not_required_for_subtask"
            overlays["gmas_prewrite_context_subtask_id"] = active_subtask_id
            return
        task_id = str(task.get("id") or "").strip()
        if self._tool_log_has_tool(
            task_id=task_id,
            tool_names={"get_gmas_context", "search_gmas_knowledge"},
            active_subtask_id=active_subtask_id,
            require_successful_context=True,
        ):
            overlays["gmas_prewrite_context_injected"] = "already_present"
            overlays["gmas_prewrite_context_subtask_id"] = active_subtask_id
            return
        query = str(prelude_info.get("query") or "")
        try:
            from umbrella.retrieval.gmas_context import build_gmas_context

            payload = build_gmas_context(
                self._repo_root,
                query,
                max_results=4,
                max_chars_per_hit=6000,
            )
            if isinstance(payload, dict):
                payload = dict(payload)
                payload.setdefault("status", "ok")
                if active_subtask_id:
                    payload["active_subtask_id"] = active_subtask_id
            status = "ok"
        except Exception as exc:
            log.debug("GMAS execute prelude retrieval failed", exc_info=True)
            payload = {"status": "error", "error": str(exc), "query": query}
            status = "error"
        payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
        task["input"] = (
            str(task.get("input") or "")
            + "\n\n## Umbrella execute prelude: GMAS context\n"
            + "Umbrella detected that the current execute subtask needs "
            + "GMAS/LLM agent context and retrieved it before the worker can "
            + "write that subtask. This satisfies the first-write GMAS gate "
            + "for the scoped subtask; "
            + "refresh with `get_gmas_context`/`search_gmas_knowledge` before "
            + "writing task-specific GMAS agent code if the prelude is not "
            + "specific enough.\n"
            + "```json\n"
            + payload_text[:12000]
            + ("\n...[truncated]" if len(payload_text) > 12000 else "")
            + "\n```\n"
        )
        overlays["gmas_prewrite_context_injected"] = status
        overlays["gmas_prewrite_context_query"] = query
        overlays["gmas_prewrite_context_subtask_id"] = active_subtask_id
        log_dir = self._drive_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task_id": task_id,
            "tool": "get_gmas_context",
            "args": {
                "query": query,
                "max_results": 4,
                "injected_by": "umbrella_phase_prelude",
                "active_subtask_id": active_subtask_id,
            },
            "result_preview": json.dumps(
                {
                    "status": status,
                    "injected_by": "umbrella_phase_prelude",
                    "query": query,
                    "active_subtask_id": active_subtask_id,
                    "recommended_pattern": payload.get("recommended_pattern")
                    if isinstance(payload, dict)
                    else None,
                    "confidence": payload.get("confidence")
                    if isinstance(payload, dict)
                    else None,
                },
                ensure_ascii=False,
            ),
        }
        with (log_dir / "tools.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _read_phase_control_records(
        self,
        *,
        task_id: str,
        phase_started_at: float | None,
    ) -> list[dict[str, Any]]:
        state_dir = self._drive_root / "state"
        records: list[dict[str, Any]] = []
        for line_path in (state_dir / "phase_control_signals.jsonl",):
            if not line_path.exists():
                continue
            try:
                for line in line_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        records.append(row)
            except OSError:
                log.debug("Failed to read phase control ledger", exc_info=True)
        single = state_dir / "phase_control_signal.json"
        if single.exists():
            try:
                row = json.loads(single.read_text(encoding="utf-8"))
                if isinstance(row, dict):
                    records.append(row)
            except (OSError, json.JSONDecodeError):
                log.debug("Failed to read current phase control signal", exc_info=True)

        filtered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in records:
            row_task_id = str(row.get("task_id") or "")
            if task_id and row_task_id and row_task_id != task_id:
                continue
            created = row.get("created_at")
            if (
                phase_started_at is not None
                and isinstance(created, (int, float))
                and float(created) < float(phase_started_at)
            ):
                continue
            signal_id = str(row.get("signal_id") or "").strip()
            if signal_id:
                dedupe_key = "signal:" + signal_id
            else:
                try:
                    dedupe_key = "row:" + json.dumps(
                        row,
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                    )
                except TypeError:
                    dedupe_key = "row:" + repr(sorted(row.items()))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            filtered.append(row)
        return filtered

    @staticmethod
    def _micro_review_revision_reason(payload: dict[str, Any]) -> str:
        parts: list[str] = []
        revisions = payload.get("revisions")
        if isinstance(revisions, list):
            parts.extend(str(item).strip() for item in revisions[:5] if str(item).strip())
        notes = str(payload.get("notes") or "").strip()
        if notes:
            parts.append(notes)
        if not parts:
            return "micro review requested revisions"
        details = "; ".join(parts)
        if len(details) > 4000:
            details = details[:3997].rstrip() + "..."
        return "micro review requested revisions: " + details

    def _latest_revision_contract(
        self,
        *,
        phase_node: PhaseNode,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = str(outcome.get("task_id") or "")
        rows = self._read_phase_control_records(
            task_id=task_id,
            phase_started_at=phase_node.started_at,
        )
        for row in reversed(rows):
            if str(row.get("kind") or "") != "submit_micro_review":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if str((payload or {}).get("verdict") or "").strip().lower() != "revise":
                continue
            revisions = payload.get("revisions")
            items: list[str] = []
            if isinstance(revisions, list):
                items = [str(item).strip() for item in revisions if str(item).strip()]
            notes = str((payload or {}).get("notes") or "").strip()
            return {
                "source_phase": phase_node.id,
                "source_task_id": task_id,
                "verdict": "revise",
                "revisions": items,
                "notes": notes,
            }
        return {}

    @staticmethod
    def _merged_revision_contract(
        existing: Any,
        latest: dict[str, Any],
    ) -> dict[str, Any]:
        if not latest:
            return {}
        contracts: list[dict[str, Any]] = []
        if isinstance(existing, dict):
            contracts.append(existing)
        contracts.append(latest)
        revisions: list[str] = []
        notes: list[str] = []
        sources: list[dict[str, str]] = []
        for contract in contracts:
            raw_revisions = contract.get("revisions")
            if isinstance(raw_revisions, list):
                for item in raw_revisions:
                    text = str(item or "").strip()
                    if text and text not in revisions:
                        revisions.append(text)
            note = str(contract.get("notes") or "").strip()
            if note and note not in notes:
                notes.append(note)
            source_phase = str(contract.get("source_phase") or "").strip()
            source_task_id = str(contract.get("source_task_id") or "").strip()
            if source_phase or source_task_id:
                source = {
                    "source_phase": source_phase,
                    "source_task_id": source_task_id,
                }
                if source not in sources:
                    sources.append(source)
        merged = dict(latest)
        merged["revisions"] = revisions
        if notes:
            merged["notes"] = "\n\n".join(notes)
        if sources:
            merged["sources"] = sources
        return merged

    def _phase_completion_failure(
        self,
        *,
        phase_node: PhaseNode,
        plan: PhasePlan,
        manifest: Any,
        outcome: dict[str, Any],
    ) -> str:
        required = list(getattr(manifest.exit_criteria, "required_calls", ()) or ())
        if not required:
            return ""
        task_id = str(outcome.get("task_id") or "")
        records = self._read_phase_control_records(
            task_id=task_id,
            phase_started_at=phase_node.started_at,
        )
        by_kind: dict[str, dict[str, Any]] = {}
        for row in records:
            kind = str(row.get("kind") or "")
            if kind:
                by_kind[kind] = row
        missing = [name for name in required if name not in by_kind]
        if missing:
            return (
                "phase exit criteria missing required call(s): "
                + ", ".join(sorted(missing))
            )

        preflight = by_kind.get("submit_preflight_report")
        if preflight and (preflight.get("payload") or {}).get("status") == "blocked":
            blockers = (preflight.get("payload") or {}).get("blockers") or []
            return "preflight reported blocked: " + ", ".join(map(str, blockers))

        verification = by_kind.get("submit_verification")
        if verification and (verification.get("payload") or {}).get("status") != "pass":
            if self._phase_loop_back_target(
                phase_node=phase_node,
                outcome=outcome,
            ):
                return ""
            details = str((verification.get("payload") or {}).get("details") or "")
            return f"verification did not pass: {details[:500]}"

        palace_rules = list(
            getattr(manifest.exit_criteria, "required_palace_writes", ()) or ()
        ) + list(getattr(manifest.exit_criteria, "min_palace_writes", ()) or ())
        for rule in palace_rules:
            needed = max(1, int(getattr(rule, "n", 1) or 1))
            tools = self._palace_write_tools_for_rule(manifest=manifest, rule=rule)
            count = self._phase_required_palace_write_count(
                task_id=task_id,
                rule=rule,
            )
            if count < needed:
                tag_hint = f" tag={rule.tag}" if getattr(rule, "tag", None) else ""
                tool_hint = "/".join(tools) if tools else "palace_add"
                return (
                    "phase exit criteria missing palace writes: "
                    f"{tool_hint} {count}/{needed} for store={rule.store}{tag_hint}. "
                    f"Call {tool_hint} with concrete, non-placeholder content and "
                    "wait for accepted/saved results before calling the phase "
                    "completion tool again."
                )

        if phase_node.id == "research":
            task_run_id = str(outcome.get("run_id") or "").strip()
            if not task_run_id and ":" in task_id:
                task_run_id = task_id.split(":", 1)[0]
            summary_failure = self._latest_research_summary_handoff_failure(
                run_id=task_run_id or None,
                min_valid_findings=self._research_summary_min_valid_findings_for_manifest(
                    manifest
                ),
            )
            if summary_failure:
                return summary_failure

        micro_review = by_kind.get("submit_micro_review")
        payload = micro_review.get("payload") if micro_review else {}
        if not isinstance(payload, dict):
            payload = {}
        verdict = str((payload or {}).get("verdict") or "").strip().lower()
        if micro_review and verdict == "abort":
            return "micro review aborted the phase"
        if micro_review and verdict == "revise":
            target = self._phase_loop_back_target(
                phase_node=phase_node,
                outcome=outcome,
            )
            if target and plan.get_node(target) is not None:
                return self._micro_review_revision_reason(payload)

            explicit_target = ""
            loop_signal = by_kind.get("loop_back_to")
            if loop_signal:
                loop_payload = loop_signal.get("payload")
                if isinstance(loop_payload, dict):
                    explicit_target = str(loop_payload.get("phase") or "").strip()
            fallback_target = explicit_target or self._default_review_loop_back_target(
                phase_node.id
            )
            if fallback_target and plan.get_node(fallback_target) is not None:
                # A capped repeated review can intentionally continue only when
                # _phase_loop_back_target suppressed a known noisy review class.
                return ""
            return (
                "micro review requested revisions but no accepted loop_back_to "
                "signal was recorded"
            )

        if phase_node.id in {"plan", "plan_review"}:
            task_run_id = str(outcome.get("run_id") or "").strip()
            if not task_run_id and ":" in task_id:
                task_run_id = task_id.split(":", 1)[0]
            floor_failure = self._latest_phase_plan_execution_floor_failure(
                run_id=task_run_id or None,
            )
            if floor_failure:
                return floor_failure

        return ""

    @staticmethod
    def _text_excerpt(value: Any, *, limit: int = 6000) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        if limit < 200:
            return text[:limit].rstrip()
        head = max(1, limit // 2)
        tail = max(1, limit - head - 40)
        return (
            text[:head].rstrip()
            + "\n...[truncated retry context]...\n"
            + text[-tail:].lstrip()
        )

    @staticmethod
    def _subtask_retry_payload(card: SubtaskCard) -> dict[str, Any]:
        return {
            "id": card.id,
            "title": card.title,
            "status": card.status,
            "goal": card.goal,
            "success_test": card.success_test.value if card.success_test else "",
            "files_to_create": list(card.files_to_create or []),
            "files_to_change": list(card.files_to_change or []),
            "files_affected": list(card.files_affected or []),
        }

    def _phase_retry_context(
        self,
        *,
        phase_node: PhaseNode,
        outcome: dict[str, Any],
        retry_reason: str,
    ) -> dict[str, Any]:
        task_id = str(outcome.get("task_id") or "").strip()
        result_text = (
            outcome.get("result")
            or outcome.get("final_message")
            or outcome.get("message")
            or outcome.get("error")
            or ""
        )
        context: dict[str, Any] = {
            "source_phase": phase_node.id,
            "source_task_id": task_id,
            "retry_reason": retry_reason,
        }
        status = str(outcome.get("status") or outcome.get("outcome") or "").strip()
        if status:
            context["last_task_status"] = status
        excerpt = self._text_excerpt(result_text, limit=6000)
        if excerpt:
            context["last_task_result_excerpt"] = excerpt
        if task_id:
            context["full_task_result_hint"] = (
                "Full task result is available through get_task_result("
                f'task_id="{task_id}") or under .memory/drive/task_results/.'
            )
        if phase_node.id == "execute" and phase_node.subtasks:
            pending = [card for card in phase_node.subtasks if card.status != "done"]
            if pending:
                context["next_pending_subtask"] = self._subtask_retry_payload(pending[0])
                context["pending_subtask_ids"] = [card.id for card in pending]
        return context

    def _mirror_phase_retry_context_to_palace(
        self,
        *,
        phase_node: PhaseNode,
        run_id: str,
        retry_context: dict[str, Any],
    ) -> None:
        if not retry_context:
            return
        subtask_id = ""
        pending = retry_context.get("next_pending_subtask")
        if isinstance(pending, dict):
            subtask_id = str(pending.get("id") or "").strip()
        try:
            self._palace.add(
                store="palace.subtask" if subtask_id else "palace.phase",
                content=json.dumps(
                    {
                        "artifact": "phase_retry_context",
                        "run_id": run_id,
                        "workspace_id": self._workspace_id,
                        **retry_context,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                tier="hot",
                scope="subtask_scoped" if subtask_id else "run_scoped",
                tags=[
                    "phase_retry",
                    "execution_failure",
                    "retry_context",
                ],
                phase=phase_node.id,
                subtask_id=subtask_id or None,
                run_id=run_id,
                verified=True,
                source_path=".memory/drive/task_results",
                extra={
                    "source_task_id": str(
                        retry_context.get("source_task_id") or ""
                    ),
                    "retry_reason": str(
                        retry_context.get("retry_reason") or ""
                    )[:500],
                },
            )
        except Exception:
            log.debug("Failed to mirror phase retry context to palace", exc_info=True)

    def _finish_phase_loop_back(
        self,
        *,
        phase_node: PhaseNode,
        plan: PhasePlan,
        run_id: str,
        outcome: dict[str, Any],
        loop_back_target: str,
        retry_reason: str,
    ) -> tuple[PhaseResult, ResultEnvelope]:
        phase_node.status = "done"
        phase_node.ended_at = time.time()
        overlay: dict[str, Any] = {"retry_reason": retry_reason}
        retry_context = self._phase_retry_context(
            phase_node=phase_node,
            outcome=outcome,
            retry_reason=retry_reason,
        )
        if retry_context:
            overlay["retry_context"] = retry_context
        revision_contract = self._latest_revision_contract(
            phase_node=phase_node,
            outcome=outcome,
        )
        target = plan.get_node(loop_back_target)
        existing_contract: Any = None
        if target is not None and isinstance(target.overlay, dict):
            existing_contract = target.overlay.get("revision_contract")
        if revision_contract:
            overlay["revision_contract"] = self._merged_revision_contract(
                existing_contract,
                revision_contract,
            )
        phase_node.overlay = dict(overlay)
        if target is not None and target.id != phase_node.id:
            target.overlay = dict(overlay)
        self._mirror_phase_retry_context_to_palace(
            phase_node=phase_node,
            run_id=run_id,
            retry_context=retry_context,
        )
        save_plan(plan, self._drive_root)
        result = PhaseResult(
            phase_id=phase_node.id,
            outcome="loop_back",
            loop_back_target=loop_back_target,
        )
        envelope = self._emit(ResultEnvelope.success(
            data={
                "event": "phase_done",
                "phase": phase_node.id,
                "outcome": result.outcome,
                "retry_reason": retry_reason,
                "events": outcome.get("event_count", 0),
            },
            run_id=run_id,
            phase=phase_node.id,
            took_ms=int(
                (phase_node.ended_at - (phase_node.started_at or phase_node.ended_at))
                * 1000
            ),
        ))
        return result, envelope

    def _phase_tool_success_count(self, *, task_id: str, tool_name: str) -> int:
        path = self._drive_root / "logs" / "tools.jsonl"
        if not path.exists():
            return 0
        count = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                if str(row.get("tool") or "") != tool_name:
                    continue
                if is_effective_write_tool_log_row(row):
                    count += 1
        except OSError:
            log.debug("Failed to read tools log for %s count", tool_name, exc_info=True)
        return count

    @staticmethod
    def _palace_write_tools_for_rule(*, manifest: Any, rule: Any) -> tuple[str, ...]:
        allowed = set(getattr(manifest, "allowed_tools", set()) or set())
        forbidden = set(getattr(manifest, "forbidden_tools", set()) or set())
        tools: list[str] = []
        if "palace_add" in allowed and "palace_add" not in forbidden:
            tools.append("palace_add")
        if (
            getattr(rule, "store", "") == "palace.run"
            and "submit_research_summary" in allowed
            and "submit_research_summary" not in forbidden
        ):
            tools.append("submit_research_summary")
        if (
            getattr(rule, "store", "") == "palace.run"
            and "propose_phase_plan" in allowed
            and "propose_phase_plan" not in forbidden
        ):
            tools.append("propose_phase_plan")
        if (
            getattr(rule, "store", "") == "palace.durable"
            and "promote_to_durable" in allowed
            and "promote_to_durable" not in forbidden
        ):
            tools.append("promote_to_durable")
        return tuple(tools or ("palace_add",))

    @staticmethod
    def _tool_row_tags(row: dict[str, Any]) -> set[str]:
        args = row.get("args")
        if not isinstance(args, dict):
            args = {}
        raw = args.get("tags") or args.get("tag") or ""
        values: list[str] = []
        if isinstance(raw, str):
            values.extend(part.strip() for part in raw.replace(";", ",").split(","))
            values.extend(part.strip() for part in raw.split())
        elif isinstance(raw, (list, tuple, set)):
            values.extend(str(part).strip() for part in raw)
        return {value for value in values if value}

    @classmethod
    def _tool_row_has_tag(
        cls,
        row: dict[str, Any],
        tag: str | None,
        *,
        allow_missing_tags: bool,
    ) -> bool:
        if not tag:
            return True
        tags = cls._tool_row_tags(row)
        if not tags:
            return allow_missing_tags
        return tag in tags

    @staticmethod
    def _row_json_payload(row: dict[str, Any], *keys: str) -> dict[str, Any]:
        for key in keys:
            value = row.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip():
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def _accepted_palace_add_ids_for_task(self, *, task_id: str) -> set[str]:
        if not task_id:
            return set()
        path = self._drive_root / "logs" / "tools.jsonl"
        if not path.exists():
            return set()
        accepted: set[str] = set()
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                if str(row.get("tool") or "") != "palace_add":
                    continue
                result = self._row_json_payload(row, "result_preview", "result")
                if result.get("saved") is not True:
                    continue
                for key in ("id", "memory_id", "artifact_id"):
                    value = str(result.get(key) or "").strip()
                    if value:
                        accepted.add(value)
                legacy = result.get("legacy")
                if isinstance(legacy, dict):
                    value = str(legacy.get("id") or "").strip()
                    if value:
                        accepted.add(value)
        except OSError:
            return accepted
        return accepted

    def _research_summary_tool_row_is_valid(
        self, row: dict[str, Any], *, task_id: str
    ) -> bool:
        args = row.get("args")
        if not isinstance(args, dict):
            args = self._row_json_payload(row, "args")
        notes = str(args.get("notes") or "").strip()
        if len(notes) < 20 or _RESEARCH_SUMMARY_PLACEHOLDER_RE.search(notes):
            return False
        architecture_id = str(args.get("architecture_id") or "").strip()
        if not architecture_id:
            return False
        raw_findings = args.get("findings_ids")
        if not isinstance(raw_findings, list):
            return False
        findings = [str(item).strip() for item in raw_findings if str(item).strip()]
        if not findings:
            return False
        accepted = self._accepted_palace_add_ids_for_task(task_id=task_id)
        return any(item in accepted for item in findings)

    def _phase_required_palace_write_count(self, *, task_id: str, rule: Any) -> int:
        path = self._drive_root / "logs" / "tools.jsonl"
        if not path.exists():
            return 0
        count = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                tool_name = str(row.get("tool") or "")
                if not is_effective_write_tool_log_row(row):
                    continue
                if tool_name == "palace_add":
                    if self._tool_row_has_tag(
                        row,
                        getattr(rule, "tag", None),
                        allow_missing_tags=True,
                    ):
                        count += 1
                    continue
                if (
                    tool_name == "submit_research_summary"
                    and getattr(rule, "store", "") == "palace.run"
                    and self._research_summary_tool_row_is_valid(
                        row,
                        task_id=task_id,
                    )
                    and self._tool_row_has_tag(
                        row,
                        getattr(rule, "tag", None),
                        allow_missing_tags=True,
                    )
                ):
                    count += 1
                    continue
                if (
                    tool_name == "propose_phase_plan"
                    and getattr(rule, "store", "") == "palace.run"
                    and self._tool_row_has_tag(
                        row,
                        getattr(rule, "tag", None),
                        allow_missing_tags=True,
                    )
                ):
                    count += 1
                    continue
                if (
                    tool_name == "promote_to_durable"
                    and getattr(rule, "store", "") == "palace.durable"
                    and self._promote_to_durable_tool_row_is_valid(row)
                    and self._tool_row_has_tag(
                        row,
                        getattr(rule, "tag", None),
                        allow_missing_tags=False,
                    )
                ):
                    count += 1
        except OSError:
            log.debug("Failed to read tools log for palace write count", exc_info=True)
        return count

    @staticmethod
    def _default_review_loop_back_target(phase_id: str) -> str:
        return {
            "research_review": "research",
            "plan_review": "plan",
            "final_review": "execute",
            "verify": "execute",
        }.get(phase_id, "")

    def _phase_loop_back_target(
        self,
        *,
        phase_node: PhaseNode,
        outcome: dict[str, Any],
    ) -> str:
        task_id = str(outcome.get("task_id") or "")
        records = self._read_phase_control_records(
            task_id=task_id,
            phase_started_at=phase_node.started_at,
        )
        for row in reversed(records):
            if str(row.get("kind") or "") != "loop_back_to":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            target = str((payload or {}).get("phase") or "").strip()
            if target:
                return target
        for row in reversed(records):
            if str(row.get("kind") or "") != "submit_micro_review":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if str((payload or {}).get("verdict") or "") == "revise":
                return self._default_review_loop_back_target(phase_node.id)
        for row in reversed(records):
            kind = str(row.get("kind") or "")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if (
                kind == "submit_final_review"
                and str((payload or {}).get("outcome") or "") == "loop_back"
            ):
                return self._default_review_loop_back_target(phase_node.id)
            if (
                kind == "submit_verification"
                and str((payload or {}).get("status") or "") == "fail"
            ):
                return self._default_review_loop_back_target(phase_node.id)
        return ""

    @staticmethod
    def _research_summary_min_valid_findings_for_manifest(manifest: Any) -> int:
        criteria = getattr(manifest, "exit_criteria", None)
        rules = list(getattr(criteria, "required_palace_writes", ()) or ()) + list(
            getattr(criteria, "min_palace_writes", ()) or ()
        )
        required = 1
        for rule in rules:
            if str(getattr(rule, "store", "") or "") != "palace.run":
                continue
            try:
                n = max(1, int(getattr(rule, "n", 1) or 1))
            except (TypeError, ValueError):
                n = 1
            # Research min_palace_writes is a finding floor. The summary is the
            # handoff after those accepted palace_add findings, not a substitute
            # for one of them.
            required = max(required, n)
        return required

    def _latest_research_summary_handoff_failure(
        self, *, run_id: str | None, min_valid_findings: int = 1
    ) -> str:
        path = self._drive_root / "state" / "research_summary_latest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return "latest research summary artifact is missing or unreadable"
        if run_id and str(data.get("run_id") or "") not in {"", run_id}:
            return "latest research summary artifact belongs to a different run"
        architecture_id = str(data.get("architecture_id") or "").strip()
        notes = str(data.get("notes") or "").strip()
        findings = data.get("findings_ids")
        if not isinstance(findings, list):
            findings = []
        concrete_findings = [
            str(item).strip() for item in findings if str(item).strip()
        ]
        if not architecture_id:
            return "latest research summary is missing architecture_id"
        if len(notes) < 20:
            return "latest research summary notes are too short for planning handoff"
        if _RESEARCH_SUMMARY_PLACEHOLDER_RE.search(notes):
            return "latest research summary notes are placeholder/pending text"
        task_id = str(data.get("task_id") or "").strip()
        if not task_id and run_id:
            task_id = f"{run_id}:research"
        accepted_ids = self._accepted_palace_add_ids_for_task(task_id=task_id)
        valid_findings = [item for item in concrete_findings if item in accepted_ids]
        if len(valid_findings) < min_valid_findings:
            return (
                "latest research summary references "
                f"{len(valid_findings)}/{min_valid_findings} accepted palace_add "
                "finding id(s); use the id or legacy.id returned by palace_add, "
                "not invented finding labels"
            )
        return ""

    def _latest_research_summary_has_handoff_floor(
        self, *, run_id: str | None
    ) -> bool:
        return (
            self._latest_research_summary_handoff_failure(
                run_id=run_id,
                min_valid_findings=1,
            )
            == ""
        )

    def _latest_phase_plan_has_execution_floor(self, *, run_id: str | None) -> bool:
        return self._latest_phase_plan_execution_floor_failure(run_id=run_id) == ""

    def _phase_plan_execution_payload(self, *, run_id: str | None) -> tuple[dict[str, Any], str]:
        for filename in (
            "phase_plan_submitted_latest.json",
            "phase_plan_proposal_latest.json",
        ):
            path = self._drive_root / "state" / filename
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if run_id and str(data.get("run_id") or "") not in {"", run_id}:
                continue
            return data, filename
        return {}, ""

    def _latest_phase_plan_execution_floor_failure(
        self, *, run_id: str | None
    ) -> str:
        data, source = self._phase_plan_execution_payload(run_id=run_id)
        if not data:
            return "submitted phase plan artifact is missing or unreadable"
        if run_id and str(data.get("run_id") or "") not in {"", run_id}:
            return "submitted phase plan artifact belongs to a different run"
        plan = data.get("plan") if isinstance(data, dict) else None
        if not isinstance(plan, dict):
            return "submitted phase plan artifact does not contain a plan object"
        plan, embedded_issue = self._coerce_embedded_phase_plan(plan)
        if embedded_issue:
            return embedded_issue
        artifact_notes = data.get("notes") if isinstance(data, dict) else ""
        content_plan = {"plan": plan, "notes": artifact_notes} if artifact_notes else plan
        content_issue = self._phase_plan_content_issue(content_plan)
        if content_issue:
            return content_issue
        layout_issue = self._phase_plan_greenfield_layout_issue(plan)
        if layout_issue:
            return layout_issue
        subtasks = self._execution_items_from_plan(plan)
        if not subtasks:
            return (
                "submitted phase plan has no executable subtasks/steps/phases; "
                "submit a compact top-level `subtasks` array with real leaf "
                "objects, not only title/policy/risk notes or an empty tool call"
            )
        compactness_failure = self._phase_plan_compactness_failure(subtasks, plan)
        if compactness_failure:
            return compactness_failure
        broad_leaf_failure = self._phase_plan_broad_leaf_failure(subtasks, plan)
        if broad_leaf_failure:
            return broad_leaf_failure
        missing: list[str] = []
        invalid: list[str] = []
        for idx, item in enumerate(subtasks):
            item_id = str(
                item.get("id")
                or item.get("subtask_id")
                or item.get("title")
                or item.get("name")
                or f"item_{idx + 1}"
            )
            issue = self._success_test_issue_from_plan_item(item)
            if issue == "missing success test":
                missing.append(item_id)
            elif issue:
                invalid.append(f"{item_id} ({issue})")
        if missing:
            return (
                "submitted phase plan has subtask(s) without success tests: "
                + ", ".join(missing[:8])
            )
        if invalid:
            return (
                "submitted phase plan has non-automatable success test(s): "
                + ", ".join(invalid[:8])
            )
        missing_files = self._phase_plan_missing_leaf_file_fields(subtasks, plan)
        if missing_files:
            return missing_files
        generic_failure = self._phase_plan_generic_success_test_failure(subtasks)
        if generic_failure:
            return generic_failure
        pytest_target_failures = phase_plan_pytest_target_availability_messages(
            subtasks=subtasks,
            plan=plan,
            workspace_root=self._repo_root / "workspaces" / self._workspace_id,
            workspace_id=self._workspace_id,
        )
        if pytest_target_failures:
            return "latest phase plan has unavailable pytest proof target: " + pytest_target_failures[0]
        frontend_test_failure = self._phase_plan_frontend_test_target_failure(
            subtasks, plan
        )
        if frontend_test_failure:
            return frontend_test_failure
        frontend_build_failure = self._phase_plan_frontend_build_order_failure(
            subtasks, plan
        )
        if frontend_build_failure:
            return frontend_build_failure
        return ""

    def _phase_plan_compactness_failure(
        self, subtasks: list[dict[str, Any]], plan: dict[str, Any]
    ) -> str:
        if len(subtasks) <= 16:
            return ""
        plan_text = "\n".join(self._iter_plan_strings(plan)).lower()
        if not re.search(
            r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|frontend|backend|"
            r"websocket|fastapi|react|typescript|civilization|game)\b",
            plan_text,
        ):
            return ""
        return (
            f"latest phase plan has {len(subtasks)} executable leaves; keep "
            "large greenfield Umbrella plans compact at roughly 8-16 leaves "
            "by grouping related work into vertical slices with one real "
            "success_test each"
        )

    def _phase_plan_broad_leaf_failure(
        self, subtasks: list[dict[str, Any]], plan: dict[str, Any]
    ) -> str:
        if len(subtasks) < 6:
            return ""
        plan_text = "\n".join(self._iter_plan_strings(plan)).lower()
        complex_greenfield = bool(
            re.search(
                r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|frontend|backend|"
                r"websocket|fastapi|react|typescript|civilization|game)\b",
                plan_text,
            )
        )
        if not complex_greenfield:
            return ""

        too_broad: list[str] = []
        for idx, item in enumerate(subtasks, start=1):
            label = " ".join(
                str(item.get(key) or "")
                for key in ("id", "subtask_id", "title", "name", "goal", "description", "mode")
            ).lower()
            if re.search(
                r"\b(?:setup|initiali[sz]e|scaffold|project structure|"
                r"documentation|docs|final|e2e|smoke|verification|launch)\b",
                label,
            ):
                continue
            paths = sorted(self._phase_plan_file_paths(item))
            if len(paths) <= 4:
                continue
            code_paths = [path for path in paths if self._plan_path_looks_like_code(path)]
            if len(code_paths) <= 3:
                continue
            item_id = str(
                item.get("id")
                or item.get("subtask_id")
                or item.get("title")
                or item.get("name")
                or f"subtask_{idx}"
            )
            too_broad.append(f"{item_id} ({len(paths)} files)")

        if not too_broad:
            return ""
        return (
            "latest phase plan has implementation subtask(s) that are too broad "
            "for a bounded Umbrella execute loop: "
            + ", ".join(too_broad[:8])
            + ". Split large greenfield/full-stack leaves into narrower vertical "
            "subtasks of about 2-4 files each, with one behavior-focused "
            "success_test per leaf, instead of packing multiple domains or "
            "frontend/backend surfaces behind one pytest/build command."
        )

    def _phase_plan_greenfield_layout_issue(self, plan: dict[str, Any]) -> str:
        if self._workspace_existing_impl_roots():
            return ""
        paths = self._phase_plan_file_paths(plan)
        code_paths = {path for path in paths if self._plan_path_looks_like_code(path)}
        if not code_paths:
            return ""
        subtasks = self._execution_items_from_plan(plan)
        plan_text = "\n".join(self._iter_plan_strings(plan)).lower()
        has_python = any(
            pathlib.PurePosixPath(path).suffix.lower() == ".py" for path in code_paths
        )
        has_frontend = any(
            pathlib.PurePosixPath(path).suffix.lower() in {".tsx", ".jsx", ".ts", ".js"}
            or path.startswith("frontend/")
            for path in code_paths
        )
        has_project_config = any(
            pathlib.PurePosixPath(path).name.lower()
            in {"pyproject.toml", "package.json"}
            for path in paths
        )
        has_agent_llm = bool(
            re.search(r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|bot)\b", plan_text)
        )
        complex_greenfield = (
            len(subtasks) >= 3
            or has_project_config
            or (has_python and has_frontend)
            or has_agent_llm
        )
        if not complex_greenfield:
            return ""

        disallowed_python: list[str] = []
        disallowed_python_tests: list[str] = []
        for rel in sorted(code_paths):
            pure = pathlib.PurePosixPath(rel)
            if pure.suffix.lower() != ".py":
                continue
            parts = [part for part in pure.parts if part and part != "."]
            if not parts:
                continue
            lowered = [part.lower() for part in parts]
            top = lowered[0]
            name = lowered[-1]
            is_test_path = (
                name.startswith("test_")
                or name.endswith("_test.py")
                or any(part in {"test", "tests"} for part in lowered)
            )
            if is_test_path and top not in {"tests", "test"}:
                disallowed_python_tests.append(rel)
                continue
            if top in {"src", "tests", "test", "docs", "doc", "frontend"}:
                continue
            if name.startswith("test_") or "tests" in lowered or "test" in lowered:
                continue
            if len(parts) == 1 and name in _PLAN_GREENFIELD_ALLOWED_ROOT_PY:
                continue
            if top in _PLAN_NON_IMPL_ROOTS:
                continue
            disallowed_python.append(rel)

        if disallowed_python_tests:
            return (
                "latest phase plan puts greenfield Python pytest/test modules "
                "outside `tests/`; move "
                f"{disallowed_python_tests[:8]} under `tests/` or make them "
                "non-pytest verification scripts with non-test filenames"
            )

        if disallowed_python:
            return (
                "latest phase plan puts greenfield Python application/library "
                "code outside `src/<package>/...`; move "
                f"{disallowed_python[:8]} under `src/` and keep tests under `tests/`"
            )

        requires_docs = (
            (has_agent_llm and (len(subtasks) >= 4 or has_project_config))
            or (has_python and has_frontend)
            or len(subtasks) >= 6
        )
        has_docs = any(path.startswith("docs/") for path in paths)
        if requires_docs and not has_docs:
            return (
                "latest phase plan for a complex greenfield/LLM project lacks "
                "a durable `docs/` architecture/topology artifact"
            )
        return ""

    def _workspace_existing_impl_roots(self) -> set[str]:
        root = self._repo_root / "workspaces" / self._workspace_id
        if not root.is_dir():
            return set()
        roots: set[str] = set()
        try:
            for child in root.iterdir():
                name = child.name
                lower = name.lower()
                if lower.startswith(".") or lower in _PLAN_NON_IMPL_ROOTS:
                    continue
                if child.is_file():
                    suffix = child.suffix.lower()
                    if (
                        suffix in _PLAN_CODE_EXTENSIONS
                        and lower not in _PLAN_GREENFIELD_ALLOWED_ROOT_PY
                    ):
                        roots.add(name)
                    continue
                if child.is_dir():
                    has_code = any(
                        p.is_file()
                        and p.suffix.lower() in _PLAN_CODE_EXTENSIONS
                        and "node_modules" not in {part.lower() for part in p.parts}
                        and "__pycache__" not in {part.lower() for part in p.parts}
                        for p in child.rglob("*")
                    )
                    if has_code:
                        roots.add(name)
        except OSError:
            return set()
        return roots

    @staticmethod
    def _plan_path_looks_like_code(path: str) -> bool:
        pure = pathlib.PurePosixPath(str(path or "").replace("\\", "/").strip("/"))
        return pure.suffix.lower() in _PLAN_CODE_EXTENSIONS or pure.name.lower() in {
            "package.json",
            "pyproject.toml",
            "vite.config.ts",
            "tsconfig.json",
        }

    def _normalise_phase_plan_path(self, raw: str) -> str:
        norm = str(raw or "").replace("\\", "/").strip().strip("`'\"")
        if (
            not norm
            or " " in norm
            or norm.startswith(("http://", "https://"))
        ):
            return ""
        norm = norm.strip("/")
        workspace_id = str(self._workspace_id or "").strip().strip("/\\")
        if norm.startswith("workspaces/") and norm.count("/") >= 2:
            _, workspace, rest = norm.split("/", 2)
            if not workspace_id or workspace.lower() == workspace_id.lower():
                norm = rest
        if workspace_id:
            prefix = f"{workspace_id}/"
            if norm.lower() == workspace_id.lower():
                return ""
            if norm.lower().startswith(prefix.lower()):
                norm = norm[len(prefix) :]
        return norm.strip("/")

    def _phase_plan_file_paths(self, value: Any) -> set[str]:
        paths: set[str] = set()

        def add_path(raw: Any) -> None:
            if isinstance(raw, str):
                norm = self._normalise_phase_plan_path(raw)
                if norm:
                    paths.add(norm)
            elif isinstance(raw, dict):
                for key in ("path", "file_path", "file", "target"):
                    if key in raw:
                        add_path(raw[key])
            elif isinstance(raw, (list, tuple, set, frozenset)):
                for item in raw:
                    add_path(item)

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    if str(key).lower() in _PLAN_FILE_FIELD_KEYS:
                        add_path(child)
                    else:
                        visit(child)
            elif isinstance(node, (list, tuple, set, frozenset)):
                for child in node:
                    visit(child)

        visit(value)
        return {path for path in paths if path}

    @classmethod
    def _phase_plan_missing_leaf_file_fields(
        cls, subtasks: list[dict[str, Any]], plan: dict[str, Any]
    ) -> str:
        plan_text = "\n".join(cls._iter_plan_strings(plan)).lower()
        has_agent_llm = bool(
            re.search(r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|bot)\b", plan_text)
        )
        complex_plan = len(subtasks) >= 6 or (has_agent_llm and len(subtasks) >= 4)
        if not complex_plan:
            return ""
        missing: list[str] = []
        for idx, item in enumerate(subtasks, start=1):
            if cls._subtask_has_file_contract(item):
                continue
            item_id = str(
                item.get("id")
                or item.get("subtask_id")
                or item.get("title")
                or item.get("name")
                or f"item_{idx}"
            )
            missing.append(item_id)
        if not missing:
            return ""
        return (
            "latest phase plan has leaf subtask(s) without files_to_create, "
            "files_to_change, or files_affected: " + ", ".join(missing[:10])
        )

    @staticmethod
    def _subtask_has_file_contract(item: dict[str, Any]) -> bool:
        for key in _PLAN_LEAF_FILE_KEYS:
            if key not in item:
                continue
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                return True
            if isinstance(raw, dict) and any(str(value or "").strip() for value in raw.values()):
                return True
            if isinstance(raw, (list, tuple, set, frozenset)) and any(
                str(value or "").strip() for value in raw
            ):
                return True
        return False

    def _merge_persisted_plan_state(self, plan: PhasePlan) -> PhasePlan:
        """Refresh in-memory phase state after phase tools mutate phase_plan.json."""
        try:
            persisted = load_plan(self._drive_root)
        except Exception:
            return plan
        if (
            persisted is None
            or persisted.run_id != plan.run_id
            or persisted.workspace_id != plan.workspace_id
        ):
            return plan
        plan.nodes = persisted.nodes
        plan.version = persisted.version
        plan.edits_log = persisted.edits_log
        return plan

    @staticmethod
    def _plan_item_has_success_test(item: dict[str, Any]) -> bool:
        for key in (
            "success_test",
            "acceptance_command",
            "verification_command",
            "verification_commands",
            "verification",
            "test_strategy",
            "test",
        ):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                return True
            if isinstance(raw, dict) and any(str(value or "").strip() for value in raw.values()):
                return True
            if isinstance(raw, (list, tuple, set, frozenset)) and any(
                str(value or "").strip() for value in raw
            ):
                return True
        return False

    @staticmethod
    def _iter_plan_child_dicts(raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            return [item for item in raw.values() if isinstance(item, dict)]
        return []

    @classmethod
    def _execution_leaf_items_from_plan_item(
        cls, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        for key in ("subtasks", "ordered_subtasks", "steps", "phases"):
            for child in cls._iter_plan_child_dicts(item.get(key)):
                children.extend(cls._execution_leaf_items_from_plan_item(child))
        if children:
            # Phase wrappers can include summaries or test_strategy prose, but
            # only nested leaf subtasks should become executable cards.
            return children
        return [item]

    @classmethod
    def _execution_items_from_plan(cls, plan: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("subtasks", "ordered_subtasks", "steps", "phases"):
            raw = plan.get(key)
            if isinstance(raw, (list, dict)):
                items: list[dict[str, Any]] = []
                for item in cls._iter_plan_child_dicts(raw):
                    items.extend(cls._execution_leaf_items_from_plan_item(item))
                if items:
                    return items
        return []

    @staticmethod
    def _success_test_text_from_raw(raw: Any) -> str:
        if isinstance(raw, dict):
            parts: list[str] = []
            for name in (
                "value",
                "command",
                "commands",
                "cmd",
                "command_line",
                "pytest_id",
                "verification",
                "checks",
                "description",
                "text",
            ):
                value = raw.get(name)
                if isinstance(value, (dict, list, tuple, set, frozenset)):
                    text = PhaseRunner._success_test_text_from_raw(value)
                else:
                    text = str(value or "").strip()
                if text:
                    parts.append(text)
            return " ".join(part for part in parts if part)
        if isinstance(raw, (list, tuple, set, frozenset)):
            parts: list[str] = []
            for value in raw:
                if isinstance(value, dict):
                    text = PhaseRunner._success_test_text_from_raw(value)
                else:
                    text = str(value).strip()
                if text:
                    parts.append(text)
            return "; ".join(parts)
        return str(raw or "").strip()

    @staticmethod
    def _bare_success_test_tool(value: str) -> str:
        text = str(value or "").strip().strip("`").lower()
        return text if text in _GENERIC_SUCCESS_TEST_TOOLS else ""

    @classmethod
    def _success_test_raw_from_plan_item(cls, item: dict[str, Any]) -> Any:
        raw_success = item.get("success_test")
        success_text = cls._success_test_text_from_raw(raw_success)
        if success_text and cls._bare_success_test_tool(success_text):
            for key in (
                "verification_command",
                "verification_commands",
                "verification",
                "acceptance_command",
                "success_check",
                "success_checks",
                "test",
                "test_strategy",
            ):
                raw = item.get(key)
                text = cls._success_test_text_from_raw(raw)
                if text and not cls._bare_success_test_tool(text):
                    return raw
        if success_text:
            return raw_success
        for key in (
            "success_check",
            "success_checks",
            "acceptance_command",
            "verification_command",
            "verification_commands",
            "verification",
            "test_strategy",
            "test",
        ):
            raw = item.get(key)
            if cls._success_test_text_from_raw(raw):
                return raw
        return None

    @staticmethod
    def _success_test_from_plan_item(item: dict[str, Any]) -> SuccessTest:
        raw = PhaseRunner._success_test_raw_from_plan_item(item)
        if isinstance(raw, dict):
            kind = str(raw.get("kind") or "").strip() or "cmd"
            if kind not in {"cmd", "pytest_id", "check_fn", "none"}:
                kind = "cmd"
            value = (
                raw.get("value")
                or raw.get("command")
                or raw.get("commands")
                or raw.get("cmd")
                or raw.get("command_line")
                or raw.get("pytest_id")
                or raw.get("verification")
                or raw.get("checks")
                or raw.get("description")
                or raw.get("text")
                or ""
            )
            if isinstance(value, (dict, list, tuple, set, frozenset)):
                value = PhaseRunner._success_test_text_from_raw(value)
            return SuccessTest(kind=kind, value=str(value or ""))
        if isinstance(raw, (list, tuple, set, frozenset)):
            parts: list[str] = []
            for value in raw:
                if isinstance(value, dict):
                    nested = (
                        value.get("value")
                        or value.get("command")
                        or value.get("commands")
                        or value.get("cmd")
                        or value.get("command_line")
                        or value.get("pytest_id")
                        or value.get("verification")
                        or value.get("checks")
                        or value.get("description")
                        or value.get("text")
                        or value.get("name")
                        or ""
                    )
                    if isinstance(nested, (dict, list, tuple, set, frozenset)):
                        text = PhaseRunner._success_test_text_from_raw(nested)
                    else:
                        text = str(nested or "").strip()
                else:
                    text = str(value).strip()
                if text:
                    parts.append(text)
            if parts:
                return SuccessTest(kind="cmd", value="; ".join(parts))
        if isinstance(raw, str) and raw.strip():
            return SuccessTest(kind="cmd", value=raw.strip())
        return SuccessTest(kind="none", value="")

    @staticmethod
    def _success_test_automation_issue(value: str) -> str:
        raw_text = str(value or "").strip()
        text = raw_text.lower()
        if not text:
            return "success test is empty"
        if _SUCCESS_TEST_WORKSPACE_CD_RE.search(raw_text):
            return (
                "success test hard-codes a host workspace path; phase success "
                "tests run from the active workspace root, so use a workspace-"
                "relative command such as `python -m pytest ...` or "
                "`cd backend && python -m pytest ...`"
            )
        if _SUCCESS_TEST_FAILURE_MASK_RE.search(raw_text):
            return (
                "success test masks command failure with `|| true`, `|| exit 0`, "
                "or another unconditional success path; use a proof command "
                "that fails when the checked behavior is broken"
            )
        if PhaseRunner._command_quote_issue(raw_text):
            return PhaseRunner._command_quote_issue(raw_text)
        python_issue = PhaseRunner._python_inline_syntax_issue(raw_text)
        if python_issue:
            return python_issue
        python_test_issue = PhaseRunner._python_test_module_invocation_issue(raw_text)
        if python_test_issue:
            return python_test_issue
        portability_issue = PhaseRunner._command_portability_issue(raw_text)
        if portability_issue:
            return portability_issue
        localhost_issue = PhaseRunner._unmanaged_localhost_success_test_issue(raw_text)
        if localhost_issue:
            return localhost_issue
        if _JS_EMPTY_TEST_BYPASS_RE.search(raw_text):
            return (
                "success test allows an empty JavaScript test suite "
                "(`--passWithNoTests`/`--allowEmpty`); write a real checked-in "
                "test or use a build/typecheck command that fails on regressions"
            )
        if _PYTEST_COLLECT_ONLY_SUCCESS_TEST_RE.search(raw_text):
            return (
                "success test only collects pytest tests (`--collect-only`); "
                "use a real checked-in test, build, smoke, HTTP/browser proof, "
                "or verification script that executes behavior and can fail for "
                "the implemented feature"
            )
        if _PYTEST_CD_SRC_SUCCESS_TEST_RE.search(raw_text):
            return (
                "success test changes into source root `src` before running "
                "pytest; greenfield tests must live under workspace-level "
                "`tests/`, so run `python -m pytest tests/test_x.py -q` from "
                "the workspace root instead of `cd src && ...`"
            )
        if _FILE_EXISTENCE_ONLY_SUCCESS_TEST_RE.search(
            raw_text
        ) and not _BEHAVIORAL_SUCCESS_TEST_RE.search(raw_text):
            return (
                "success test only checks file/path existence; move file "
                "presence into acceptance criteria and use a checked-in "
                "unit/integration test, build command, HTTP/browser proof, "
                "or verification script that exercises behavior"
            )
        shell_segment_issue = PhaseRunner._shell_command_segment_issue(raw_text)
        if shell_segment_issue:
            return shell_segment_issue
        if _GENERIC_SUCCESS_TOOL_WITH_ARGS_RE.search(raw_text):
            return (
                "success test uses a generic Umbrella tool name with "
                "pseudo-arguments; use the bare tool only for final gates or "
                "write the exact underlying command such as "
                "`python -m pytest ... -q`, `npm test`, or a checked-in "
                "verification script"
            )
        if _DESCRIPTIVE_SUCCESS_TEST_RE.search(raw_text):
            return (
                "success test mixes an executable with descriptive acceptance "
                "text; move the prose into goal/acceptance_criteria and leave "
                "success_test as one exact command or tool target"
            )
        human_required = re.search(
            r"\b(user reports?|human reports?|ask the user|by hand)\b", text
        )
        manual_required = re.search(
            r"\b(manual|manually|manual smoke|visual inspection|visually inspect)\b",
            text,
        )
        if human_required or manual_required:
            return (
                "success test depends on a human/manual report; replace it with "
                "an agent-run command, HTTP/browser automation, or verification tool"
            )
        if _DESCRIPTIVE_BROWSER_SUCCESS_TEST_RE.search(
            raw_text
        ) and not _CONCRETE_BROWSER_AUTOMATION_RE.search(raw_text):
            return (
                "success test describes browser/user observation instead of an "
                "automated proof; use a concrete command/tool such as "
                "`npx playwright test`, `python -m pytest ...`, `run_real_e2e`, "
                "`http_boot`, or `behavioral_http`"
            )
        if not _SUCCESS_TEST_AUTOMATION_RE.search(text):
            return (
                "success test is not an executable proof; use an exact command, "
                "`run_workspace_verify`, `run_unit_tests`, `harness_run`, "
                "`http_boot`/`behavioral_http`, or browser automation"
            )
        if _SUCCESS_TEST_VAGUE_RE.search(text) and not re.search(
            r"\b(run_workspace_verify|run_unit_tests|harness_run|http_boot|"
            r"behavioral_http|pytest|python\s+-m\s+pytest|npm\s+(run\s+)?test|"
            r"npm\s+run\s+build|pnpm|yarn|playwright|browser)\b",
            text,
        ):
            return (
                "success test is too vague; include the concrete command or "
                "automation target that produces pass/fail evidence"
            )
        return ""

    @staticmethod
    def _success_test_shape_issue(raw: Any) -> str:
        if isinstance(raw, (list, tuple, set, frozenset)):
            return (
                "success_test must be a single executable string/object, not a "
                "list of commands; split the work into separate subtasks or "
                "call a checked-in test script"
            )
        if isinstance(raw, dict):
            text = PhaseRunner._success_test_text_from_raw(raw)
            if re.match(r"^\s*-\w", text):
                return (
                    "success_test command is missing an executable; include "
                    "the full command such as `python -m pytest ...` instead "
                    "of an option-only command like `-m pytest ...`"
                )
        return ""

    @staticmethod
    def _success_test_alias_shape_issue(item: dict[str, Any]) -> str:
        if not isinstance(item, dict):
            return ""
        if "success_test" in item:
            return ""
        for key in _SUCCESS_TEST_ALIAS_KEYS:
            if key not in item:
                continue
            raw = item.get(key)
            if isinstance(raw, (list, tuple, set, frozenset)):
                return (
                    f"`{key}` is a list; phase plans must put one exact "
                    "executable command/object in top-level `success_test`. "
                    "Split multiple checks into separate subtasks or call a "
                    "checked-in verification script."
                )
            if isinstance(raw, str) and re.match(
                r"(?i)^\s*(?:run|verify|check|assert)\s*:", raw
            ):
                return (
                    f"`{key}` is descriptive text with a prefix; put only the "
                    "exact command/tool target in top-level `success_test`, "
                    "without `Run:`, `Verify:`, `Check:`, or `Assert:` prose."
                )
        return ""

    @staticmethod
    def _command_quote_issue(value: str) -> str:
        text = str(value or "")
        escaped = False
        double_quotes = 0
        for ch in text:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                double_quotes += 1
        if double_quotes % 2:
            return (
                "success test command has unbalanced double quotes; provide a "
                "valid executable command"
            )
        return ""

    @staticmethod
    def _iter_python_inline_snippets(value: str) -> list[str]:
        text = str(value or "")
        snippets: list[str] = []
        pos = 0
        pattern = re.compile(r"(?i)\b(?:python|py)(?:\.exe)?\s+-c\s*([\"'])")
        while True:
            match = pattern.search(text, pos)
            if not match:
                return snippets
            quote = match.group(1)
            idx = match.end()
            escaped = False
            chars: list[str] = []
            while idx < len(text):
                ch = text[idx]
                if escaped:
                    chars.append("\\" + ch)
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    break
                else:
                    chars.append(ch)
                idx += 1
            if idx >= len(text):
                return snippets
            snippet = "".join(chars).replace(r"\"", '"').replace(r"\'", "'")
            snippets.append(snippet)
            pos = idx + 1

    @staticmethod
    def _python_inline_import_only_issue(tree: ast.AST) -> str:
        if not isinstance(tree, ast.Module):
            return ""
        if not tree.body:
            return ""

        def is_print_expr(stmt: ast.stmt) -> bool:
            if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
                return False
            func = stmt.value.func
            return isinstance(func, ast.Name) and func.id == "print"

        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            if is_print_expr(stmt):
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue
            return ""
        return (
            "success test `python -c` only imports modules and/or prints text; "
            "use assertions, instantiate/call the behavior, or run a real "
            "test command so the subtask proof can fail when the "
            "implementation is wrong"
        )

    _PYTHON_INLINE_ALLOWED_IMPORT_ROOTS = frozenset(
        getattr(sys, "stdlib_module_names", frozenset())
    ) | {"__future__"}

    @staticmethod
    def _python_inline_workspace_import_issue(tree: ast.AST) -> str:
        if not isinstance(tree, ast.Module):
            return ""
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    return (
                        "success test `python -c` imports workspace/application "
                        "modules; put this behavioral check in a checked-in "
                        "pytest/node/browser test or verification script"
                    )
                module = str(node.module or "").strip()
                if module:
                    roots.append(module.split(".", 1)[0])
            for root in roots:
                if root and root not in PhaseRunner._PYTHON_INLINE_ALLOWED_IMPORT_ROOTS:
                    return (
                        "success test `python -c` imports workspace/application "
                        "modules; put this behavioral check in a checked-in "
                        "pytest/node/browser test or verification script"
                    )
        return ""

    def _python_inline_complexity_issue(snippet: str) -> str:
        text = str(snippet or "").strip()
        if (
            "\n" in text
            or "\r" in text
            or len(text) > 280
            or text.count(";") > 5
            or re.search(r"\b(subprocess|time\.sleep|requests|urllib\.request)\b", text)
        ):
            return (
                "success test `python -c` is too complex for reliable "
                "phase-plan verification; put the behavior in a checked-in "
                "pytest/node/browser test or verification script and use that "
                "command as success_test"
            )
        return ""

    @staticmethod
    def _python_inline_syntax_issue(value: str) -> str:
        for snippet in PhaseRunner._iter_python_inline_snippets(value):
            try:
                tree = ast.parse(snippet)
            except SyntaxError as exc:
                detail = exc.msg or "invalid syntax"
                return (
                    "success test contains invalid `python -c` code "
                    f"({detail}); provide a syntactically valid command"
                )
            lowered = snippet.lower()
            has_failure_print = bool(
                re.search(r"else\s+['\"](?:fail|failed|error)['\"]", lowered)
            )
            has_real_failure = any(
                token in lowered for token in ("assert ", "raise ", "sys.exit")
            )
            if has_failure_print and not has_real_failure:
                return (
                    "success test `python -c` only prints FAIL/ERROR while "
                    "still exiting successfully; use assert, raise, sys.exit, "
                    "or a real test command so failure changes the exit code"
                )
            import_only_issue = PhaseRunner._python_inline_import_only_issue(tree)
            if import_only_issue:
                return import_only_issue
            docs_content_issue = _python_inline_docs_content_issue(tree)
            if docs_content_issue:
                return docs_content_issue
            complexity_issue = PhaseRunner._python_inline_complexity_issue(snippet)
            if complexity_issue:
                return complexity_issue
            workspace_import_issue = PhaseRunner._python_inline_workspace_import_issue(tree)
            if workspace_import_issue:
                return workspace_import_issue
        return ""

    _DIRECT_PYTHON_TEST_MODULE_RE = re.compile(
        r"(?ix)"
        r"(?<![\w.-])"
        r"(?:python|py)(?:\.exe)?\s+"
        r"(?!-m\s+pytest\b)"
        r"(?:-[A-Za-z]\s+)*"
        r"(?P<target>[^\s;&|]*?(?:^|[\\/])?(?:test_[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+_test)\.py)"
        r"(?=$|\s|::|[;&|])"
    )

    @classmethod
    def _python_test_module_invocation_issue(cls, value: str) -> str:
        match = cls._DIRECT_PYTHON_TEST_MODULE_RE.search(str(value or ""))
        if not match:
            return ""
        target = match.group("target") or "test module"
        return (
            f"success test invokes pytest module `{target}` with `python ...`; "
            "run test modules through `python -m pytest <path>[::test] -q` or "
            "`pytest <path>[::test] -q`, or use a checked-in verification script "
            "whose name is not a pytest test module"
        )

    @staticmethod
    def _shell_command_segment_issue(value: str) -> str:
        for segment in PhaseRunner._split_shell_command_segments(str(value or "")):
            stripped = segment.strip()
            if not stripped:
                continue
            if re.match(r"(?i)^assert\b", stripped):
                return (
                    "success test contains a bare Python `assert` as a shell "
                    "command; put assertions in `python -c \"assert ...\"`, "
                    "a checked-in pytest, or a verification script"
                )
            if re.match(
                r"(?i)^(?:os\.path\.exists|fs\.existsSync|pathlib\.Path\(|Path\()",
                stripped,
            ):
                return (
                    "success test contains a bare file-existence expression as "
                    "a shell command; put it in a real pytest/python/node "
                    "assertion or use a verification script"
            )
        return ""

    @staticmethod
    def _split_shell_command_segments(value: str) -> list[str]:
        segments: list[str] = []
        current: list[str] = []
        quote: str | None = None
        escaped = False
        index = 0
        text = str(value or "")
        while index < len(text):
            ch = text[index]
            if escaped:
                current.append(ch)
                escaped = False
                index += 1
                continue
            if ch == "\\":
                current.append(ch)
                escaped = True
                index += 1
                continue
            if quote:
                current.append(ch)
                if ch == quote:
                    quote = None
                index += 1
                continue
            if ch in {"'", '"'}:
                quote = ch
                current.append(ch)
                index += 1
                continue
            if text.startswith("&&", index) or text.startswith("||", index):
                segments.append("".join(current))
                current = []
                index += 2
                continue
            if ch == ";":
                segments.append("".join(current))
                current = []
                index += 1
                continue
            current.append(ch)
            index += 1
        segments.append("".join(current))
        return segments

    _NON_PORTABLE_SHELL_RE = re.compile(
        r"(?ix)("
        r"\b(?:bash|sh)\s+[^\s;&|]+\.sh\b|"
        r"(?:^|[;&|]\s*)test\s+-[efs]\b|"
        r"\bps\s+aux\b|"
        r"\bpkill\b|"
        r"\breadlink\b|"
        r"\bexit\s+(?:\$\?|\d+\b)|"
        r"(?:^|[;&|]\s*)if\s+\[|"
        r";\s*(?:then|else|fi)\b|"
        r"\bStart-Job\b|"
        r"\bgrep\b|"
        r"\bsed\b|"
        r"\bawk\b|"
        r"\bhead\s+-\d+\b|"
        r"\btail\s+-\d+\b|"
        r"\|\s*grep\b"
        r")"
    )

    @staticmethod
    def _has_background_shell_operator(value: str) -> bool:
        text = str(value or "")
        for idx, ch in enumerate(text):
            if ch != "&":
                continue
            prev_ch = text[idx - 1] if idx > 0 else ""
            next_ch = text[idx + 1] if idx + 1 < len(text) else ""
            if prev_ch == "&" or next_ch == "&":
                continue
            return True
        return False

    @classmethod
    def _command_portability_issue(cls, value: str) -> str:
        text = str(value or "")
        if cls._NON_PORTABLE_SHELL_RE.search(text) or cls._has_background_shell_operator(text):
            return (
                "success test uses non-portable or unmanaged "
                "shell/process-control syntax that is not a reliable Umbrella "
                "workspace proof on this host; use Python/pytest/node/npm, "
                "a checked-in verification script, or a managed HTTP/browser "
                "verification gate that starts and stops services cleanly"
            )
        return ""

    @classmethod
    def _unmanaged_localhost_success_test_issue(cls, value: str) -> str:
        for segment in cls._split_shell_command_segments(str(value or "")):
            stripped = segment.strip()
            if not _DIRECT_LOCALHOST_HTTP_RE.search(stripped):
                continue
            if _MANAGED_LOCALHOST_PROOF_RE.search(stripped):
                continue
            return (
                "success test probes localhost with a direct HTTP shell command "
                "(`curl`/`Invoke-WebRequest`) without a managed server harness "
                "in that same proof step; use a checked-in pytest/playwright/e2e "
                "harness or Umbrella `http_boot`/`behavioral_http` so the proof "
                "starts and stops services instead of depending on a "
                "pre-existing listener"
            )
        return ""

    @classmethod
    def _success_test_issue_from_plan_item(cls, item: dict[str, Any]) -> str:
        if item.get("_depth_limit") is True:
            return (
                "depth-limit placeholder is not an executable subtask; provide "
                "the real leaf object with id, title, goal, files, and success_test"
            )
        shape_issue = cls._success_test_alias_shape_issue(item)
        if not shape_issue:
            shape_issue = cls._success_test_shape_issue(item.get("success_test"))
        if shape_issue:
            return shape_issue
        success = cls._success_test_from_plan_item(item)
        if not success.value:
            return "missing success test"
        automation_issue = cls._success_test_automation_issue(success.value)
        if automation_issue:
            return automation_issue
        mock_issue = cls._llm_mock_success_test_issue(item, success.value)
        if mock_issue:
            return mock_issue
        return cls._llm_error_as_success_issue(item, success.value)

    @classmethod
    def _llm_mock_success_test_issue(cls, item: dict[str, Any], success_text: str) -> str:
        if not _LLM_MOCK_SUCCESS_TEST_RE.search(success_text):
            return ""
        context = cls._plan_item_non_success_context_text(item)
        if not _MOCKED_PROOF_WORK_ITEM_CONTEXT_RE.search(context):
            return ""
        return (
            "success test uses a mocked path for an LLM/e2e/integration proof; "
            "required behavior must be proved with the inherited real runtime env "
            "or fail/skip explicitly when that env is absent"
        )

    _LLM_ERROR_AS_SUCCESS_RE = re.compile(
        r"(?is)\bassert\b.{0,300}(?:\bor\b|\|\|).{0,300}"
        r"\b(?:error_llm|llm_error|error|exception|failed)\b|"
        r"\b(?:error_llm|llm_error|error|exception|failed)\b.{0,300}"
        r"(?:\bor\b|\|\|).{0,300}\bassert\b"
    )
    _LLM_ERROR_PROTECTIVE_RE = re.compile(
        r"(?is)\b(?:not|never|without|forbid(?:s|den)?|reject(?:s|ed)?|"
        r"fail(?:s|ed)?\s+if)\b.{0,80}"
        r"\b(?:error_llm|llm_error|error|exception|failed)\b|"
        r"\b(?:error_llm|llm_error|error|exception|failed)\b.{0,80}"
        r"\b(?:not|forbidden|rejected|disallowed|absent)\b"
    )

    @classmethod
    def _plan_item_non_success_context_text(cls, item: dict[str, Any]) -> str:
        context_parts: list[str] = []
        for key, value in item.items():
            if str(key).strip().lower() in _PLAN_SUCCESS_TEST_KEYS:
                continue
            context_parts.extend(cls._iter_plan_strings(value))
        return "\n".join(context_parts)

    @classmethod
    def _plan_item_has_llm_context(cls, item: dict[str, Any]) -> bool:
        return bool(_LLM_WORK_ITEM_CONTEXT_RE.search(cls._plan_item_non_success_context_text(item)))

    @classmethod
    def _llm_error_as_success_issue(cls, item: dict[str, Any], success_text: str) -> str:
        if not cls._plan_item_has_llm_context(item):
            return ""
        if not cls._LLM_ERROR_AS_SUCCESS_RE.search(success_text):
            return ""
        if cls._LLM_ERROR_PROTECTIVE_RE.search(success_text):
            return ""
        return (
            "success test treats an LLM/GMAS error path as a passing outcome; "
            "with the inherited real runtime env, the proof must require a "
            "successful real LLM decision and reserve explicit error/skip "
            "behavior only for missing or failing configuration"
        )

    @classmethod
    def _coerce_embedded_phase_plan(
        cls, plan: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        embedded = plan.get("plan")
        if isinstance(embedded, dict) and not cls._execution_items_from_plan(plan):
            return embedded, ""
        if isinstance(embedded, str):
            try:
                parsed = json.loads(embedded)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and cls._execution_items_from_plan(parsed):
                return parsed, ""
            if not cls._execution_items_from_plan(plan):
                return (
                    plan,
                    "latest phase plan embeds serialized text in `plan.plan`; "
                    "the artifact must contain a plan object with executable "
                    "subtasks/steps/phases, not a string or truncated digest",
                )
        return plan, ""

    @classmethod
    def _phase_plan_generic_success_test_failure(
        cls,
        subtasks: list[dict[str, Any]],
    ) -> str:
        if not subtasks:
            return ""
        generic_ids: list[str] = []
        inappropriate: list[str] = []
        for idx, item in enumerate(subtasks, start=1):
            text = cls._success_test_from_plan_item(item).value
            tool = cls._bare_success_test_tool(text)
            if not tool:
                continue
            subtask_id = str(
                item.get("id")
                or item.get("subtask_id")
                or item.get("title")
                or item.get("name")
                or f"subtask_{idx}"
            )
            generic_ids.append(subtask_id)
            if tool in {"run_workspace_verify", "run_unit_tests"}:
                inappropriate.append(subtask_id)
        if inappropriate:
            return (
                "latest phase plan uses bare `run_workspace_verify`/`run_unit_tests` "
                "instead of a concrete local proof or smoke/e2e command before the "
                "workspace-level gate: "
                + ", ".join(inappropriate[:8])
            )
        if len(subtasks) >= 6 and len(generic_ids) > max(2, len(subtasks) // 2):
            return (
                "latest phase plan overuses bare verification tool names as "
                "success tests; use concrete per-subtask commands and reserve "
                "`run_workspace_verify` for final/integration gates"
            )
        return ""

    @classmethod
    def _js_test_file_targets_from_success_test(cls, value: str) -> list[str]:
        targets: list[str] = []
        for segment in cls._split_shell_command_segments(str(value or "")):
            match = _JS_TEST_COMMAND_SEGMENT_RE.search(segment)
            if not match:
                continue
            if not re.search(r"(?i)\b(?:test|vitest|jest)\b", segment):
                continue
            for token in re.split(r"\s+", match.group("args").strip()):
                cleaned = token.strip().strip("`'\"()[]{}.,;").replace("\\", "/")
                if not cleaned or cleaned.startswith("-"):
                    continue
                cleaned = cleaned.split("::", 1)[0].lstrip("./")
                if _JS_TEST_FILE_TOKEN_RE.search(cleaned):
                    targets.append(cleaned)
        return list(dict.fromkeys(targets))

    @classmethod
    def _iter_declared_plan_path_strings(cls, value: Any) -> Iterator[str]:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in _PLAN_FILE_FIELD_KEYS:
                    yield from cls._iter_declared_plan_path_strings(child)
                else:
                    yield from cls._iter_declared_plan_path_strings(child)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                yield from cls._iter_declared_plan_path_strings(item)
        elif isinstance(value, str):
            text = value.strip().strip("`'\"").replace("\\", "/").lstrip("./")
            if text:
                yield text

    def _phase_plan_frontend_test_target_failure(
        self, subtasks: list[dict[str, Any]], plan: dict[str, Any]
    ) -> str:
        declared_paths = set(self._iter_declared_plan_path_strings(plan))
        declared_by_name: dict[str, set[str]] = {}
        for path in declared_paths:
            if re.search(r"(?i)\.(?:test|spec)\.(?:[cm]?[jt]sx?)$", path):
                declared_by_name.setdefault(path.rsplit("/", 1)[-1], set()).add(path)
        workspace_root = self._repo_root / "workspaces" / self._workspace_id
        for idx, item in enumerate(subtasks, start=1):
            success_text = self._success_test_from_plan_item(item).value
            if not _FRONTEND_TEST_CWD_RE.search(success_text):
                continue
            targets = self._js_test_file_targets_from_success_test(success_text)
            if not targets:
                continue
            subtask_id = str(
                item.get("id")
                or item.get("subtask_id")
                or item.get("title")
                or item.get("name")
                or f"subtask_{idx}"
            )
            for target in targets:
                normalized = target.replace("\\", "/").lstrip("./")
                expected = ""
                if normalized.startswith("frontend/"):
                    expected = normalized
                elif "/" in normalized:
                    expected = f"frontend/{normalized}"
                if expected and expected in declared_paths:
                    continue
                if expected and (workspace_root / expected).is_file():
                    continue
                basename = normalized.rsplit("/", 1)[-1]
                matches = declared_by_name.get(basename, set())
                if any(path.startswith("frontend/") for path in matches):
                    continue
                if "/" not in normalized and list((workspace_root / "frontend").rglob(basename)):
                    continue
                if matches:
                    return (
                        f"latest phase plan subtask `{subtask_id}` runs frontend "
                        f"tests from `cd frontend` with target `{target}`, but "
                        "declares the matching test outside the frontend package: "
                        + ", ".join(sorted(matches)[:4])
                    )
                return (
                    f"latest phase plan subtask `{subtask_id}` runs frontend "
                    f"test target `{target}` from `cd frontend`, but no plan "
                    "leaf declares that checked-in frontend test file"
                )
        return ""

    def _workspace_has_frontend_script_source(self) -> bool:
        src = self._repo_root / "workspaces" / self._workspace_id / "frontend" / "src"
        if not src.is_dir():
            return False
        for pattern in ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mts", "*.cts"):
            if list(src.rglob(pattern)):
                return True
        return False

    def _workspace_has_vite_config(self) -> bool:
        frontend = self._repo_root / "workspaces" / self._workspace_id / "frontend"
        return frontend.is_dir() and any(frontend.glob("vite.config.*"))

    def _phase_plan_frontend_build_order_failure(
        self, subtasks: list[dict[str, Any]], plan: dict[str, Any]
    ) -> str:
        workspace_root = self._repo_root / "workspaces" / self._workspace_id
        cumulative_paths = {
            path
            for path in self._iter_declared_plan_path_strings(plan)
            if (workspace_root / path).is_file()
        }
        for idx, item in enumerate(subtasks, start=1):
            declared_now = set(self._iter_declared_plan_path_strings(item))
            success_text = self._success_test_from_plan_item(item).value
            available = cumulative_paths | declared_now
            if not _FRONTEND_BUILD_COMMAND_RE.search(success_text):
                cumulative_paths.update(declared_now)
                continue
            missing: list[str] = []
            if not any(
                _FRONTEND_SCRIPT_SOURCE_RE.search(path) for path in available
            ) and not self._workspace_has_frontend_script_source():
                missing.append("frontend/src/<entry>.tsx")
            vite_declared = any(
                _FRONTEND_VITE_CONFIG_RE.search(path) for path in available
            ) or self._workspace_has_vite_config()
            if (
                vite_declared
                and "frontend/index.html" not in available
                and not (workspace_root / "frontend/index.html").is_file()
            ):
                missing.append("frontend/index.html")
            if missing:
                subtask_id = str(
                    item.get("id")
                    or item.get("subtask_id")
                    or item.get("title")
                    or item.get("name")
                    or f"subtask_{idx}"
                )
                return (
                    f"latest phase plan subtask `{subtask_id}` runs a frontend "
                    "build success_test before the files needed by that build "
                    "are declared in the same or an earlier leaf: "
                    + ", ".join(missing)
                    + ". Move the build success_test to the leaf that owns the "
                    "entrypoint, or declare the entrypoint files on this leaf."
                )
            cumulative_paths.update(declared_now)
        return ""

    @staticmethod
    def _iter_plan_strings(value: Any) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from PhaseRunner._iter_plan_strings(child)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                yield from PhaseRunner._iter_plan_strings(child)

    _PLAN_LLM_FALLBACK_RE = re.compile(
        r"(?is)("
        r"\b(?:llm|gmas|agent|bot|model)\b.{0,240}\b(?:fallback|fall[-\s]+back)\b"
        r".{0,160}\b(?:heuristic|deterministic|static|hardcoded|mock|random|"
        r"rule[-\s]?based|default|valid\s+action|cached\s+decisions?|"
        r"cached\s+actions?|graceful\s+degradation)\b|"
        r"\b(?:llm|gmas|agent|bot|model)\b.{0,240}\b(?:heuristic|"
        r"deterministic|static|hardcoded|mock|random|rule[-\s]?based|"
        r"cached\s+decisions?|cached\s+actions?|graceful\s+degradation)\s+"
        r"(?:(?:ai|bot|agent|model|llm|gmas)\s+)?"
        r"(?:fallback|replacement|decision|action)\b|"
        r"\b(?:fallback|fall[-\s]+back)\b.{0,160}\b(?:heuristic|deterministic|"
        r"static|hardcoded|mock|random|rule[-\s]?based|default|valid\s+action|"
        r"cached\s+decisions?|cached\s+actions?|graceful\s+degradation)"
        r"\b.{0,240}\b(?:llm|gmas|agent|bot|model)\b"
        r")"
    )

    @classmethod
    def _phase_plan_content_issue(cls, plan: dict[str, Any]) -> str:
        def has_depth_limit(value: Any) -> bool:
            if isinstance(value, dict):
                if value.get("_depth_limit") is True:
                    return True
                return any(has_depth_limit(child) for child in value.values())
            if isinstance(value, list):
                return any(has_depth_limit(child) for child in value)
            return False

        if has_depth_limit(plan):
            return (
                "latest phase plan contains depth-limit placeholder subtasks; "
                "provide real executable leaf subtasks before execute"
            )
        for value in cls._iter_plan_llm_fallback_contexts(plan):
            matches = list(cls._PLAN_LLM_FALLBACK_RE.finditer(value))
            first_unprotected = ""
            for match in matches:
                if cls._llm_fallback_match_is_protective(match.group(0)) or (
                    cls._llm_fallback_match_is_protective(value)
                ):
                    continue
                first_unprotected = " ".join(match.group(0).split())
                break
            if first_unprotected:
                return (
                    "latest phase plan proposes deterministic/static/heuristic "
                    "fallback for required LLM behavior; use real GMAS/LLM behavior "
                    "and make LLM failure explicit, retried, paused, or surfaced as "
                    "an error instead of hardcoded replacement logic. Matched text: "
                    f"`{first_unprotected[:220]}`"
                )
            cached_match = _PLAN_LLM_CACHED_DECISION_RE.search(value)
            if (
                cached_match
                and _LLM_WORK_ITEM_CONTEXT_RE.search(value)
                and not cls._llm_cached_decision_match_is_protective(value)
            ):
                first_unprotected = " ".join(cached_match.group(0).split())
                return (
                    "latest phase plan proposes cached decision/action/reasoning reuse for "
                    "LLM/GMAS/bot behavior; required bot decisions must use the "
                    "inherited real LLM runtime, with retry, pause, or surfaced "
                    "errors instead of cached replacement decisions. Matched text: "
                    f"`{first_unprotected[:220]}`"
                )
            if (
                _LLM_WORK_ITEM_CONTEXT_RE.search(value)
                and _PLAN_GENERIC_FALLBACK_RE.search(value)
                and not _PLAN_BAD_FALLBACK_REPLACEMENT_RE.search(value)
                and not cls._plan_string_is_identifier_like(value)
                and not cls._llm_fallback_match_is_protective(value)
            ):
                return (
                    "latest phase plan describes generic fallback logic for "
                    "LLM/GMAS/bot behavior; use explicit retry, paused bot turn, "
                    "surfaced runtime/startup error, or configuration requirement "
                    "instead of vague fallback handling. Matched text: "
                    f"`{' '.join(value.split())[:220]}`"
                )
        test_double_issue = cls._phase_plan_llm_test_double_issue(plan)
        if test_double_issue:
            return test_double_issue
        env_issue = cls._phase_plan_llm_env_issue(plan)
        if env_issue:
            return env_issue
        provider_default_issue = cls._phase_plan_llm_provider_default_issue(plan)
        if provider_default_issue:
            return provider_default_issue
        empty_test_issue = cls._phase_plan_empty_test_skeleton_issue(plan)
        if empty_test_issue:
            return empty_test_issue
        return ""

    @staticmethod
    def _phase_plan_llm_test_double_issue(plan: dict[str, Any]) -> str:
        for value in PhaseRunner._iter_plan_non_success_strings(plan):
            if not _PLAN_LLM_TEST_DOUBLE_RE.search(value):
                continue
            lowered = str(value or "").lower()
            protective = bool(
                re.search(
                    r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
                    r"disallow(?:s|ed)?|reject(?:s|ed)?|prevent(?:s|ed)?)\b"
                    r".{0,100}\b(?:mock|fake|dry[-\s]?run|test\s+double)\b",
                    lowered,
                )
                or re.search(
                    r"\b(detect|detects|verification|verify|assert|"
                    r"enforce|prevent|reject)\b.{0,140}\b(?:mock|fake|"
                    r"dry[-\s]?run|test\s+double)\b",
                    lowered,
                )
            )
            if protective:
                continue
            return (
                "latest phase plan proposes mock/fake/dry-run LLM behavior for "
                "an LLM/GMAS/bot/model path; required LLM behavior must be proved "
                "with the inherited real runtime env, while missing credentials "
                "should fail, skip explicitly, pause, retry, or surface an error"
            )
        return ""

    @staticmethod
    def _iter_plan_non_success_strings(value: Any) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, child in value.items():
                if str(key).strip().lower() in {
                    "success_test",
                    "acceptance_command",
                    "verification_command",
                    "verification_commands",
                    "verification",
                    "test",
                    "anti_patterns",
                    "anti_patterns_to_avoid",
                    "forbidden_patterns",
                    "avoid",
                }:
                    continue
                yield from PhaseRunner._iter_plan_non_success_strings(child)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                yield from PhaseRunner._iter_plan_non_success_strings(child)

    @staticmethod
    def _phase_plan_llm_env_issue(plan: dict[str, Any]) -> str:
        text = "\n".join(PhaseRunner._iter_plan_strings(plan))
        if not _LLM_WORK_ITEM_CONTEXT_RE.search(text):
            return ""
        unsupported_alias = next(_UNSUPPORTED_OUROBOROS_MODEL_ALIAS_RE.finditer(text), None)
        if unsupported_alias:
            return (
                "latest phase plan uses unsupported model env alias "
                "`OUROBOROS_LLM_MODEL`; generated projects should expose "
                "`LLM_MODEL` as their public model setting and may accept "
                "`OUROBOROS_MODEL` only as an inherited Umbrella compatibility alias"
            )
        invalid_alias_issues = unsupported_llm_env_alias_issues(
            text,
            subject="latest phase plan",
            exclude_aliases={"OUROBOROS_LLM_MODEL"},
        )
        if invalid_alias_issues:
            return invalid_alias_issues[0].message
        has_ouroboros_alias = bool(_LLM_ENV_ALIAS_RE.search(text))
        legacy_mentions = _LLM_LEGACY_ENV_RE.findall(text)
        openai_required = bool(_OPENAI_REQUIRED_RE.search(text))
        openai_mentions = bool(_OPENAI_KEY_RE.search(text))
        web_search_only = bool(_WEB_SEARCH_ONLY_CONTEXT_RE.search(text)) and not legacy_mentions
        missing_aliases = _missing_llm_runtime_aliases(text)
        has_any_runtime_alias = len(missing_aliases) < len(_LLM_LEGACY_ENV_ALIASES)
        if has_any_runtime_alias and not missing_aliases:
            return ""
        if (
            has_any_runtime_alias
            or legacy_mentions
            or (openai_mentions and not web_search_only)
            or openai_required
        ):
            missing_text = (
                "; missing aliases: "
                + ", ".join(f"`{alias}`" for alias in missing_aliases)
                if missing_aliases
                else ""
            )
            return (
                "latest phase plan uses an LLM credential contract that is too "
                "narrow for a standalone generated project; generated workspace "
                "code/tests must support public aliases `LLM_API_KEY`, "
                "`LLM_BASE_URL`, and `LLM_MODEL`, and may also accept inherited "
                "Umbrella aliases `OUROBOROS_LLM_API_KEY`, "
                "`OUROBOROS_LLM_BASE_URL`, and `OUROBOROS_MODEL`, with "
                "`OPENAI_API_KEY` treated only as a provider/web-search-specific "
                "credential"
                f"{missing_text}"
            )
        if not (
            _LLM_ENV_OMISSION_REQUIRED_RE.search(text)
            or _LLM_ENV_CONTRACT_REQUIRED_RE.search(text)
        ):
            return ""
        return (
            "latest phase plan omits the standalone LLM runtime env "
            "contract for LLM/GMAS/bot work; generated workspace code/tests "
            "must explicitly resolve public aliases `LLM_API_KEY`, "
            "`LLM_BASE_URL`, and `LLM_MODEL`, optionally with inherited "
            "Umbrella compatibility aliases, and fail/skip/pause clearly when "
            "real LLM credentials are absent"
        )

    @staticmethod
    def _phase_plan_llm_provider_default_issue(plan: dict[str, Any]) -> str:
        text = "\n".join(PhaseRunner._iter_plan_strings(plan))
        if not _LLM_WORK_ITEM_CONTEXT_RE.search(text):
            return ""
        for match in _LLM_PROVIDER_DEFAULT_PLAN_RE.finditer(text):
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 80)
            if re.search(
                r"(?i)\b(no|not|never|without|avoid|reject|forbid(?:s|den)?|"
                r"disallow(?:s|ed)?|do\s+not)\b",
                text[start:end],
            ):
                continue
            return (
                "latest phase plan hardcodes provider/model-specific LLM "
                "defaults; use `OUROBOROS_MODEL`/`LLM_MODEL` and env-driven "
                "provider/base URL instead of `gpt-*` or OpenAI URLs"
            )
        return ""

    @staticmethod
    def _phase_plan_empty_test_skeleton_issue(plan: dict[str, Any]) -> str:
        first_unprotected: str | None = None
        for text in PhaseRunner._iter_plan_strings(plan):
            if PhaseRunner._plan_string_is_identifier_like(text):
                continue
            for match in _EMPTY_TEST_SKELETON_RE.finditer(text):
                start = max(0, match.start() - 100)
                end = min(len(text), match.end() + 100)
                context = text[start:end]
                direct_start = max(0, match.start() - 80)
                direct_end = min(len(text), match.end() + 40)
                if _EMPTY_TEST_DIRECT_PROTECTIVE_RE.search(text[direct_start:direct_end]):
                    continue
                if _EMPTY_TEST_PROTECTIVE_RE.search(context) and (
                    _EMPTY_TEST_BEHAVIORAL_PROOF_RE.search(context)
                    or _EMPTY_TEST_PROTECTIVE_RE.search(match.group(0))
                ):
                    continue
                first_unprotected = " ".join(match.group(0).split())
                break
            if first_unprotected is not None:
                break
        if first_unprotected is None:
            return ""
        return (
            "latest phase plan asks for empty/basic-import test skeletons; "
            "tests must contain executable assertions or fixtures that can fail "
            "for real behavior. Matched text: "
            f"`{first_unprotected}`"
        )

    @staticmethod
    def _iter_plan_llm_fallback_contexts(value: Any) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            direct_strings = []
            for key, child in value.items():
                if not isinstance(child, str):
                    continue
                child_text = str(child).strip()
                if not child_text:
                    continue
                key_text = str(key).strip().replace("_", " ").replace("-", " ")
                direct_strings.append(
                    f"{key_text}: {child_text}" if key_text else child_text
                )
            if direct_strings and PhaseRunner._should_combine_llm_fallback_context(value):
                yield "\n".join(direct_strings)
            for child in value.values():
                yield from PhaseRunner._iter_plan_llm_fallback_contexts(child)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                yield from PhaseRunner._iter_plan_llm_fallback_contexts(child)

    @staticmethod
    def _should_combine_llm_fallback_context(value: dict[str, Any]) -> bool:
        keys = {str(key).strip().lower() for key in value}
        if keys & {"subtasks", "steps", "phases", "ordered_subtasks"}:
            return False
        normalized_key_text = "\n".join(
            key.replace("_", " ").replace("-", " ") for key in keys
        )
        if _LLM_WORK_ITEM_CONTEXT_RE.search(normalized_key_text):
            return True
        if keys & {
            "risk",
            "risks",
            "mitigation",
            "risk_mitigation",
            "risk_mitigations",
            "risks_and_mitigations",
        }:
            return True
        if keys & {
            "decision_policy",
            "decision_policies",
            "failure_policy",
            "failure_policies",
            "error_handling",
            "acceptance_criteria",
            "llm_policy",
            "policies",
            "runtime_policy",
            "runtime_policies",
        }:
            return True
        return False

    @staticmethod
    def _plan_string_is_identifier_like(value: str) -> bool:
        text = str(value or "").strip()
        return bool(re.fullmatch(r"[A-Za-z0-9_.:/\\-]{1,160}", text))

    @staticmethod
    def _llm_cached_decision_match_is_protective(text: str) -> bool:
        lowered = str(text or "").lower()
        return bool(
            re.search(
                r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
                r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
                r"reject(?:s|ed)?|prevent(?:s|ed)?)\b.{0,140}\b"
                r"(?:decision|action|response|reasoning)\s+caching\b",
                lowered,
            )
            or re.search(
                r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
                r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
                r"reject(?:s|ed)?|prevent(?:s|ed)?)\b.{0,140}\b"
                r"cached\s+(?:decisions?|actions?|responses?|outputs?|reasoning)\b",
                lowered,
            )
            or re.search(
                r"\b(?:tests?|verification|harness|assertions?)\b.{0,140}"
                r"\bfail(?:s|ed|ing)?\b.{0,160}\b"
                r"(?:decision|action|response|reasoning)\s+caching\b",
                lowered,
            )
        )

    @staticmethod
    def _llm_fallback_match_is_protective(text: str) -> bool:
        lowered = str(text or "").lower()
        if re.search(
            r"\bno\s+(?:llm\s+)?(?:credentials?|api\s+keys?|keys?|env(?:ironment)?"
            r"(?:\s+vars?)?|providers?|configuration|config)\b.{0,140}"
            r"\b(?:fallback|fall[-\s]+back)\b",
            lowered,
        ):
            return False
        if (
            _PLAN_ENV_ALIAS_FALLBACK_RE.search(lowered)
            and not _PLAN_BAD_FALLBACK_REPLACEMENT_RE.search(lowered)
        ):
            return True
        if re.search(
            r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
            r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
            r"refuse(?:s)?\s+to)\b.{0,100}\b(?:fallback|fall[-\s]+back)\b",
            lowered,
        ):
            return True
        if re.search(
            r"\b(?:fallback|fall[-\s]+back)\b.{0,120}\b("
            r"forbidden|disallowed|prohibited|blocked|rejected|not\s+allowed"
            r")\b",
            lowered,
        ):
            return True
        if re.search(
            r"\b(?:tests?|verification|harness|check|assertions?)\b.{0,120}"
            r"\bfail(?:s|ed|ing)?\b.{0,160}\b("
            r"fallback|fall[-\s]+back|hardcoded|heuristic|deterministic|static"
            r")\b",
            lowered,
        ):
            return True
        if re.search(
            r"\b(detect|detects|detected|assert|asserts|enforce|enforces|"
            r"prevent|prevents|prove|proves|confirm|confirms|reject|rejects|"
            r"block|blocks|catch|catches|caught)\b.{0,140}\b("
            r"fallback|fall[-\s]+back|hardcoded|heuristic|deterministic|static"
            r")\b",
            lowered,
        ):
            return True
        return False

    def _subtask_card_from_plan_item(
        self,
        item: dict[str, Any],
        *,
        idx: int,
        previous_status: dict[str, str],
    ) -> SubtaskCard:
        title = str(item.get("title") or item.get("name") or f"Subtask {idx + 1}").strip()
        subtask_id = str(
            item.get("id") or item.get("subtask_id") or f"subtask_{idx + 1:02d}"
        ).strip()
        return SubtaskCard(
            id=subtask_id,
            title=title,
            goal=str(item.get("goal") or item.get("description") or title),
            allowed_tools=frozenset(
                str(tool)
                for tool in (item.get("allowed_tools") or item.get("tools") or [])
                if str(tool).strip()
            ),
            allowed_skills=frozenset(
                str(skill)
                for skill in (item.get("allowed_skills") or item.get("skills") or [])
                if str(skill).strip()
            ),
            success_test=self._success_test_from_plan_item(item),
            codeptr_refs=[str(value) for value in (item.get("codeptr_refs") or [])],
            mcp_refs=[str(value) for value in (item.get("mcp_refs") or [])],
            files_to_create=self._first_plan_string_list(
                item,
                "files_to_create",
                "file_to_create",
                "new_files",
                "new_file",
                "files_to_add",
            ),
            files_to_change=self._first_plan_string_list(
                item,
                "files_to_change",
                "file_to_change",
                "files_to_modify",
                "files_to_update",
                "target_files",
                "target_file",
            ),
            files_affected=self._first_plan_string_list(
                item,
                "files_affected",
                "files",
                "paths",
            ),
            dependencies=self._first_plan_string_list(
                item,
                "dependencies",
                "depends_on",
                "requires",
            ),
            contract_migration_reason=str(
                item.get("contract_migration_reason")
                or item.get("test_contract_migration_reason")
                or item.get("success_test_contract_migration_reason")
                or item.get("contract_migration")
                or item.get("test_contract_migration")
                or item.get("success_test_contract_migration")
                or ""
            ).strip()
            or None,
            contract_migration_files=self._first_plan_string_list(
                item,
                "contract_migration_files",
                "test_contract_migration_files",
                "success_test_contract_migration_files",
            ),
            status=previous_status.get(subtask_id, "pending"),  # type: ignore[arg-type]
        )

    @classmethod
    def _first_plan_string_list(cls, item: dict[str, Any], *keys: str) -> list[str]:
        for key in keys:
            values = cls._plan_string_list(item.get(key))
            if values:
                return values
        return []

    @classmethod
    def _plan_string_list(cls, raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            text = raw.strip()
            return [text] if text else []
        if isinstance(raw, dict):
            for key in ("path", "file_path", "file", "target", "value", "name", "id"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    return [value.strip()]
            return []
        if isinstance(raw, (list, tuple, set, frozenset)):
            values: list[str] = []
            for item in raw:
                values.extend(cls._plan_string_list(item))
            return values
        text = str(raw).strip()
        return [text] if text else []

    def _sync_execute_subtasks_from_latest_plan(
        self,
        plan: PhasePlan,
        *,
        run_id: str,
    ) -> bool:
        """Project the accepted plan artifact into the executable phase plan.

        The model-authored plan is stored as an artifact for review, but the
        runner needs concrete SubtaskCard state so execute can work one bounded
        subtask at a time and resume after loop-backs.
        """
        plan_node = plan.get_node("plan")
        if plan_node is not None and plan_node.status != "done":
            return False
        review_node = plan.get_node("plan_review")
        if review_node is not None and review_node.status != "done":
            return False
        payload, _source = self._phase_plan_execution_payload(run_id=run_id)
        if not payload:
            return False
        proposed = payload.get("plan") if isinstance(payload, dict) else None
        if not isinstance(proposed, dict):
            return False
        raw_subtasks = self._execution_items_from_plan(proposed)
        if not raw_subtasks:
            return False
        execute = plan.get_node("execute")
        if execute is None:
            return False

        previous_status = {
            card.id: card.status
            for card in (execute.subtasks or [])
            if isinstance(card, SubtaskCard)
        }
        cards = [
            self._subtask_card_from_plan_item(
                item,
                idx=idx,
                previous_status=previous_status,
            )
            for idx, item in enumerate(raw_subtasks)
            if isinstance(item, dict)
        ]
        if not cards:
            return False
        old_ids = [card.id for card in (execute.subtasks or [])]
        new_ids = [card.id for card in cards]
        if old_ids == new_ids and [
            self._subtask_card_contract_key(card)
            for card in (execute.subtasks or [])
        ] == [self._subtask_card_contract_key(card) for card in cards]:
            return False
        execute.subtasks = cards
        plan.version += 1
        plan.edits_log.append(
            PlanEdit(
                timestamp=time.time(),
                actor="runner",
                patch={
                    "sync_execute_subtasks_from_plan_id": payload.get("plan_id"),
                    "subtask_ids": new_ids,
                },
            )
        )
        return True

    @staticmethod
    def _subtask_card_contract_key(card: SubtaskCard) -> tuple[Any, ...]:
        success = card.success_test
        return (
            card.id,
            card.title,
            card.goal,
            tuple(sorted(card.allowed_tools or ())),
            tuple(sorted(card.allowed_skills or ())),
            success.kind if success else "",
            success.value if success else "",
            tuple(card.codeptr_refs or ()),
            tuple(card.mcp_refs or ()),
            tuple(card.files_to_create or ()),
            tuple(card.files_to_change or ()),
            tuple(card.files_affected or ()),
            tuple(card.dependencies or ()),
            card.contract_migration_reason or "",
            tuple(card.contract_migration_files or ()),
        )

    @staticmethod
    def _incomplete_subtasks(node: PhaseNode | None) -> list[SubtaskCard]:
        if node is None or not node.subtasks:
            return []
        return [card for card in node.subtasks if card.status != "done"]

    def _phase_effective_write_count(self, *, task_id: str) -> int:
        write_tools = {
            "apply_workspace_patch",
            "update_workspace_seed",
            "update_workspace_from_instance",
            "delete_workspace_file",
            "commit_workspace_changes",
            "repo_write_commit",
        }
        path = self._drive_root / "logs" / "tools.jsonl"
        if not path.exists():
            return 0
        count = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("task_id") or "") != task_id:
                    continue
                if (
                    str(row.get("tool") or "") in write_tools
                    and is_effective_write_tool_log_row(row)
                ):
                    count += 1
        except OSError:
            log.debug("Failed to read tools log for write count", exc_info=True)
        return count

    def run(
        self,
        task_input: str,
        *,
        phases: list[str] | None = None,
        run_id: str | None = None,
        dry_run: bool = False,
        stream: bool = False,
    ) -> Iterator[ResultEnvelope]:
        run_id = run_id or str(uuid.uuid4())
        loaded_plan = load_plan(self._drive_root)
        if (
            loaded_plan is not None
            and loaded_plan.run_id == run_id
            and loaded_plan.workspace_id == self._workspace_id
        ):
            plan = loaded_plan
        else:
            plan = build_default_plan(self._workspace_id, run_id=run_id, phases=phases)
        save_plan(plan, self._drive_root)

        manifest_errors = self._registry.validate_all()
        if not manifest_errors:
            try:
                from umbrella.phases.tool_contract import validate_phase_tool_contract

                manifest_errors.extend(
                    validate_phase_tool_contract(
                        self._registry.all(), repo_root=self._repo_root
                    )
                )
            except Exception as exc:
                manifest_errors.append(f"phase tool contract validation failed: {exc}")
        if manifest_errors:
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.PHASE_MANIFEST_INVALID,
                "; ".join(manifest_errors),
                run_id=run_id,
            ))
            return

        if dry_run:
            yield self._emit(ResultEnvelope.success(
                data={
                    "phases": plan.ids() if hasattr(plan, "ids") else [n.id for n in plan.nodes],
                    "manifests_ok": True,
                },
                run_id=run_id,
                phase="dry_run",
                took_ms=0,
            ))
            return

        try:
            max_iterations = max(32, len(plan.nodes) * 8)
            iterations = 0
            while True:
                self._merge_persisted_plan_state(plan)
                if self._sync_execute_subtasks_from_latest_plan(plan, run_id=run_id):
                    save_plan(plan, self._drive_root)
                phase_node = plan.next_pending()
                if phase_node is None:
                    break
                iterations += 1
                if iterations > max_iterations:
                    yield self._emit(ResultEnvelope.failure(
                        ErrorCode.WATCHER_ABORT,
                        "phase runner exceeded loop-back iteration limit",
                        run_id=run_id,
                        phase=phase_node.id,
                    ))
                    return
                if self._stop_requested():
                    yield self._emit(ResultEnvelope.failure(
                        ErrorCode.WATCHER_ABORT,
                        "stop_requested by user before phase start",
                        run_id=run_id,
                        phase=phase_node.id,
                    ))
                    return
                result = yield from self._run_phase(
                    phase_node, plan, run_id=run_id, task_input=task_input
                )
                if result is None or result.outcome == "failed":
                    return
                if result and result.outcome == "loop_back" and result.loop_back_target:
                    self._merge_persisted_plan_state(plan)
                    target = plan.get_node(result.loop_back_target)
                    if target:
                        target.status = "pending"
                        target.started_at = None
                        target.ended_at = None
                    current = plan.get_node(result.phase_id)
                    if current:
                        current.status = "pending"
                        current.started_at = None
                        current.ended_at = None
                    save_plan(plan, self._drive_root)
        finally:
            if self._owns_launcher and self._launcher is not None:
                try:
                    self._launcher.stop()
                except Exception:
                    log.debug("Launcher stop failed", exc_info=True)

        yield self._emit(ResultEnvelope.success(
            data={"run_id": run_id, "status": "complete"},
            run_id=run_id,
            took_ms=0,
        ))

    def _run_phase(
        self,
        phase_node: PhaseNode,
        plan: PhasePlan,
        *,
        run_id: str,
        task_input: str,
    ) -> Iterator[ResultEnvelope]:
        try:
            manifest = self._registry.get(phase_node.manifest_id)
        except KeyError as exc:
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.UNKNOWN_PHASE, str(exc), run_id=run_id, phase=phase_node.id
            ))
            return None

        phase_node.status = "running"
        phase_node.started_at = time.time()
        self._clear_pending_phase_signal()
        save_plan(plan, self._drive_root)

        yield self._emit(ResultEnvelope.success(
            data={"event": "phase_started", "phase": phase_node.id, "label": manifest.id},
            run_id=run_id,
            phase=phase_node.id,
            took_ms=0,
        ))

        base_task = build_phase_task(
            phase_node=phase_node,
            manifest=manifest,
            workspace_id=self._workspace_id,
            run_id=run_id,
            palace=self._palace,
            drive_root=self._drive_root,
            repo_root=self._repo_root,
        )
        if isinstance(phase_node.overlay, dict) and phase_node.overlay.get(
            "retry_reason"
        ):
            revision_contract = phase_node.overlay.get("revision_contract")
            retry_context = phase_node.overlay.get("retry_context")
            if isinstance(revision_contract, dict):
                revision_text = json.dumps(
                    revision_contract,
                    ensure_ascii=False,
                    indent=2,
                )
            elif isinstance(retry_context, dict):
                revision_text = json.dumps(
                    retry_context,
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                revision_text = str(phase_node.overlay.get("retry_reason") or "")
            base_task["input"] = (
                (base_task.get("input") or "")
                + "\n\n## Active retry/revision contract\n"
                + "This phase is being retried after an Umbrella control-plane gate. Treat the retry context below as required acceptance criteria for the new attempt. Do not call the completion tool until the latest artifact explicitly addresses the previous failure and no longer depends on the rejected older attempt.\n"
                + "For planning retries, `propose_phase_plan.plan` must be the full revised compact object with executable leaves. Do not send a diff, notes-only patch, markdown, or serialized/truncated JSON string under `plan.plan`; shorten prose instead of wrapping or truncating the plan.\n"
                + "```json\n"
                + revision_text
                + "\n```\n"
                + "Do not finish this phase until the required completion calls are accepted with concrete verification evidence.\n"
            )
        self._inject_gmas_prewrite_context(base_task)
        base_task["input"] = (base_task.get("input") or "") + f"\n\n## User task\n{task_input}\n"

        if self._candidates_per_phase > 1:
            outcome = self._run_phase_with_harness(
                base_task, phase_node, manifest, run_id=run_id
            )
        else:
            outcome = self._run_phase_single(base_task, phase_node, run_id=run_id)

        self._merge_persisted_plan_state(plan)
        phase_node = plan.get_node(phase_node.id) or phase_node

        if outcome.get("status") == "error":
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.WORKER_PANIC,
                str(outcome.get("error") or "worker failure"),
                run_id=run_id,
                phase=phase_node.id,
            ))
            return None

        completion_failure = self._phase_completion_failure(
            phase_node=phase_node,
            plan=plan,
            manifest=manifest,
            outcome=outcome,
        )
        if not completion_failure and manifest.id == "execute":
            incomplete = self._incomplete_subtasks(phase_node)
            if incomplete:
                first = incomplete[0]
                completion_failure = (
                    "execute phase still has incomplete subtask card(s): "
                    + ", ".join(card.id for card in incomplete[:8])
                    + ". Continue with exactly the next pending subtask "
                    f"`{first.id}` ({first.title}) and call "
                    f"`mark_subtask_complete(subtask_id=\"{first.id}\")` only "
                    "after its success test passes."
                )
        if not completion_failure and manifest.id == "execute":
            if self._phase_effective_write_count(
                task_id=str(outcome.get("task_id") or "")
            ) <= 0:
                completion_failure = (
                    "execute phase completed without any effective workspace write tool calls"
                )
        if completion_failure:
            if completion_failure.startswith("micro review requested revisions"):
                loop_back_target = self._phase_loop_back_target(
                    phase_node=phase_node,
                    outcome=outcome,
                )
                if loop_back_target and plan.get_node(loop_back_target) is not None:
                    result, envelope = self._finish_phase_loop_back(
                        phase_node=phase_node,
                        plan=plan,
                        run_id=run_id,
                        outcome=outcome,
                        loop_back_target=loop_back_target,
                        retry_reason=completion_failure,
                    )
                    yield envelope
                    return result
            if (
                (
                    manifest.id in {"execute", "plan"}
                    and completion_failure.startswith(
                        "phase exit criteria missing required call(s):"
                    )
                )
                or (
                    manifest.id == "execute"
                    and completion_failure.startswith(
                        "execute phase still has incomplete subtask"
                    )
                )
                or (
                    manifest.id == "research"
                    and completion_failure.startswith(
                        "phase exit criteria missing palace writes:"
                    )
                )
                or (
                    manifest.id == "research"
                    and completion_failure.startswith("latest research summary")
                )
                or (
                    manifest.id == "plan"
                    and completion_failure.startswith("latest phase plan")
                )
            ):
                result, envelope = self._finish_phase_loop_back(
                    phase_node=phase_node,
                    plan=plan,
                    run_id=run_id,
                    outcome=outcome,
                    loop_back_target=phase_node.id,
                    retry_reason=completion_failure,
                )
                yield envelope
                return result
            if (
                manifest.id == "plan_review"
                and completion_failure.startswith("latest phase plan")
            ):
                result, envelope = self._finish_phase_loop_back(
                    phase_node=phase_node,
                    plan=plan,
                    run_id=run_id,
                    outcome=outcome,
                    loop_back_target="plan",
                    retry_reason=completion_failure,
                )
                yield envelope
                return result
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.VERIFY_FAILED,
                completion_failure,
                run_id=run_id,
                phase=phase_node.id,
            ))
            return PhaseResult(
                phase_id=phase_node.id,
                outcome="failed",
                error=completion_failure,
            )

        result = PhaseResult(phase_id=phase_node.id, outcome="done")
        if outcome.get("outcome") == "loop_back":
            result = PhaseResult(
                phase_id=phase_node.id,
                outcome="loop_back",
                loop_back_target=outcome.get("loop_back_target"),
            )
        else:
            loop_back_target = self._phase_loop_back_target(
                phase_node=phase_node,
                outcome=outcome,
            )
            if loop_back_target:
                result = PhaseResult(
                    phase_id=phase_node.id,
                    outcome="loop_back",
                    loop_back_target=loop_back_target,
                )

        signal = self._watcher.read_pending_signal()
        if signal and signal.kind == "abort_phase":
            self._watcher.mark_processed(signal.signal_id)
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.WATCHER_ABORT, signal.reason, run_id=run_id, phase=phase_node.id
            ))
            return None

        if self._stop_requested():
            phase_node.status = "failed"
            phase_node.ended_at = time.time()
            save_plan(plan, self._drive_root)
            yield self._emit(ResultEnvelope.failure(
                ErrorCode.WATCHER_ABORT,
                "stop_requested by user during phase",
                run_id=run_id,
                phase=phase_node.id,
            ))
            return None

        phase_node.status = "done"
        phase_node.ended_at = time.time()
        phase_node.overlay = None
        save_plan(plan, self._drive_root)

        yield self._emit(ResultEnvelope.success(
            data={
                "event": "phase_done",
                "phase": phase_node.id,
                "outcome": result.outcome,
                "events": outcome.get("event_count", 0),
            },
            run_id=run_id,
            phase=phase_node.id,
            took_ms=int(
                (phase_node.ended_at - (phase_node.started_at or phase_node.ended_at)) * 1000
            ),
        ))
        return result

    def _run_phase_single(
        self, task: dict[str, Any], phase_node: PhaseNode, *, run_id: str
    ) -> dict[str, Any]:
        launcher = self._ensure_launcher()
        try:
            handle = launcher.submit_task(task, timeout=self._phase_timeout_seconds) \
                if hasattr(launcher, "submit_task") else None
            if handle is None:
                return {"status": "error", "error": "launcher.submit_task returned None"}
            outcome = handle.wait()
            outcome["event_count"] = len(outcome.get("events") or [])
            return outcome
        except Exception as exc:
            log.error("Phase %s launcher invocation failed", phase_node.id, exc_info=True)
            return {"status": "error", "error": str(exc)}

    def _run_phase_with_harness(
        self,
        base_task: dict[str, Any],
        phase_node: PhaseNode,
        manifest: Any,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        """Run N candidates in parallel for this phase, pick the winner.

        Each candidate gets an isolated task_id and mutated prompt; after all complete
        the Watcher (or a heuristic) selects the best result and that one is promoted
        as the phase outcome.
        """
        import concurrent.futures

        launcher = self._ensure_launcher()
        candidates: list[dict[str, Any]] = []
        for k in range(self._candidates_per_phase):
            task_k = dict(base_task)
            task_k["id"] = f"{base_task['id']}:c{k}"
            task_k["input"] = (
                base_task["input"]
                + f"\n\n## Candidate {k+1} of {self._candidates_per_phase}\n"
                "Explore one specific approach; differ from sibling candidates."
            )
            task_k["candidate_isolation"] = True
            candidates.append(task_k)

        results: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._candidates_per_phase, 4)
        ) as executor:
            futures = {
                executor.submit(self._submit_candidate, launcher, c): c for c in candidates
            }
            for fut in concurrent.futures.as_completed(futures):
                cand = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    res = {"status": "error", "error": str(exc), "task_id": cand["id"]}
                res["_candidate_id"] = cand["id"]
                results.append(res)

        winner = self._pick_candidate_winner(results, phase_id=phase_node.id, run_id=run_id)
        winner["event_count"] = len(winner.get("events") or [])
        winner["harness_candidates"] = [
            {"id": r.get("_candidate_id"), "status": r.get("status")} for r in results
        ]
        return winner

    def _submit_candidate(self, launcher: Any, task: dict[str, Any]) -> dict[str, Any]:
        handle = launcher.submit_task(task, timeout=self._phase_timeout_seconds)
        return handle.wait()

    def _pick_candidate_winner(
        self, results: list[dict[str, Any]], *, phase_id: str, run_id: str
    ) -> dict[str, Any]:
        """Pick best candidate. Heuristic: prefer complete > recovered > error;
        within same tier, more events wins."""
        if not results:
            return {"status": "error", "error": "no candidates ran"}

        def score(r: dict[str, Any]) -> tuple[int, int]:
            status = r.get("status", "")
            tier = {"complete": 3, "recovered": 2, "ok": 2}.get(status, 0)
            return (tier, len(r.get("events") or []))

        return max(results, key=score)


def run_phases(
    task_input: str,
    *,
    repo_root: pathlib.Path,
    workspace_id: str,
    phases: list[str] | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    launcher: Any = None,
    candidates_per_phase: int = 1,
    on_envelope: Callable[[ResultEnvelope], None] | None = None,
) -> Iterator[ResultEnvelope]:
    runner = PhaseRunner(
        repo_root=repo_root,
        workspace_id=workspace_id,
        launcher=launcher,
        candidates_per_phase=candidates_per_phase,
        on_envelope=on_envelope,
    )
    yield from runner.run(task_input, phases=phases, run_id=run_id, dry_run=dry_run)
