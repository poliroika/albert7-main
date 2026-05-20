"""Shared imports and constants for split Umbrella workspace tools."""

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import ast
import builtins
import tomllib
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from umbrella.file_preview import read_file_preview
from umbrella.deep_agent_tools.context import (
    _coerce_int,
    _current_workspace_id_from_drive,
    _drive_state_path,
    _json,
    _matching_stop_request,
    _memory_store,
    _palace_backend,
    _read_drive_state,
    _resolve_prompt_name,
    _resolve_umbrella_repo_root,
    _resolve_workspace_file,
    _split_tags,
    _stop_request_matches_task,
    _stop_requested_block,
    _strip_workspace_prefix,
    _workspace_memory_root,
    _workspace_path,
    _workspace_root,
    _workspace_verification_passed,
    _write_drive_state,
    _rewrite_python_command_for_workspace,
    _set_workspace_verification_state,
)
from umbrella.deep_agent_tools.memory import (
    _PHASE_MEMORY_TAGS,
    _is_unverified_memory,
    _lesson_is_verified,
    _memory_evidence_kind,
    _memory_hit_tags,
    _memory_metadata,
    _memory_room,
    _memory_run_id,
    _memory_tags_from_value,
    _phase_rerank_memory_hits,
    _preferred_memory_tags_for_phase,
    _publish_recall_state_to_ctx,
    _resolve_memory_query_scope,
    _run_id_from_task_id,
    _split_verified_first,
    _current_run_id_from_ctx,
    get_umbrella_memory,
    list_memory_tree,
    python_eval,
    record_idea,
    record_workspace_event,
    save_umbrella_lesson,
    save_umbrella_memory,
    update_prompt,
)
from ouroboros.tools import background_jobs as _bg_jobs
from umbrella.deep_agent_tools.skills import (
    _WORKSPACE_TOML_KNOWN_SKILLS,
    _upsert_workspace_toml_skill,
    configure_workspace_skills,
    load_skill,
)
from ouroboros.tools.terminal_session import (
    RunResult,
    get_or_create_session,
)
log = logging.getLogger(__name__)
_PYTHON_COMMAND_NAMES = {
    "python",
    "python3",
    "py",
    "python.exe",
    "python3.exe",
    "py.exe",
}
_SCROLLBACK_REL_PATH = Path("memory") / "terminal_scrollback.md"
_SCROLLBACK_MAX_BYTES = 2 * 1024 * 1024
_SCROLLBACK_TRIM_FRACTION = 0.25
_LLM_RUNTIME_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".env",
    ".md",
    ".markdown",
}
_LLM_PROVIDER_DEFAULT_PATTERNS = (
    re.compile(
        r"""["']?https://api\.openai\.com/(?:v1/?|v1/[^\s"'#,)]*)["']?""",
        re.I,
    ),
    re.compile(r"""["']?(?:openai/)?gpt-[A-Za-z0-9_.:-]+["']?""", re.I),
)
_LLM_BEHAVIOR_SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
}
_LLM_BEHAVIOR_SIGNAL_RE = re.compile(
    r"(?i)\b(?:llm|gmas|agent|bot|model|openai|create_openai_caller|"
    r"llm_response|llm_caller)\b"
)
_LLM_BEHAVIOR_FALLBACK_PATTERNS = (
    re.compile(
        r"(?is)\b(?:fallback|fall[-\s]?back)\b.{0,240}"
        r"(?:\bpositive\s*/\s*negative\s+sentiment\b|"
        r"\b(?:positive|negative)[_\s-]*(?:words?|count|sentiment)\b)"
    ),
    re.compile(
        r"(?is)\b(?:fallback|fall[-\s]?back)\b.{0,240}"
        r"\b(?:sentiment|keyword|word\s+count|positive\s*/\s*negative|"
        r"positive\s+negative)\b.{0,200}"
        r"\b(?:accept|reject|decision|action|proposal|return\s+(?:True|False))\b"
    ),
    re.compile(
        r"(?is)\b(?:positive_count|negative_count|positive_words|negative_words)\b"
        r".{0,360}\b(?:return\s+(?:True|False)|accept|reject|decision)\b"
    ),
    re.compile(
        r"(?is)\b(?:fallback|fall[-\s]?back)\b.{0,240}"
        r"\b(?:heuristics?|deterministic|static|rule[-\s]?based|default)\b"
        r".{0,200}\b(?:decision|action|accept|reject|proposal)\b"
    ),
)
_READ_CACHE_MAX_ENTRIES = 256
_read_cache: "OrderedDict[tuple[str, str, int, int], str]" = OrderedDict()
_RUN_WORKSPACE_DEFAULT_TIMEOUT_S = 180
_RUN_WORKSPACE_MAX_TIMEOUT_S = 600
_SECRET_ENV_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])\.env(?:[.\w-]*)?(?![A-Za-z0-9_])"
)
_SECRET_PATH_COMPONENTS = {
    "credential",
    "credentials",
    "secret",
    "secrets",
}
_SECRET_FILE_STEMS = {
    "api_key",
    "apikey",
}
_SOURCE_CONTROL_ROLLBACK_COMMANDS = {
    "checkout",
    "reset",
    "restore",
    "clean",
    "stash",
}
_DIRECT_WORKSPACE_MUTATION_COMMANDS = {
    "copy",
    "copy.exe",
    "cp",
    "cp.exe",
    "del",
    "del.exe",
    "erase",
    "erase.exe",
    "mkdir",
    "mkdir.exe",
    "move",
    "move.exe",
    "mv",
    "mv.exe",
    "ren",
    "ren.exe",
    "rename",
    "rename.exe",
    "rm",
    "rm.exe",
    "rmdir",
    "rmdir.exe",
    "tee",
    "tee.exe",
    "touch",
    "touch.exe",
}
_NONPORTABLE_WORKSPACE_PROBE_COMMANDS = {
    "awk",
    "awk.exe",
    "cat",
    "cat.exe",
    "find",
    "find.exe",
    "findstr",
    "findstr.exe",
    "grep",
    "grep.exe",
    "head",
    "head.exe",
    "less",
    "less.exe",
    "more",
    "more.com",
    "nl",
    "nl.exe",
    "sed",
    "sed.exe",
    "tail",
    "tail.exe",
    "type",
    "wc",
    "wc.exe",
}
_COMMAND_CHAIN_TOKENS = {"&&", ";", "||"}
_SERVER_TOKEN_PATTERNS = (
    "uvicorn",
    "gunicorn",
    "hypercorn",
    "daphne",
    "vllm",
    "streamlit",
    "tensorboard",
    "jupyter",
    "nodemon",
    "vite",
    "webpack-dev-server",
)
_SERVER_TOKEN_SUBSTRINGS = ("runserver", "serve_forever")
_SERVER_SOURCE_MARKERS = (
    "uvicorn.run(",
    "app.run(",
    ".serve_forever(",
    "fastapi(",
)
_INTERACTIVE_APP_ENTRY_NAMES = {
    "main.py",
    "app.py",
    "game.py",
    "run.py",
    "play.py",
}
_INTERACTIVE_APP_MODULE_NAMES = {
    "main",
    "app",
    "game",
    "play",
}
_ROOT_DIAGNOSTIC_WRITE_RE = re.compile(
    r"(?i)^(?:"
    r"(?:check|debug|diagnose|extract|find|fix|inspect|probe|read|scan|scratch|search|verify|validate)_.*\.py|"
    r"run_(?:check|checks|verification|dry_run|manual_.*|news_.*)\.py|"
    r"test_.*\.py|real_test_.*\.py"
    r")$"
)
_ROOT_DOC_WRITE_RE = re.compile(
    r"(?i)^(?:handoff.*|agent_topology.*|architecture|agent_.*|.*_handoff|.*_topology)\.md$"
)
_DIAGNOSTIC_SCRIPT_BASENAME_RE = re.compile(
    r"(?i)^(?:"
    r"(?:check|debug|diagnose|extract|find|fix|inspect|probe|read|scan|scratch|search|verify|validate)_.*\.py|"
    r"run_(?:check|checks|verification|dry_run|manual_.*|news_.*)\.py|"
    r"test_.*\.py|real_test_.*\.py"
    r")$"
)
_RAW_ARTIFACT_BASENAME_RE = re.compile(
    r"(?i).*(?:_raw|_raw_extracted|_extracted)\.(?:txt|md|json|csv|tsv)$"
)
_GREENFIELD_PY_NON_IMPL_TOPS: frozenset[str] = frozenset(
    {
        ".git",
        ".memory",
        ".umbrella",
        ".umbrella_scratch",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "doc",
        "docs",
        "frontend",
        "node_modules",
        "public",
        "reports",
        "test",
        "tests",
        "tmp",
        "venv",
    }
)
_GREENFIELD_PY_ALLOWED_ROOT_FILES: frozenset[str] = frozenset(
    {
        "asgi.py",
        "conftest.py",
        "manage.py",
        "setup.py",
        "wsgi.py",
    }
)
_PY_MAGIC_GLOBALS = {
    "__annotations__",
    "__builtins__",
    "__cached__",
    "__debug__",
    "__doc__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__spec__",
}
_SOURCE_TRUNCATION_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".css",
        ".scss",
        ".html",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".md",
        ".markdown",
    }
)
_STRONG_VERIFICATION_KINDS = {"shell", "pytest", "smoke_run"}
_DELETE_PROTECTED_TOP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".umbrella",
        ".umbrella_scratch",
        ".memory",
        ".venv",
        "venv",
    }
)
_DELETE_PROTECTED_BASENAMES: frozenset[str] = frozenset(
    {
        "task_main.md",
        "workspace.toml",
        "verification.toml",
        "readme.md",
        "pyproject.toml",
        "requirements.txt",
    }
)
_DELETE_SOURCE_EXTS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".css",
        ".scss",
        ".html",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
    }
)
_DELETE_MANAGED_SOURCE_TOP_DIRS: frozenset[str] = frozenset(
    {
        "api",
        "app",
        "backend",
        "client",
        "frontend",
        "server",
        "src",
        "test",
        "tests",
    }
)
_DELETE_AS_REPAIR_REASON_RE = re.compile(
    r"\b(corrupt(?:ed|ion)?|truncat(?:ed|ion)?|hunk\s*mismatch|"
    r"patch\s*(?:failed|mismatch)|recreate|rewrite\s+clean|reset|"
    r"replac(?:e|ed|es|ing)|rewrit(?:e|es|ing)|correct(?:ed|ion)?|"
    r"fix(?:ed|es|ing)?|clean\s+up|"
    r"line\s+endings?|imports?|schema|duplicate)\b",
    re.IGNORECASE,
)
__all__ = [
    'search_gmas_knowledge',
    'get_gmas_context',
    '_mark_explicit_gmas_context_call',
    '_task_tool_log_has',
    '_workspace_has_gmas_skill',
    '_llm_runtime_contract_block',
    '_llm_behavior_fallback_contract_block',
    '_llm_behavior_fallback_match_is_protective',
    '_gmas_context_before_write_block',
]


__all__ = [
    'Any',
    'Dict',
    'List',
    'Optional',
    'OrderedDict',
    'Path',
    'RunResult',
    '_COMMAND_CHAIN_TOKENS',
    '_DELETE_AS_REPAIR_REASON_RE',
    '_DELETE_MANAGED_SOURCE_TOP_DIRS',
    '_DELETE_PROTECTED_BASENAMES',
    '_DELETE_PROTECTED_TOP_DIRS',
    '_DELETE_SOURCE_EXTS',
    '_DIAGNOSTIC_SCRIPT_BASENAME_RE',
    '_DIRECT_WORKSPACE_MUTATION_COMMANDS',
    '_GREENFIELD_PY_ALLOWED_ROOT_FILES',
    '_GREENFIELD_PY_NON_IMPL_TOPS',
    '_INTERACTIVE_APP_ENTRY_NAMES',
    '_INTERACTIVE_APP_MODULE_NAMES',
    '_LLM_BEHAVIOR_FALLBACK_PATTERNS',
    '_LLM_BEHAVIOR_SIGNAL_RE',
    '_LLM_BEHAVIOR_SOURCE_EXTENSIONS',
    '_LLM_PROVIDER_DEFAULT_PATTERNS',
    '_LLM_RUNTIME_CODE_EXTENSIONS',
    '_NONPORTABLE_WORKSPACE_PROBE_COMMANDS',
    '_PHASE_MEMORY_TAGS',
    '_PYTHON_COMMAND_NAMES',
    '_PY_MAGIC_GLOBALS',
    '_RAW_ARTIFACT_BASENAME_RE',
    '_READ_CACHE_MAX_ENTRIES',
    '_ROOT_DIAGNOSTIC_WRITE_RE',
    '_ROOT_DOC_WRITE_RE',
    '_RUN_WORKSPACE_DEFAULT_TIMEOUT_S',
    '_RUN_WORKSPACE_MAX_TIMEOUT_S',
    '_SCROLLBACK_MAX_BYTES',
    '_SCROLLBACK_REL_PATH',
    '_SCROLLBACK_TRIM_FRACTION',
    '_SECRET_ENV_PATH_RE',
    '_SECRET_FILE_STEMS',
    '_SECRET_PATH_COMPONENTS',
    '_SERVER_SOURCE_MARKERS',
    '_SERVER_TOKEN_PATTERNS',
    '_SERVER_TOKEN_SUBSTRINGS',
    '_SOURCE_CONTROL_ROLLBACK_COMMANDS',
    '_SOURCE_TRUNCATION_EXTENSIONS',
    '_STRONG_VERIFICATION_KINDS',
    '_WORKSPACE_TOML_KNOWN_SKILLS',
    '__all__',
    '_bg_jobs',
    '_coerce_int',
    '_current_run_id_from_ctx',
    '_current_workspace_id_from_drive',
    '_drive_state_path',
    '_is_unverified_memory',
    '_json',
    '_lesson_is_verified',
    '_matching_stop_request',
    '_memory_evidence_kind',
    '_memory_hit_tags',
    '_memory_metadata',
    '_memory_room',
    '_memory_run_id',
    '_memory_store',
    '_memory_tags_from_value',
    '_palace_backend',
    '_phase_rerank_memory_hits',
    '_preferred_memory_tags_for_phase',
    '_publish_recall_state_to_ctx',
    '_read_cache',
    '_read_drive_state',
    '_resolve_memory_query_scope',
    '_resolve_prompt_name',
    '_resolve_umbrella_repo_root',
    '_resolve_workspace_file',
    '_rewrite_python_command_for_workspace',
    '_run_id_from_task_id',
    '_set_workspace_verification_state',
    '_split_tags',
    '_split_verified_first',
    '_stop_request_matches_task',
    '_stop_requested_block',
    '_strip_workspace_prefix',
    '_upsert_workspace_toml_skill',
    '_workspace_memory_root',
    '_workspace_path',
    '_workspace_root',
    '_workspace_verification_passed',
    '_write_drive_state',
    'ast',
    'builtins',
    'configure_workspace_skills',
    'datetime',
    'get_or_create_session',
    'get_umbrella_memory',
    'json',
    'list_memory_tree',
    'load_skill',
    'log',
    'logging',
    'os',
    'python_eval',
    're',
    'read_file_preview',
    'record_idea',
    'record_workspace_event',
    'save_umbrella_lesson',
    'save_umbrella_memory',
    'shlex',
    'subprocess',
    'sys',
    'timezone',
    'tomllib',
    'update_prompt',
]
