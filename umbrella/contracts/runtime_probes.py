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

log = logging.getLogger(__name__)

_RUNTIME_CAPABILITIES_FILENAME = "runtime_capabilities.json"
_PROBE_KINDS = frozenset({"command"})
_DESKTOP_GUI_RUNTIME_MARKERS = (
    "tk.tk(",
    "tkinter.tk(",
    "tk(",
    "customtkinter.ctk(",
    "ctk.ctk(",
    "ctk(",
    "qapplication(",
    "qwidget(",
    "qmainwindow(",
    "wx.app(",
    ".show(",
    ".mainloop(",
    ".update(",
)


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
    command_text = _probe_command_text(spec)
    compact = "".join(str(command_text or "").lower().split())
    if any(marker in compact for marker in _DESKTOP_GUI_RUNTIME_MARKERS):
        return None
    return (
        "desktop_gui_runtime probe must exercise real native GUI runtime "
        "(create/update/destroy or show a window/root). Import-only library "
        "checks belong to desktop_gui_headless or a library-specific capability."
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
        if tag == "desktop_gui_runtime" and issue.code == "import_only_proof":
            continue
        return issue.message
    return None


def execute_probe(
    spec: dict[str, Any],
    *,
    workspace_root: Path,
    capability_tag: str = "",
    timeout_sec: float = 5.0,
) -> tuple[bool, str]:
    issue = validate_probe_spec(spec, capability_tag=capability_tag)
    if issue:
        return False, issue
    argv = tuple(str(item) for item in spec.get("command") or ())
    expect_exit = int(spec.get("expect_exit", 0))
    cwd = str(spec.get("cwd") or "").strip()
    run_cwd = workspace_root
    if cwd:
        candidate = (workspace_root / cwd).resolve()
        if workspace_root.resolve() not in candidate.parents and candidate != workspace_root.resolve():
            return False, "probe.cwd must stay inside workspace"
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
        return False, str(exc)
    if result.returncode != expect_exit:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace")[:200]
        return False, f"exit {result.returncode} (expected {expect_exit}){': ' + detail if detail else ''}"
    return True, ""


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
            available, reason = execute_probe(
                probe,
                workspace_root=workspace_root,
                capability_tag=name,
                timeout_sec=float(probe.get("timeout_sec", 5.0)),
            )
            merged[name] = {
                "available": available,
                "source": "probe",
                "reason": reason,
                "probe": probe,
            }
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
