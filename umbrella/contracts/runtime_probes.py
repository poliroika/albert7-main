"""Execute agent-declared capability probes with harness probe semantics."""

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from umbrella.analysis.shell_commands import validate_argv
from umbrella.contracts.capability_declaration import (
    declaration_effective_capabilities,
    load_capability_declaration,
    validate_proof_against_capabilities,
)
from umbrella.contracts.environments import (
    DEFAULT_EXECUTION_ENVIRONMENT_ID,
    resolve_execution_environment,
)

log = logging.getLogger(__name__)

_RUNTIME_CAPABILITIES_FILENAME = "runtime_capabilities.json"
_PROBE_KINDS = frozenset({"command"})
_IMPORT_ONLY_CAPABILITY_PROBE_TAGS = frozenset({
    "desktop_gui_headless",
})
_DESKTOP_GUI_RUNTIME_INTENT = "real_gui_root_lifecycle"


def baseline_runtime_capabilities() -> dict[str, bool]:
    """Universal baseline only: interpreter present and we can spawn subprocesses."""

    return {
        "python": shutil.which(sys.executable) is not None,
        "subprocess": True,
    }


def probe_runtime_capabilities(workspace_root: Path | None = None) -> dict[str, bool]:
    """Backward-compatible alias: baseline only (workspace_root ignored)."""

    _ = workspace_root
    return baseline_runtime_capabilities()


def _probe_command_text(spec: dict[str, Any]) -> str:
    command = spec.get("command") or ()
    if not isinstance(command, (list, tuple)):
        return ""
    return " ".join(str(item) for item in command)


def _validate_desktop_gui_runtime_probe(spec: dict[str, Any]) -> str | None:
    intent = str(spec.get("intent") or "").strip().lower()
    if intent == _DESKTOP_GUI_RUNTIME_INTENT:
        return None
    return (
        "desktop_gui_runtime probe must declare "
        "intent=real_gui_root_lifecycle. Import-only toolkit checks belong to "
        "desktop_gui_headless or a library-specific capability."
    )


def validate_probe_spec(spec: Any, *, capability_tag: str = "") -> str | None:
    if not isinstance(spec, dict):
        return "probe must be an object."
    kind = str(spec.get("kind") or "command").strip().lower()
    if kind not in _PROBE_KINDS:
        return f"unsupported probe kind `{kind}`."
    command = spec.get("command") or ()
    if not isinstance(command, (list, tuple)) or not command:
        return "probe.command must be a non-empty argv array."
    argv = tuple(str(item) for item in command)
    tag = str(capability_tag or "").strip().lower()
    if tag == "desktop_gui_runtime":
        runtime_issue = _validate_desktop_gui_runtime_probe(spec)
        if runtime_issue:
            return runtime_issue
    for issue in validate_argv(argv, shell=bool(spec.get("shell"))):
        if (
            issue.code == "import_only_proof"
            and tag in _IMPORT_ONLY_CAPABILITY_PROBE_TAGS
        ):
            continue
        return issue.message
    return None


def _repo_root_for_workspace(workspace_root: Path) -> Path:
    try:
        parts = workspace_root.resolve().parts
        if "workspaces" in parts:
            idx = len(parts) - 1 - list(reversed(parts)).index("workspaces")
            if idx > 0:
                return Path(*parts[:idx])
    except Exception:
        pass
    return workspace_root.parent.parent


def _python_command_rewritten(argv: tuple[str, ...], python_executable: str) -> tuple[str, ...]:
    if not argv or not python_executable:
        return argv
    first = Path(str(argv[0])).name.lower()
    if first in {"python", "python3", "py", "python.exe", "python3.exe", "py.exe"}:
        return (python_executable, *argv[1:])
    return argv


def execute_probe_result(
    spec: dict[str, Any],
    *,
    workspace_root: Path,
    capability_tag: str = "",
    timeout_sec: float = 5.0,
) -> dict[str, Any]:
    issue = validate_probe_spec(spec, capability_tag=capability_tag)
    env_id = str(
        spec.get("execution_environment_id")
        or spec.get("environment_id")
        or spec.get("env_id")
        or DEFAULT_EXECUTION_ENVIRONMENT_ID
    ).strip()
    repo_root = _repo_root_for_workspace(workspace_root)
    env_record = resolve_execution_environment(
        repo_root=repo_root,
        workspace_root=workspace_root,
        env_id=env_id,
        cwd=workspace_root / str(spec.get("cwd") or "").strip()
        if str(spec.get("cwd") or "").strip()
        else workspace_root,
    )
    if issue:
        return {
            "available": False,
            "reason": issue,
            "execution_environment_id": env_record.env_id,
            "python_executable": env_record.python_executable,
            "cwd": env_record.cwd,
            "env_hash": env_record.env_hash,
            "probe_exit_code": None,
        }
    argv = tuple(str(item) for item in spec.get("command") or ())
    argv = _python_command_rewritten(argv, env_record.python_executable)
    expect_exit = int(spec.get("expect_exit", 0))
    cwd = str(spec.get("cwd") or "").strip()
    run_cwd = workspace_root
    if cwd:
        candidate = (workspace_root / cwd).resolve()
        if workspace_root.resolve() not in candidate.parents and candidate != workspace_root.resolve():
            return {
                "available": False,
                "reason": "probe.cwd must stay inside workspace",
                "execution_environment_id": env_record.env_id,
                "python_executable": env_record.python_executable,
                "cwd": str(candidate),
                "env_hash": env_record.env_hash,
                "probe_exit_code": None,
            }
        run_cwd = candidate
    try:
        result = subprocess.run(
            list(argv),
            cwd=run_cwd,
            capture_output=True,
            timeout=max(1.0, float(timeout_sec)),
            shell=bool(spec.get("shell")),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("capability probe failed: %s", exc, exc_info=True)
        return {
            "available": False,
            "reason": str(exc),
            "execution_environment_id": env_record.env_id,
            "python_executable": env_record.python_executable,
            "cwd": str(run_cwd),
            "env_hash": env_record.env_hash,
            "probe_command": list(argv),
            "probe_exit_code": None,
        }
    reason = ""
    available = result.returncode == expect_exit
    if not available:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace")[:200]
        reason = f"exit {result.returncode} (expected {expect_exit}){': ' + detail if detail else ''}"
    return {
        "available": available,
        "reason": reason,
        "execution_environment_id": env_record.env_id,
        "python_executable": env_record.python_executable,
        "cwd": str(run_cwd),
        "env_hash": env_record.env_hash,
        "probe_command": list(argv),
        "probe_exit_code": int(result.returncode),
    }


def execute_probe(
    spec: dict[str, Any],
    *,
    workspace_root: Path,
    capability_tag: str = "",
    timeout_sec: float = 5.0,
) -> tuple[bool, str]:
    result = execute_probe_result(
        spec,
        workspace_root=workspace_root,
        capability_tag=capability_tag,
        timeout_sec=timeout_sec,
    )
    return bool(result.get("available")), str(result.get("reason") or "")


def run_capability_probes(
    capabilities: dict[str, Any],
    *,
    workspace_root: Path,
) -> dict[str, dict[str, Any]]:
    """Run optional per-capability probe specs; return normalized capability entries."""

    merged: dict[str, dict[str, Any]] = {}
    for tag, raw in capabilities.items():
        name = str(tag).strip()
        if not name:
            continue
        if isinstance(raw, bool):
            merged[name] = {
                "available": raw,
                "source": "declared",
                "reason": "",
            }
            continue
        if not isinstance(raw, dict):
            continue
        probe = raw.get("probe")
        if isinstance(probe, dict):
            probe_result = execute_probe_result(
                probe,
                workspace_root=workspace_root,
                capability_tag=name,
                timeout_sec=float(probe.get("timeout_sec", 5.0)),
            )
            merged[name] = {
                "available": bool(probe_result.get("available")),
                "source": "probe",
                "reason": str(probe_result.get("reason") or ""),
                "probe": probe,
                "execution_environment_id": str(
                    probe_result.get("execution_environment_id") or ""
                ),
                "python_executable": str(probe_result.get("python_executable") or ""),
                "cwd": str(probe_result.get("cwd") or ""),
                "env_hash": str(probe_result.get("env_hash") or ""),
            }
            if probe_result.get("probe_command"):
                merged[name]["probe_command"] = probe_result["probe_command"]
            if probe_result.get("probe_exit_code") is not None:
                merged[name]["probe_exit_code"] = int(probe_result["probe_exit_code"])
            continue
        merged[name] = {
            "available": bool(raw.get("available")),
            "source": str(raw.get("source") or "declared"),
            "reason": str(raw.get("reason") or ""),
        }
    return merged


def probe_requested_capabilities(
    workspace_root: Path | None,
    requests: Any,
    *,
    baseline: dict[str, bool] | None = None,
) -> dict[str, bool]:
    """Deprecated: string probe_requests are ignored; use capability probes instead."""

    _ = requests
    return dict(baseline or baseline_runtime_capabilities())


def persist_runtime_capabilities(drive_root: Path, caps: dict[str, bool]) -> Path:
    """Write audit cache only; SoT for gates is capability_declaration.json."""

    state_dir = drive_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / _RUNTIME_CAPABILITIES_FILENAME
    path.write_text(
        json.dumps({"audit": caps, "source": "probe_baseline"}, indent=2),
        encoding="utf-8",
    )
    return path


def load_runtime_capabilities(drive_root: Path | None) -> dict[str, bool]:
    if drive_root is None:
        return {}
    path = drive_root / "state" / _RUNTIME_CAPABILITIES_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("audit"), dict):
        return {str(k): bool(v) for k, v in payload["audit"].items()}
    return {str(k): bool(v) for k, v in payload.items()}


def effective_runtime_capabilities(drive_root: Path | None) -> dict[str, bool]:
    declaration = load_capability_declaration(drive_root)
    if declaration is not None:
        return declaration_effective_capabilities(declaration)
    probed = load_runtime_capabilities(drive_root)
    if probed:
        return probed
    return baseline_runtime_capabilities()


def proof_requires_capability(proof: Any, caps: dict[str, bool]) -> str | None:
    return validate_proof_against_capabilities(proof, caps)
