"""Ouroboros ToolEntry injection for Umbrella-owned deep-agent tools.

This module keeps the bulky schema surface outside ``ouroboros.tools``. The
current adapter still returns Ouroboros ``ToolEntry`` objects, while the
handlers live on the thin Ouroboros bridge module or in ``umbrella.deep_agent_tools``.
A future Hermes adapter can reuse the same Umbrella-side specs without copying
Ouroboros' large bridge file.
"""

def get_ouroboros_tool_entries():
    from ouroboros.tools.registry import ToolEntry
    from umbrella.deep_agent_tools import workspace_tools as _handlers
    globals().update(vars(_handlers))

    return [
        ToolEntry(
            "search_gmas_knowledge",
            {
                "name": "search_gmas_knowledge",
                "description": "Search GMAS docs/examples/code and return rich snippets. Use before authoring GMAS agents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 6},
                        "limit": {
                            "type": "integer",
                            "description": "Backward-compatible alias for max_results; prefer max_results.",
                        },
                        "max_chars_per_hit": {"type": "integer", "default": 8000},
                        "intent": {
                            "type": "string",
                            "default": "",
                            "description": "Optional audit metadata describing why this GMAS lookup is being performed.",
                        },
                        "slug": {
                            "type": "string",
                            "default": "",
                            "description": "Optional audit metadata label for the GMAS lookup; does not affect retrieval.",
                        },
                    },
                    "required": ["query"],
                },
            },
            lambda ctx, **kw: search_gmas_knowledge(ctx, **kw),
            timeout_sec=300,
        ),
        ToolEntry(
            "get_gmas_context",
            {
                "name": "get_gmas_context",
                "description": "Return full-enough GMAS context for implementation: docs, examples, code windows, and usage hints.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 6},
                        "limit": {
                            "type": "integer",
                            "description": "Backward-compatible alias for max_results; prefer max_results.",
                        },
                        "max_chars_per_hit": {"type": "integer", "default": 12000},
                        "intent": {
                            "type": "string",
                            "default": "",
                            "description": "Optional audit metadata describing why this GMAS lookup is being performed.",
                        },
                        "slug": {
                            "type": "string",
                            "default": "",
                            "description": "Optional audit metadata label for the GMAS lookup; does not affect retrieval.",
                        },
                    },
                    "required": ["query"],
                },
            },
            lambda ctx, **kw: get_gmas_context(ctx, **kw),
            timeout_sec=300,
        ),

        ToolEntry(
            "list_workspace_files",
            {
                "name": "list_workspace_files",
                "description": "List files inside host repo workspaces/<workspace_id>.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "subdir": {"type": "string", "default": ""},
                        "max_entries": {"type": "integer", "default": 300},
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: list_workspace_files(ctx, **kw),
        ),
        ToolEntry(
            "read_workspace_file",
            {
                "name": "read_workspace_file",
                "description": (
                    "Read a file from host repo workspaces/<workspace_id> and return a text preview. "
                    "`offset` is a character offset, not a line number. If you have line numbers "
                    "from pytest, rg, findstr, or stack traces, use `line_start` and `line_count`. "
                    "Handles UTF-8 text files AND natively previews `.docx` (returns paragraphs, "
                    "content_kind=`office_docx`) and `.pptx` (returns slide-by-slide text, "
                    "content_kind=`office_pptx`) WITHOUT shelling out — do NOT call "
                    "`run_workspace_command python -c 'import docx ...'` for these formats, just "
                    "use this tool. Binary files return a `[binary file preview unavailable]` "
                    "marker. The `file_path` is relative to `workspaces/<workspace_id>/` and may "
                    "contain non-ASCII (e.g. Cyrillic) characters — pass them verbatim."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "file_path": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 30000},
                        "offset": {
                            "type": "integer",
                            "default": 0,
                            "description": "Character offset, not a line number.",
                        },
                        "line_start": {
                            "type": "integer",
                            "default": 0,
                            "description": "1-based line number for line-based reads.",
                        },
                        "line_count": {"type": "integer", "default": 160},
                    },
                    "required": ["workspace_id", "file_path"],
                },
            },
            lambda ctx, **kw: read_workspace_file(ctx, **kw),
        ),
        ToolEntry(
            "run_workspace_command",
            {
                "name": "run_workspace_command",
                "description": (
                    "Run a NON-INTERACTIVE, FOREGROUND command inside a host repo workspace. "
                    "You may pass either `argv` (preferred) or `command`. "
                    "Default per-call timeout is 180s; hard cap is 600s. "
                    "USE THE RIGHT TOOL FOR THE JOB:\n"
                    "  - Workspace file/tree inspection -> use `read_workspace_file`/"
                    "`list_workspace_files` (or `repo_read`/`repo_list` for repo paths), "
                    "not shell-only utilities like cat/grep/sed/head/tail.\n"
                    "  - Do not call `bash`, `sh`, or argv starting with `-c`; this "
                    "tool needs an explicit portable executable such as `python`, "
                    "`python -m pytest`, `node`, or `npm`.\n"
                    "  - Long-running server (uvicorn, fastapi, vllm, gunicorn, dev server, "
                    "ollama serve, etc.) -> use `bg_start` (this tool will REJECT such commands "
                    "to prevent timeout/zombie leaks).\n"
                    "  - Multi-line Python script with `def`/`async def`/`class`/`for`/`if`/"
                    '`try` joined by `;` -> use `run_python_code`. `python -c "..."` only '
                    "parses simple statements; this tool will REJECT compound `python -c` calls.\n"
                    "  - Need fresh info from the public web (current best library, model, API) "
                    "-> `web_search` + `web_fetch`.\n"
                    "  - Quick one-liner shell, build, test, curl, or CLI behavior check -> this tool.\n"
                    "On POSIX with tmux/bash there is one persistent shell per workspace "
                    "(cd/export/background `&` survive across calls). On Windows there is "
                    "NO persistence: each call is a fresh process spawn. Always pass "
                    "absolute paths or use `subdir`. "
                    "On timeout the entire process tree is killed (taskkill /T on Windows, "
                    "killpg on POSIX), so a hung subprocess will not survive the call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Preferred exact argv vector to execute. The first "
                                "element must be a portable executable, not `bash`, "
                                "`sh`, or an option like `-c`."
                            ),
                        },
                        "command": {
                            "type": ["array", "string"],
                            "description": "Alternative free-form command payload. May be a string or argv-style list.",
                        },
                        "subdir": {"type": "string", "default": ""},
                        "timeout_seconds": {
                            "type": "integer",
                            "default": _RUN_WORKSPACE_DEFAULT_TIMEOUT_S,
                            "description": (
                                "Per-call wall-clock timeout in seconds. "
                                f"Default {_RUN_WORKSPACE_DEFAULT_TIMEOUT_S}, "
                                f"hard-capped at {_RUN_WORKSPACE_MAX_TIMEOUT_S}."
                            ),
                        },
                        "allow_dependency_install": {
                            "type": "boolean",
                            "default": False,
                        },
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: run_workspace_command(ctx, **kw),
            is_code_tool=True,
            timeout_sec=_RUN_WORKSPACE_MAX_TIMEOUT_S,
        ),
        ToolEntry(
            "run_python_code",
            {
                "name": "run_python_code",
                "description": (
                    "Run a multi-line Python script inside a workspace. Use this "
                    'INSTEAD OF `python -c "..."` for any non-trivial script: '
                    "`def`, `async def`, `class`, `for`, `while`, `if`, `with`, "
                    "`try/except` -- CPython's `-c` parses its body as a single "
                    "simple statement and SyntaxErrors on the first compound block "
                    "keyword joined with `;`. The script is written to "
                    "`<workspace>/.umbrella_scratch/run_<id>.py` and executed via "
                    "`uv run python <file>` (or plain `python` if `use_uv=false`). "
                    "Default per-call timeout 180s, hard-capped at 600s. "
                    "Stdout+stderr and exit_code are returned identically to "
                    "`run_workspace_command`. Don't use this for long-running "
                    "servers -- use `bg_start` instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "code": {
                            "type": "string",
                            "description": "Full Python source as a single multi-line string.",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional CLI arguments forwarded to the script.",
                        },
                        "subdir": {"type": "string", "default": ""},
                        "timeout_seconds": {
                            "type": "integer",
                            "default": _RUN_WORKSPACE_DEFAULT_TIMEOUT_S,
                            "description": (
                                f"Per-call timeout. Default {_RUN_WORKSPACE_DEFAULT_TIMEOUT_S}s, "
                                f"hard cap {_RUN_WORKSPACE_MAX_TIMEOUT_S}s."
                            ),
                        },
                        "use_uv": {
                            "type": "boolean",
                            "default": True,
                            "description": "Run via `uv run python` (recommended) so the workspace's pyproject is honored.",
                        },
                        "extra_env": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Extra env vars exported for this call only (PYTHONPATH, ports, etc.).",
                        },
                    },
                    "required": ["workspace_id", "code"],
                },
            },
            lambda ctx, **kw: run_python_code(ctx, **kw),
            is_code_tool=True,
            timeout_sec=_RUN_WORKSPACE_MAX_TIMEOUT_S,
        ),
        ToolEntry(
            "terminal_view",
            {
                "name": "terminal_view",
                "description": (
                    "Read recent scrollback from the persistent shell for a workspace. "
                    "Cheap, read-only -- use it to re-read what an earlier "
                    "run_workspace_command printed once the raw tool result has been "
                    "compacted out of the conversation history."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "last_lines": {
                            "type": "integer",
                            "default": 200,
                            "description": "Tail size in lines (1..4000).",
                        },
                        "grep": {
                            "type": "string",
                            "default": "",
                            "description": "Optional Python regex; only matching lines are returned.",
                        },
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: terminal_view(ctx, **kw),
            is_code_tool=False,
            timeout_sec=30,
        ),
        ToolEntry(
            "terminal_reset",
            {
                "name": "terminal_reset",
                "description": (
                    "Destroy and re-create the persistent shell for a workspace. "
                    "Drops cwd, env vars and background jobs. Use ONLY when the shell "
                    "is genuinely wedged or you must guarantee a clean environment; a "
                    "non-empty `reason` is required so the decision is auditable."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "reason": {
                            "type": "string",
                            "default": "",
                            "description": "Why the reset is necessary. Required.",
                        },
                    },
                    "required": ["workspace_id", "reason"],
                },
            },
            lambda ctx, **kw: terminal_reset(ctx, **kw),
            is_code_tool=True,
            timeout_sec=30,
        ),
        ToolEntry(
            "commit_workspace_changes",
            {
                "name": "commit_workspace_changes",
                "description": "Commit host repo workspace changes locally after run_workspace_verify passes. Never pushes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "commit_message": {"type": "string"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "include_data": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, include workspaces/<id>/data/ cache files in the commit.",
                        },
                    },
                    "required": ["workspace_id", "commit_message"],
                },
            },
            lambda ctx, **kw: commit_workspace_changes(ctx, **kw),
            is_code_tool=True,
            timeout_sec=300,
        ),
        ToolEntry(
            "get_workspace_metrics",
            {
                "name": "get_workspace_metrics",
                "description": "Get performance metrics for workspaces.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string", "default": ""},
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: get_workspace_metrics(ctx, **kw),
        ),
        ToolEntry(
            "get_workspace_logs",
            {
                "name": "get_workspace_logs",
                "description": "Read recent logs from workspace instances.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "run_id": {"type": "string", "default": ""},
                        "tail": {"type": "integer", "default": 100},
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: get_workspace_logs(ctx, **kw),
        ),
        ToolEntry(
            "update_workspace_seed",
            {
                "name": "update_workspace_seed",
                "description": "Update a seed workspace file with backup. For existing files, prefer apply_workspace_patch after read_workspace_file; use this for new files or intentional full rewrites.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "file_path": {"type": "string"},
                        "new_content": {"type": "string"},
                        "create_backup": {"type": "boolean", "default": True},
                        "allow_large_overwrite": {"type": "boolean", "default": False},
                        "validation_summary": {"type": "string", "default": ""},
                    },
                    "required": ["workspace_id", "file_path", "new_content"],
                },
            },
            lambda ctx, **kw: update_workspace_seed(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "apply_workspace_patch",
            {
                "name": "apply_workspace_patch",
                "description": (
                    "Apply an OpenAI-style patch envelope to workspace files with audit/backups. "
                    "Use this for targeted edits to existing files after calling read_workspace_file "
                    "on each Update/Delete target. Add File operations do not require a prior read. "
                    "Patch format: *** Begin Patch, then *** Update File: path / *** Add File: path / "
                    "*** Delete File: path, hunks with @@ and lines prefixed by space/+/- where relevant, "
                    "then *** End Patch."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "patch": {
                            "type": "string",
                            "description": (
                                "*** Begin Patch\n"
                                "*** Update File: src/app.py\n"
                                "@@\n"
                                " old_line\n"
                                "-remove_this\n"
                                "+add_this\n"
                                "*** End Patch"
                            ),
                        },
                        "validation_summary": {"type": "string", "default": ""},
                    },
                    "required": ["workspace_id", "patch"],
                },
            },
            lambda ctx, **kw: apply_workspace_patch(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "replace_workspace_file",
            {
                "name": "replace_workspace_file",
                "description": (
                    "Atomically replace a workspace file after repeated patch hunk "
                    "mismatches. Requires read_file digest match via expected_sha256."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "path": {"type": "string"},
                        "expected_sha256": {"type": "string"},
                        "content": {"type": "string"},
                        "validation_summary": {"type": "string", "default": ""},
                    },
                    "required": ["workspace_id", "path", "expected_sha256", "content"],
                },
            },
            lambda ctx, **kw: replace_workspace_file(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "delete_workspace_file",
            {
                "name": "delete_workspace_file",
                "description": (
                    "Sanctioned single-file delete for workspace cleanup. Use "
                    "this — and ONLY this — to remove ad-hoc diagnostic scripts, "
                    "raw-extracted artefacts, stray handoff docs, and similar "
                    "noise that the layout policy or final sweep flags. Shell "
                    '`rm` / `del` / `Remove-Item` and `python -c "...unlink()..."` '
                    "are blocked on purpose; this is the audited path. Protects "
                    ".git/.umbrella/.memory/.venv plus TASK_MAIN.md, workspace.toml, "
                    "README.md from accidental deletion. Records a workspace "
                    "event so the cleanup is part of the audit trail."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "file_path": {
                            "type": "string",
                            "description": "Workspace-relative POSIX path of the file to delete.",
                        },
                        "reason": {
                            "type": "string",
                            "default": "",
                            "description": (
                                "Short justification (e.g. 'ad-hoc probe script left "
                                "over from extraction', 'raw extract artefact replaced "
                                "by docs/requirements.md'). Recorded with the event."
                            ),
                        },
                    },
                    "required": ["workspace_id", "file_path"],
                },
            },
            lambda ctx, **kw: delete_workspace_file(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "update_workspace_from_instance",
            {
                "name": "update_workspace_from_instance",
                "description": "Copy improved files from an instance to seed workspace with backup.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "instance_name": {"type": "string"},
                        "files_to_copy": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["workspace_id", "instance_name", "files_to_copy"],
                },
            },
            lambda ctx, **kw: update_workspace_from_instance(ctx, **kw),
            is_code_tool=True,
        ),


        ToolEntry(
            "update_prompt",
            {
                "name": "update_prompt",
                "description": (
                    "Update a workspace-scoped Ouroboros prompt overlay. Writes only to "
                    "workspaces/<id>/.memory/prompts/{SYSTEM,BIBLE,CONSCIOUSNESS}.md and logs a diff; "
                    "never edits the repo seed prompt."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "One of SYSTEM, BIBLE, CONSCIOUSNESS",
                        },
                        "new_content": {"type": "string"},
                        "reason": {"type": "string"},
                        "workspace_id": {"type": "string", "default": ""},
                    },
                    "required": ["name", "new_content", "reason"],
                },
            },
            lambda ctx, **kw: update_prompt(ctx, **kw),
            is_code_tool=True,
        ),

        ToolEntry(
            "run_workspace_verify",
            {
                "name": "run_workspace_verify",
                "description": "Run the workspace verification spec and return a structured pass/fail report. Use every ~30-50 edits and before declaring a feature done. Resets the verify-gate counter.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "default": 600},
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: run_workspace_verify(ctx, **kw),
            is_code_tool=True,
            timeout_sec=900,
        ),
        ToolEntry(
            "probe_input_file",
            {
                "name": "probe_input_file",
                "description": (
                    "Magic-bytes probe for an input file. Call this BEFORE "
                    "choosing a parser when TASK_MAIN points you at a file "
                    "by extension — e.g. a '.docx' that might actually be "
                    "plain text, a '.xlsx' that might be CSV. Returns "
                    "{actual_format, mismatch, hint}: pick the parser that "
                    "matches actual_format, not declared_ext. Read-only, "
                    "no side effects."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path to the file. Workspace-relative paths are "
                                "resolved against the active workspace; absolute "
                                "paths must stay inside the workspace root."
                            ),
                        },
                        "workspace_id": {"type": "string", "default": ""},
                    },
                    "required": ["path"],
                },
            },
            lambda ctx, **kw: probe_input_file(ctx, **kw),
        ),
        ToolEntry(
            "python_eval",
            {
                "name": "python_eval",
                "description": "Run guarded Python code from a string in the workspace .memory/drive/tmp directory. Use instead of fragile python -c one-liners for read-only analysis.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "default": 30},
                        "workspace_id": {"type": "string", "default": ""},
                    },
                    "required": ["code"],
                },
            },
            lambda ctx, **kw: python_eval(ctx, **kw),
            is_code_tool=True,
            timeout_sec=180,
        ),
        ToolEntry(
            "run_workspace_task",
            {
                "name": "run_workspace_task",
                "description": "Compatibility shim only. Old Umbrella manager execution is disabled; use workspace tools instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_input": {"type": "string"},
                        "workspace_id": {"type": "string", "default": ""},
                        "max_iterations": {"type": "integer", "default": 5},
                    },
                    "required": ["task_input"],
                },
            },
            lambda ctx, **kw: run_workspace_task(ctx, **kw),
        ),
        ToolEntry(
            "sandbox_self_edit",
            {
                "name": "sandbox_self_edit",
                "description": (
                    "Persistently edit your own code (ouroboros/ or umbrella/) to fix a capability gap. "
                    "Use only for harness/code bugs. Do not use this for prompt updates; use "
                    "update_prompt so changes stay scoped to the current workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Repo-relative path, e.g. ouroboros/ouroboros/tools/my_fix.py",
                        },
                        "new_content": {
                            "type": "string",
                            "description": "Full file content to write",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why this self-edit is needed (capability gap description)",
                        },
                        "surface": {
                            "type": "string",
                            "default": "ouroboros",
                            "description": "ouroboros or umbrella",
                        },
                    },
                    "required": ["file_path", "new_content", "reason"],
                },
            },
            lambda ctx, **kw: sandbox_self_edit(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "delegate_to_ouroboros",
            {
                "name": "delegate_to_ouroboros",
                "description": "Queue a separate Ouroboros task. Avoid unless explicitly decomposing work.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_description": {"type": "string"},
                        "workspace_id": {"type": "string", "default": ""},
                        "code_updates": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["task_description"],
                },
            },
            lambda ctx, **kw: delegate_to_ouroboros(ctx, **kw),
        ),
        ToolEntry(
            "bg_start",
            {
                "name": "bg_start",
                "description": (
                    "Start a long-running command (server, worker, watcher) DETACHED from "
                    "this tool call. Use this for uvicorn/fastapi/vllm/etc. instead of "
                    "run_workspace_command, which would block until timeout. "
                    "Returns a `job_id` immediately; stdout+stderr stream to "
                    "<drive>/logs/bg/<job_id>.log. Combine with `bg_status` (is it alive?), "
                    "`bg_tail` (read recent log lines) and `bg_kill` (stop it)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "argv": {"type": "array", "items": {"type": "string"}},
                        "command": {"type": ["array", "string"]},
                        "subdir": {"type": "string", "default": ""},
                        "label": {
                            "type": "string",
                            "default": "",
                            "description": "Short human label (e.g. uvicorn-news-cards).",
                        },
                        "env": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Extra env vars (PYTHONPATH, PORT, ...).",
                        },
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: bg_start(ctx, **kw),
            is_code_tool=True,
            timeout_sec=60,
        ),
        ToolEntry(
            "bg_status",
            {
                "name": "bg_status",
                "description": "Check if a background job is alive and how big its log is. Cheap, read-only.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
            lambda ctx, **kw: bg_status(ctx, **kw),
            timeout_sec=15,
        ),
        ToolEntry(
            "bg_tail",
            {
                "name": "bg_tail",
                "description": "Read the last N lines of a background job's combined stdout/stderr log.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "lines": {"type": "integer", "default": 200},
                    },
                    "required": ["job_id"],
                },
            },
            lambda ctx, **kw: bg_tail(ctx, **kw),
            timeout_sec=30,
        ),
        ToolEntry(
            "bg_list",
            {
                "name": "bg_list",
                "description": "List all background jobs registered for this drive (alive + exited).",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            lambda ctx, **kw: bg_list(ctx, **kw),
            timeout_sec=15,
        ),
        ToolEntry(
            "bg_kill",
            {
                "name": "bg_kill",
                "description": "Kill a background job (taskkill /F /T on Windows, killpg on POSIX) and remove its manifest. Always use this before re-binding the same port.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
            lambda ctx, **kw: bg_kill(ctx, **kw),
            is_code_tool=True,
            timeout_sec=30,
        ),
        ToolEntry(
            "web_fetch",
            {
                "name": "web_fetch",
                "description": (
                    "GET an HTTP(S) URL; stores page + sections under "
                    "`.memory/drive/memory/knowledge/web/pages/` and registers "
                    "external_knowledge_catalog handles. Returns catalog_id + "
                    "preview by default (not full body)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 20000},
                        "intent": {
                            "type": "string",
                            "default": "planner_research",
                        },
                        "register_catalog": {"type": "boolean", "default": True},
                        "extract_sections": {"type": "boolean", "default": True},
                        "include_content": {"type": "boolean", "default": False},
                    },
                    "required": ["url"],
                },
            },
            lambda ctx, **kw: web_fetch(ctx, **kw),
            timeout_sec=60,
        ),
    ]
