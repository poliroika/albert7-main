"""Environment snapshot for prompt injection.

Gathers system/repo/workspace state so the agent does not waste early
rounds on exploratory commands.  Every probe is fail-soft: if a command
is unavailable the field says ``unavailable`` instead of raising.
"""

import logging
import platform
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Low-level probes
# ---------------------------------------------------------------------------


def _run_probe(cmd: list[str], *, cwd: str | None = None) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT,
            cwd=cwd,
        )
        return result.stdout.strip() or result.stderr.strip() or ""
    except FileNotFoundError:
        return "unavailable"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as exc:
        return f"error: {exc}"


def _probe_version(binary: str) -> str:
    return _run_probe([binary, "--version"])


def _probe_git_info(repo_root: Path) -> dict[str, str]:
    cwd = str(repo_root)
    branch = _run_probe(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    sha = _run_probe(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)
    dirty_output = _run_probe(["git", "status", "--porcelain"], cwd=cwd)
    dirty_count = (
        str(len([l for l in dirty_output.splitlines() if l.strip()]))
        if dirty_output not in ("unavailable", "timeout")
        else "unknown"
    )
    return {"branch": branch, "sha": sha, "dirty_files": dirty_count}


def _list_top_level(directory: Path, *, limit: int = 40) -> list[str]:
    if not directory.exists():
        return []
    entries = []
    try:
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith(".") and entry.name not in (".env", ".umbrella"):
                continue
            suffix = "/" if entry.is_dir() else ""
            entries.append(entry.name + suffix)
            if len(entries) >= limit:
                break
    except OSError:
        pass
    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def gather_environment_snapshot(
    repo_root: Path,
    workspace_path: Path | None = None,
) -> dict[str, Any]:
    """Collect a fail-soft environment snapshot."""
    snapshot: dict[str, Any] = {}

    try:
        snapshot["cwd"] = str(Path.cwd())
    except Exception:
        snapshot["cwd"] = "unknown"

    snapshot["repo_root"] = str(repo_root)
    snapshot["workspace_path"] = str(workspace_path) if workspace_path else ""
    snapshot["os"] = f"{platform.system()} {platform.release()}"
    snapshot["python_version"] = platform.python_version()

    snapshot["git"] = _probe_git_info(repo_root)

    snapshot["tool_versions"] = {
        "uv": _probe_version("uv"),
        "node": _probe_version("node"),
        "npm": _probe_version("npm"),
    }

    snapshot["repo_top_level"] = _list_top_level(repo_root)

    if workspace_path and workspace_path.exists():
        snapshot["workspace_top_level"] = _list_top_level(workspace_path)
        snapshot["has_task_main"] = (workspace_path / "TASK_MAIN.md").exists()
        snapshot["has_workspace_toml"] = (workspace_path / "workspace.toml").exists()
        snapshot["has_seed_profile"] = (workspace_path / "seed_profile.toml").exists()
    else:
        snapshot["workspace_top_level"] = []
        snapshot["has_task_main"] = False
        snapshot["has_workspace_toml"] = False
        snapshot["has_seed_profile"] = False

    # Test hints from evals
    evals_dir = repo_root / "umbrella" / "evals"
    if evals_dir.exists():
        toml_files = list(evals_dir.glob("*.toml"))
        snapshot["eval_configs"] = [f.name for f in toml_files[:10]]
    else:
        snapshot["eval_configs"] = []

    # Last known failure summary
    try:
        signals_path = repo_root / ".umbrella" / "memory" / "signals.jsonl"
        if signals_path.exists():
            import json

            lines = signals_path.read_text(encoding="utf-8").strip().split("\n")
            recent_failures = []
            for line in reversed(lines[-20:]):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if float(data.get("strength", 0)) < 0:
                        recent_failures.append(data.get("evidence_summary", "")[:200])
                        if len(recent_failures) >= 3:
                            break
                except Exception:
                    continue
            snapshot["recent_failure_hints"] = recent_failures
        else:
            snapshot["recent_failure_hints"] = []
    except Exception:
        snapshot["recent_failure_hints"] = []

    return snapshot


def render_environment_snapshot_section(
    repo_root: Path,
    workspace_path: Path | None = None,
) -> str:
    """Render a markdown section for prompt injection."""
    try:
        snap = gather_environment_snapshot(repo_root, workspace_path)
    except Exception:
        log.debug("Environment snapshot collection failed", exc_info=True)
        return "_Environment snapshot unavailable._"

    lines: list[str] = []

    git = snap.get("git", {})
    lines.append(f"- OS: {snap.get('os', 'unknown')}")
    lines.append(f"- Python: {snap.get('python_version', 'unknown')}")
    lines.append(
        f"- Git branch: {git.get('branch', 'unknown')} (sha: {git.get('sha', '?')}, dirty files: {git.get('dirty_files', '?')})"
    )

    tools = snap.get("tool_versions", {})
    tool_parts = []
    for name, ver in tools.items():
        short = ver.split("\n")[0][:60] if ver else "unavailable"
        tool_parts.append(f"{name}: {short}")
    if tool_parts:
        lines.append(f"- Tools: {'; '.join(tool_parts)}")

    ws_top = snap.get("workspace_top_level", [])
    if ws_top:
        lines.append(f"- Workspace files: {', '.join(ws_top[:20])}")

    flags = []
    if snap.get("has_task_main"):
        flags.append("TASK_MAIN.md")
    if snap.get("has_workspace_toml"):
        flags.append("workspace.toml")
    if snap.get("has_seed_profile"):
        flags.append("seed_profile.toml")
    if flags:
        lines.append(f"- Workspace config: {', '.join(flags)}")

    failures = snap.get("recent_failure_hints", [])
    if failures:
        lines.append("- Recent failure hints:")
        for hint in failures:
            lines.append(f"  - {hint}")

    return "\n".join(lines) if lines else "_No environment data collected._"
