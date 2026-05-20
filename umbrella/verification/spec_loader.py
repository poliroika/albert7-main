"""Load verification specs from ``workspace.toml`` or auto-detect them."""

import ast
import logging
import re
import shlex
import sys
from pathlib import Path
from typing import Any  # noqa: F401 — used by load_verification_meta

try:  # Python 3.11+
    import tomllib as _toml  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - fallback for <3.11
    import tomli as _toml  # type: ignore[no-redef]

from umbrella.verification.models import VerificationStep, VerificationStepKind

log = logging.getLogger(__name__)


# Fields that are universally meaningful regardless of step ``kind``.
_COMMON_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "type",
        "name",
        "optional",
        "timeout",
        "timeout_seconds",
        "env",
    }
)

# Per-kind whitelist of additional fields. Unknown keys for a given kind get
# a warning but do not abort parsing — TOML evolves and we'd rather degrade
# gracefully than mass-fail valid specs.
_KIND_SPECIFIC_FIELDS: dict[VerificationStepKind, frozenset[str]] = {
    VerificationStepKind.SHELL: frozenset({"command"}),
    VerificationStepKind.FILE_EXISTS: frozenset({"path"}),
    VerificationStepKind.IMPORT_CHECK: frozenset({"module", "command"}),
    VerificationStepKind.HTTP_BOOT: frozenset(
        {
            "command",
            "health_url",
            "startup_timeout",
            "startup_timeout_seconds",
            "expect_status",
            "expect_json_keys",
            "expect_json_path",
            "expect_min_items",
            "extra_health_urls",
        }
    ),
    VerificationStepKind.BEHAVIORAL_HTTP: frozenset(
        {
            "command",
            "health_url",
            "startup_timeout",
            "startup_timeout_seconds",
            "request_url",
            "request_payloads",
            "expect_status",
            "expect_json_keys",
            "expect_json_path",
            "expect_min_items",
            "extra_health_urls",
        }
    ),
    VerificationStepKind.INPUT_SENSITIVITY: frozenset(
        {
            "command",
            "health_url",
            "startup_timeout",
            "startup_timeout_seconds",
            "request_url",
            "request_payloads",
        }
    ),
    VerificationStepKind.PPTX_DIFF: frozenset({"output_glob"}),
    VerificationStepKind.SOURCE_POLICY: frozenset({"command"}),
}


class VerificationSpecError(ValueError):
    """Raised when an explicit verification spec exists but is not loadable."""

    def __init__(self, path: Path, message: str):
        self.path = Path(path)
        self.message = str(message)
        super().__init__(f"{self.path}: {self.message}")

    def to_payload(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "message": self.message,
        }


def load_verification_meta(workspace_path: str | Path) -> dict[str, Any]:
    """Return flags from ``[verification]``.

    Defaults are intentionally conservative for output quality:
    - ``skip_test_quality`` defaults to ``False`` (pytest steps get semantic guard)
    - ``skip_behavioral`` defaults to ``False`` (behavioral depth still enforced
      for web-like tasks unless explicitly disabled)
    """

    workspace_path = Path(workspace_path)
    for rel in ("workspace.toml", "verification.toml"):
        path = workspace_path / rel
        if not path.exists():
            continue
        try:
            with path.open("rb") as fh:
                data: dict[str, Any] = _toml.load(fh)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to parse %s: %s", path, exc)
            continue
        ver = data.get("verification") or {}
        if not isinstance(ver, dict):
            continue
        skip_tq = bool(ver.get("skip_test_quality", False))
        if bool(ver.get("enforce_test_quality", False)):
            skip_tq = False
        return {
            "skip_test_quality": skip_tq,
            "skip_behavioral": bool(ver.get("skip_behavioral", False)),
        }
    return {"skip_test_quality": False, "skip_behavioral": False}


def non_template_pptx_paths(workspace_path: str | Path) -> list[Path]:
    """``.pptx`` files that count as generated output (excludes ``template*.pptx``).

    Matches the filter used by ``pptx_diff`` in ``runner._run_pptx_diff_step`` so
    autodetect does not add a ``pptx_diff`` step when only a design template exists.
    """

    workspace_path = Path(workspace_path)
    return [
        p
        for p in workspace_path.glob("*.pptx")
        if p.is_file() and not p.name.lower().startswith("template")
    ]


def format_workspace_verification_digest(workspace_path: str | Path) -> str:
    """Short human-readable summary of the verification spec for LLM context."""

    path = Path(workspace_path)
    if not path.is_dir():
        return ""
    declared = (path / "workspace.toml").exists() or (
        path / "verification.toml"
    ).exists()
    try:
        steps = load_verification_spec(path)
    except Exception:  # noqa: BLE001
        log.debug("format_workspace_verification_digest: load failed", exc_info=True)
        return ""
    lines = [
        "[WORKSPACE_VERIFICATION_DIGEST]",
        f"workspace_declaration_present (workspace.toml or verification.toml): {declared}",
        f"resolved_step_count: {len(steps)}",
    ]
    for step in steps[:24]:
        lines.append(f"  - kind={step.kind.value} name={step.name}")
    if len(steps) > 24:
        lines.append(f"  … ({len(steps) - 24} more steps omitted)")
    lines.append(
        "Commands from `run_workspace_command` run with cwd = workspace root; "
        "do not prefix shell scripts with `cd workspaces/<id>`."
    )
    lines.append("[END_WORKSPACE_VERIFICATION_DIGEST]")
    return "\n".join(lines)


def load_verification_spec(workspace_path: str | Path) -> list[VerificationStep]:
    """Return the list of verification steps for ``workspace_path``.

    Order of precedence:
    1. ``workspace.toml`` ``[verification]`` section (explicit spec).
    2. ``verification.toml`` sibling file (explicit spec).
    3. Auto-detected defaults based on workspace contents.

    Explicit specs are honoured, but safety-critical local tests are
    appended when the workspace already contains a test suite. This is
    intentionally stronger than a pure "what TOML says is what runs"
    contract: an autonomous agent may write weak verification by mistake,
    but existing tests are concrete acceptance evidence and must not be
    silently bypassed.

    The autodetect path (no explicit spec) does still emit a smoke
    step as a *fallback*, because in that case there is no human or
    agent intent to respect — we have to guess something useful.
    """

    workspace_path = Path(workspace_path)

    explicit = _load_from_workspace_toml(workspace_path)
    if explicit is not None:
        return _augment_explicit_steps(workspace_path, explicit)

    explicit = _load_from_verification_toml(workspace_path)
    if explicit is not None:
        return _augment_explicit_steps(workspace_path, explicit)

    return autodetect_steps(workspace_path)


def _augment_explicit_steps(
    workspace_path: Path,
    steps: list[VerificationStep],
) -> list[VerificationStep]:
    meta = load_verification_meta(workspace_path)
    augmented = list(steps)
    pytest_step = _autodetect_pytest_step(workspace_path)
    if (
        not bool(meta.get("skip_test_quality"))
        and pytest_step is not None
        and not _has_pytest_step(augmented)
    ):
        augmented.append(pytest_step)
    return augmented


def _has_pytest_step(steps: list[VerificationStep]) -> bool:
    for step in steps:
        command = " ".join(step.command or []).lower()
        name = (step.name or "").lower()
        if "pytest" in command or "pytest" in name:
            return True
    return False


def _should_append_explicit_smoke(
    steps: list[VerificationStep],
    smoke: VerificationStep,
) -> bool:
    command_text = " ".join(smoke.command or [])
    if any(" ".join(step.command or []) == command_text for step in steps):
        return False
    has_behavioural = any(
        step.kind
        in {
            VerificationStepKind.HTTP_BOOT,
            VerificationStepKind.BEHAVIORAL_HTTP,
            VerificationStepKind.PPTX_DIFF,
            VerificationStepKind.INPUT_SENSITIVITY,
        }
        for step in steps
    )
    return not has_behavioural


def _load_from_workspace_toml(workspace_path: Path) -> list[VerificationStep] | None:
    path = workspace_path / "workspace.toml"
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            data: dict[str, Any] = _toml.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to parse %s: %s", path, exc)
        raise VerificationSpecError(path, f"Invalid TOML: {exc}") from exc

    section = data.get("verification")
    if not section:
        return None
    if not isinstance(section, dict):
        log.warning("verification section in %s must be a table", path)
        return None

    inline_steps = _parse_steps(section, source=str(path))

    # Honour ``steps_file = "verification.toml"`` (or any other sibling
    # relative path). Previously this key was silently ignored, which led
    # to agents declaring an external spec file that was never read.
    external_steps: list[VerificationStep] = []
    steps_file_raw = section.get("steps_file")
    if isinstance(steps_file_raw, str) and steps_file_raw.strip():
        external_steps = _load_steps_from_external_file(
            workspace_path, steps_file_raw.strip(), declared_in=path
        )
    elif steps_file_raw is not None:
        log.warning(
            "verification.steps_file in %s must be a string (got %s); ignored",
            path,
            type(steps_file_raw).__name__,
        )

    merged = list(inline_steps) + list(external_steps)
    if not merged:
        # No steps anywhere — fall through to the next precedence layer so
        # the caller can try ``verification.toml`` or autodetect.
        return None
    return merged


def _load_steps_from_external_file(
    workspace_path: Path, rel: str, *, declared_in: Path
) -> list[VerificationStep]:
    """Resolve and parse a ``steps_file`` reference from ``workspace.toml``.

    Path is resolved relative to ``workspace_path``. We refuse traversal
    outside the workspace because the agent may write arbitrary strings
    here and we don't want to read ``../../etc/passwd``-style targets.
    """

    candidate = (workspace_path / rel).resolve()
    try:
        candidate.relative_to(workspace_path.resolve())
    except ValueError:
        log.warning(
            "verification.steps_file in %s escapes workspace: %r; ignored",
            declared_in,
            rel,
        )
        return []
    if not candidate.exists():
        log.warning(
            "verification.steps_file %s referenced from %s does not exist",
            candidate,
            declared_in,
        )
        return []
    try:
        with candidate.open("rb") as fh:
            data: dict[str, Any] = _toml.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to parse %s: %s", candidate, exc)
        raise VerificationSpecError(candidate, f"Invalid TOML: {exc}") from exc

    # The external file may either be a bare list of steps (top-level
    # ``steps = [...]``) or a wrapped ``[verification]`` section, depending
    # on whether the operator copied a ``workspace.toml`` snippet.
    section = (
        data.get("verification") if isinstance(data.get("verification"), dict) else data
    )
    return _parse_steps(section, source=str(candidate))


def _load_from_verification_toml(workspace_path: Path) -> list[VerificationStep] | None:
    path = workspace_path / "verification.toml"
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            data: dict[str, Any] = _toml.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to parse %s: %s", path, exc)
        raise VerificationSpecError(path, f"Invalid TOML: {exc}") from exc

    return _parse_steps(data, source=str(path))


def _parse_steps(section: dict[str, Any], *, source: str) -> list[VerificationStep]:
    raw_steps = section.get("steps") or []
    if not isinstance(raw_steps, list):
        log.warning("verification.steps in %s must be a list", source)
        return []

    steps: list[VerificationStep] = []
    skipped_non_dict = 0
    for idx, raw in enumerate(raw_steps):
        # Friendly format: ``steps = ["uv run pytest", "uv run python web.py"]``
        # Each string is parsed as a shell command and treated as a SHELL step.
        # This mirrors how Ouroboros (and many users) intuitively write the
        # spec; without it the whole [verification] section was silently
        # ignored and the run reported as ``verification: skipped``.
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                command = shlex.split(stripped)
            except ValueError as exc:
                log.warning(
                    "verification step #%d in %s: cannot tokenize %r (%s)",
                    idx,
                    source,
                    stripped,
                    exc,
                )
                continue
            if not command:
                continue
            steps.append(
                VerificationStep(
                    kind=VerificationStepKind.SHELL,
                    name=_default_name(VerificationStepKind.SHELL, command, idx),
                    command=command,
                    timeout_seconds=180,
                )
            )
            continue
        if not isinstance(raw, dict):
            skipped_non_dict += 1
            log.warning(
                "verification step #%d in %s is not a table or string (got %s)",
                idx,
                source,
                type(raw).__name__,
            )
            continue
        kind_raw = str(raw.get("kind") or raw.get("type") or "shell").strip().lower()
        if kind_raw == "command":
            kind_raw = "shell"
        try:
            kind = VerificationStepKind(kind_raw)
        except ValueError:
            log.warning(
                "Unknown verification step kind %r in %s; valid kinds: %s",
                kind_raw,
                source,
                ", ".join(k.value for k in VerificationStepKind),
            )
            continue

        _warn_unknown_fields(raw, kind=kind, source=source, idx=idx)

        command = raw.get("command") or []
        if isinstance(command, str):
            try:
                command = shlex.split(command)
            except ValueError as exc:
                log.warning(
                    "verification step %r in %s: cannot tokenize command %r (%s)",
                    raw.get("name") or idx,
                    source,
                    command,
                    exc,
                )
                command = [command]
        elif isinstance(command, dict):
            raise VerificationSpecError(
                Path(source),
                f"step #{idx} ({kind.value}): 'command' must be string or list[str], "
                f"got table {command!r}",
            )
        command = [str(c) for c in command]

        name = str(raw.get("name") or _default_name(kind, command, idx))

        # ``path`` validation. For file_exists we now accept a list so the
        # common "check N files" pattern doesn't silently degrade into
        # ``str(['a', 'b'])`` and produce an unfindable literal path.
        paths_for_kind = _coerce_paths_field(
            raw.get("path"), kind=kind, name=name, source=source, idx=idx
        )

        timeout = _coerce_timeout(
            raw.get("timeout_seconds"), raw.get("timeout"), default=180
        )
        startup = _coerce_timeout(
            raw.get("startup_timeout_seconds"),
            raw.get("startup_timeout"),
            default=30,
        )
        env_raw = raw.get("env") or {}
        env = (
            {str(k): str(v) for k, v in env_raw.items()}
            if isinstance(env_raw, dict)
            else {}
        )

        ek_raw = raw.get("expect_json_keys") or []
        if isinstance(ek_raw, str):
            expect_json_keys = [ek_raw] if ek_raw.strip() else []
        else:
            expect_json_keys = (
                [str(x) for x in ek_raw] if isinstance(ek_raw, list) else []
            )

        ep_raw = raw.get("expect_json_path") or {}
        expect_json_path = (
            {str(k): str(v) for k, v in ep_raw.items()}
            if isinstance(ep_raw, dict)
            else {}
        )

        em_raw = raw.get("expect_min_items") or {}
        expect_min_items: dict[str, int] = {}
        if isinstance(em_raw, dict):
            for k, v in em_raw.items():
                try:
                    expect_min_items[str(k)] = int(v)
                except (TypeError, ValueError):
                    log.warning(
                        "expect_min_items: skip invalid entry %r=%r in %s", k, v, source
                    )

        ex_urls = raw.get("extra_health_urls") or []
        if isinstance(ex_urls, str):
            extra_health_urls = [ex_urls] if ex_urls.strip() else []
        else:
            extra_health_urls = (
                [str(u) for u in ex_urls] if isinstance(ex_urls, list) else []
            )

        expect_status = 200
        if raw.get("expect_status") is not None:
            try:
                expect_status = int(raw.get("expect_status"))
            except (TypeError, ValueError):
                expect_status = 200

        # ``paths_for_kind`` is always a list. For non-FILE_EXISTS kinds it
        # is either [single_path] or []. For FILE_EXISTS it can be N entries
        # (the syntactic-sugar expansion).
        if kind == VerificationStepKind.FILE_EXISTS and len(paths_for_kind) > 1:
            for sub_idx, sub_path in enumerate(paths_for_kind):
                sub_name = (
                    f"{name}__{sub_idx}" if name else _default_name(kind, command, idx)
                )
                steps.append(
                    VerificationStep(
                        kind=kind,
                        name=sub_name,
                        command=command,
                        timeout_seconds=timeout,
                        health_url=str(raw.get("health_url") or ""),
                        startup_timeout_seconds=startup,
                        module=str(raw.get("module") or ""),
                        path=sub_path,
                        optional=bool(raw.get("optional", False)),
                        env=env,
                        expect_status=expect_status,
                        expect_json_keys=expect_json_keys,
                        expect_json_path=expect_json_path,
                        expect_min_items=expect_min_items,
                        extra_health_urls=extra_health_urls,
                        request_url=str(raw.get("request_url") or ""),
                        request_payloads=list(raw.get("request_payloads") or [])
                        if isinstance(raw.get("request_payloads") or [], list)
                        else [],
                        output_glob=str(raw.get("output_glob") or ""),
                    )
                )
            continue

        single_path = paths_for_kind[0] if paths_for_kind else ""
        steps.append(
            VerificationStep(
                kind=kind,
                name=name,
                command=command,
                timeout_seconds=timeout,
                health_url=str(raw.get("health_url") or ""),
                startup_timeout_seconds=startup,
                module=str(raw.get("module") or ""),
                path=single_path,
                optional=bool(raw.get("optional", False)),
                env=env,
                expect_status=expect_status,
                expect_json_keys=expect_json_keys,
                expect_json_path=expect_json_path,
                expect_min_items=expect_min_items,
                extra_health_urls=extra_health_urls,
                request_url=str(raw.get("request_url") or ""),
                request_payloads=list(raw.get("request_payloads") or [])
                if isinstance(raw.get("request_payloads") or [], list)
                else [],
                output_glob=str(raw.get("output_glob") or ""),
            )
        )
    if not steps and raw_steps:
        log.error(
            "verification.steps in %s declared %d entries but none parsed; "
            "verification will be SKIPPED. Use either "
            '[[verification.steps]] tables or steps = ["<shell command>"].',
            source,
            len(raw_steps),
        )
    return steps


def _warn_unknown_fields(
    raw: dict[str, Any],
    *,
    kind: VerificationStepKind,
    source: str,
    idx: int,
) -> None:
    """Log a warning for keys not recognised for the given step kind.

    We intentionally do **not** abort parsing — unknown keys may simply be
    forward-compat additions. The warning makes the silent-mismatch class
    of bugs (e.g. ``dir = "src/foo"`` for ``file_exists`` when only
    ``path`` is honoured) visible in logs and remediation feedback.
    """

    allowed = _COMMON_FIELDS | _KIND_SPECIFIC_FIELDS.get(kind, frozenset())
    unknown = [k for k in raw.keys() if k not in allowed]
    if unknown:
        log.warning(
            "verification step #%d (%s) in %s: unknown fields %s; "
            "allowed for kind=%s: %s",
            idx,
            raw.get("name") or kind.value,
            source,
            sorted(unknown),
            kind.value,
            sorted(allowed),
        )


def _coerce_paths_field(
    raw_path: Any,
    *,
    kind: VerificationStepKind,
    name: str,
    source: str,
    idx: int,
) -> list[str]:
    """Normalise the ``path`` field into a list of non-empty strings.

    Rules:
    - ``None`` / missing -> ``[]`` (caller decides whether the kind needs it).
    - ``str`` -> ``[str]`` (unless empty -> ``[]``). A leading ``[`` is a
      strong hint that the agent wrote ``path = "['a', 'b']"`` as a string
      instead of a TOML list; we log a clear warning in that case.
    - ``list`` of strings -> each entry. Empty entries are skipped.
    - Anything else (dict, number, ...) -> ``VerificationSpecError``.
    """

    if raw_path is None:
        return []
    if isinstance(raw_path, str):
        stripped = raw_path.strip()
        if not stripped:
            return []
        if stripped.startswith("[") and "," in stripped:
            log.warning(
                "verification step #%d (%s) in %s: 'path' looks like a "
                'stringified list (%r); use TOML syntax `path = ["a", "b"]` '
                "instead. The literal string will be used as-is and will most "
                "likely fail.",
                idx,
                name,
                source,
                raw_path,
            )
        return [stripped]
    if isinstance(raw_path, (list, tuple)):
        if kind != VerificationStepKind.FILE_EXISTS:
            raise VerificationSpecError(
                Path(source),
                f"step #{idx} ({kind.value}, name={name!r}): 'path' must be a "
                f"single string for this kind; only 'file_exists' supports a "
                f"list of paths (as syntactic sugar for N separate checks).",
            )
        out: list[str] = []
        for entry in raw_path:
            if entry is None:
                continue
            if not isinstance(entry, str):
                raise VerificationSpecError(
                    Path(source),
                    f"step #{idx} ({kind.value}, name={name!r}): every "
                    f"element of 'path' must be a string, got "
                    f"{type(entry).__name__} ({entry!r}).",
                )
            stripped = entry.strip()
            if stripped:
                out.append(stripped)
        if not out:
            return []
        return out
    raise VerificationSpecError(
        Path(source),
        f"step #{idx} ({kind.value}, name={name!r}): 'path' must be string "
        f"or list[str], got {type(raw_path).__name__} ({raw_path!r}).",
    )


def _default_name(kind: VerificationStepKind, command: list[str], idx: int) -> str:
    if command:
        tail = " ".join(command[-2:]) if len(command) >= 2 else command[0]
        return f"{kind.value}:{tail}"
    return f"{kind.value}#{idx}"


def _coerce_timeout(primary: Any, secondary: Any, *, default: int) -> int:
    """Return a non-negative int timeout, honouring explicit ``0`` values.

    Using ``raw.get(...) or default`` collapses ``0`` to ``default`` because
    ``0`` is falsy in Python. Operators and tests may legitimately want to
    express "no wait" via ``timeout_seconds = 0``, so we need the full
    ``is not None`` check to distinguish "unset" from "zero".
    """

    for candidate in (primary, secondary):
        if candidate is None:
            continue
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        return max(0, value)
    return default


def _looks_like_web_entrypoint(path: Path) -> bool:
    """Heuristically decide whether *path* is a runnable web app entrypoint."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    haystack = text.lower()
    return any(
        marker in haystack
        for marker in (
            "fastapi(",
            "uvicorn.run(",
            "flask(",
            "app = fastapi",
            "streamlit",
        )
    )


def _detect_health_url(path: Path) -> str:
    """Return a best-effort health URL for a runnable web entrypoint."""

    port = 8080
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""

    port_match = re.search(r"port\s*=\s*(\d{2,5})", text, re.IGNORECASE)
    if port_match:
        try:
            port = int(port_match.group(1))
        except ValueError:
            port = 8080

    has_health = bool(re.search(r'@app\.(?:get|route)\(\s*[\'"]/health[\'"]', text))
    suffix = "/health" if has_health else "/"
    return f"http://127.0.0.1:{port}{suffix}"


def _python_command(workspace_path: Path) -> list[str]:
    venv_py = (
        workspace_path
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    if venv_py.exists():
        return [str(venv_py)]
    if (workspace_path / "pyproject.toml").exists():
        return ["uv", "run", "python"]
    return [sys.executable]


def _autodetect_http_step(workspace_path: Path) -> VerificationStep | None:
    """Return a best-effort HTTP boot step for common workspace layouts."""

    candidates = [
        ("web_server.py", workspace_path / "web_server.py"),
        ("app.py", workspace_path / "app.py"),
        ("main.py", workspace_path / "main.py"),
        ("src/app/main.py", workspace_path / "src" / "app" / "main.py"),
        ("src/main.py", workspace_path / "src" / "main.py"),
    ]
    for rel, path in candidates:
        if not path.exists() or not path.is_file():
            continue
        if rel != "web_server.py" and not _looks_like_web_entrypoint(path):
            continue
        return VerificationStep(
            kind=VerificationStepKind.HTTP_BOOT,
            name=f"http_boot:{rel}",
            command=[*_python_command(workspace_path), rel],
            health_url=_detect_health_url(path),
            startup_timeout_seconds=25,
        )
    return None


def _route_path_from_decorator(node: ast.AST, methods: set[str]) -> str:
    if not isinstance(node, ast.Call):
        return ""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return ""
    if func.attr.lower() not in methods:
        return ""
    if not node.args:
        return ""
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return ""


def _annotation_name(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
    return ""


def _basemodel_classes(tree: ast.AST) -> dict[str, list[tuple[str, str]]]:
    models: dict[str, list[tuple[str, str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {_annotation_name(base).split(".")[-1] for base in node.bases}
        if "BaseModel" not in base_names:
            continue
        fields: list[tuple[str, str]] = []
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.append((item.target.id, _annotation_name(item.annotation)))
        if fields:
            models[node.name] = fields
    return models


def _payload_value(field_name: str, annotation: str, variant: str) -> Any:
    ann = annotation.lower()
    field = field_name.lower()
    text = f"{variant} verification input"
    if "list" in ann or ann.startswith("list[") or ann.startswith("typing.list"):
        if "str" in ann or any(k in field for k in ("name", "tag", "personality")):
            return [text]
        return []
    if "dict" in ann or "mapping" in ann:
        return {"input": text}
    if "bool" in ann:
        return variant == "alpha"
    if "float" in ann:
        return 1.25 if variant == "alpha" else 2.5
    if "int" in ann:
        return 1 if variant == "alpha" else 2
    return text


def _payloads_for_model(
    fields: list[tuple[str, str]]
) -> list[dict[str, Any]] | None:
    if not fields:
        return None
    alpha = {
        name: _payload_value(name, annotation, "alpha")
        for name, annotation in fields
    }
    beta = {
        name: _payload_value(name, annotation, "beta")
        for name, annotation in fields
    }
    if alpha == beta:
        return None
    return [alpha, beta]


def _payloads_for_endpoint_args(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    models: dict[str, list[tuple[str, str]]],
) -> tuple[list[dict[str, Any]], int] | None:
    for arg in func.args.args:
        if arg.arg in {"self", "request"} and arg.annotation is None:
            continue
        annotation = _annotation_name(arg.annotation)
        model_name = annotation.split(".")[-1]
        if model_name in models:
            payloads = _payloads_for_model(models[model_name])
            return (payloads, 0) if payloads is not None else None
        if any(token in annotation.lower() for token in ("dict", "mapping", "any")):
            return [
                {"input": "alpha verification input"},
                {"input": "beta verification input"},
            ], 20
    return None


def _behavioral_route_priority(path: str) -> tuple[int, str]:
    lowered = path.lower()
    if lowered in {"/generate", "/api/generate"}:
        return (0, lowered)
    for idx, marker in enumerate(
        ("/create", "/chat", "/message", "/ask", "/complete", "/action", "/turn")
    ):
        if marker in lowered:
            return (idx + 1, lowered)
    return (50, lowered)


def _autodetect_fastapi_behavioral_step(
    entry_path: Path, http_step: VerificationStep
) -> VerificationStep | None:
    """Choose a real POST route for behavioral HTTP probing.

    Older autodetect unconditionally required ``POST /generate`` for every
    web app. That is correct for text-generation services, but harmful for
    games, dashboards, CRMs, and other domain APIs: the agent then starts
    adding verifier-only endpoints. Prefer an existing no-path-param FastAPI
    route and derive payloads from its Pydantic request model.
    """

    try:
        tree = ast.parse(entry_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    models = _basemodel_classes(tree)
    candidates: list[tuple[tuple[int, int, str], str, list[dict[str, Any]]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        route = ""
        for decorator in node.decorator_list:
            route = _route_path_from_decorator(decorator, {"post"})
            if route:
                break
        if not route or "{" in route or "}" in route:
            continue
        lowered = route.lower()
        if any(skip in lowered for skip in ("/health", "/metrics", "/docs")):
            continue
        payload_info = _payloads_for_endpoint_args(node, models)
        if payload_info is None:
            continue
        payloads, payload_rank = payload_info
        route_rank, route_name = _behavioral_route_priority(route)
        candidates.append(((payload_rank, route_rank, route_name), route, payloads))
    if not candidates:
        return None
    _priority, route, payloads = sorted(candidates, key=lambda item: item[0])[0]
    base = http_step.health_url.rsplit("/", 1)[0]
    return VerificationStep(
        kind=VerificationStepKind.BEHAVIORAL_HTTP,
        name=f"behavioral_http:{route}",
        command=list(http_step.command),
        health_url=http_step.health_url,
        startup_timeout_seconds=http_step.startup_timeout_seconds,
        request_url=f"{base}{route}",
        request_payloads=payloads,
        optional=False,
    )


def _autodetect_smoke_shell_step(workspace_path: Path) -> VerificationStep | None:
    """Return a SHELL step that actually *runs* the workspace's entrypoint.

    Goal: if no other behavioural step exists, we still want one real
    end-to-end execution so verification proves the project actually
    *works*, not just imports cleanly. The user's API keys (from
    ``.env`` or process env) are merged into the run env so calls to
    OpenAI / Anthropic / etc. land for real.

    Selection order:

    1. ``main.py`` / ``app.py`` / ``cli.py`` / ``run.py`` at workspace
       root — preferred because the agent typically writes one of
       those as the project entrypoint.
    2. ``src/main.py`` for src-layout Python projects.
    3. ``package.json`` with a ``scripts.start`` key — Node projects.

    Returns ``None`` when nothing runnable is found; the existing
    autodetect (``import_check`` / ``http_boot`` / etc.) stays in
    charge in that case.
    """
    candidates: list[tuple[Path, list[str], str]] = []
    for fname in ("main.py", "app.py", "cli.py", "run.py"):
        path = workspace_path / fname
        if path.is_file() and not _looks_like_web_entrypoint(path):
            candidates.append(
                (
                    path,
                    [*_python_command(workspace_path), fname],
                    f"smoke_run:{fname}",
                )
            )
    src_main = workspace_path / "src" / "main.py"
    if src_main.is_file() and not _looks_like_web_entrypoint(src_main):
        candidates.append(
            (
                src_main,
                [*_python_command(workspace_path), "src/main.py"],
                "smoke_run:src/main.py",
            )
        )
    package_json = workspace_path / "package.json"
    if package_json.is_file():
        try:
            import json as _json_mod

            data = _json_mod.loads(package_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                scripts = (
                    data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
                )
                if scripts.get("start"):
                    candidates.append(
                        (
                            package_json,
                            ["npm", "run", "start", "--silent"],
                            "smoke_run:npm-start",
                        )
                    )
        except Exception:
            log.debug("smoke autodetect: failed to parse package.json", exc_info=True)

    if not candidates:
        return None
    _path, command, name = candidates[0]
    return VerificationStep(
        kind=VerificationStepKind.SHELL,
        name=name,
        command=command,
        # 60s is enough for most CLI runs; web-app entrypoints already
        # have ``http_boot`` so we keep this short to avoid hung loops.
        timeout_seconds=60,
        optional=False,
    )


def _autodetect_pytest_step(workspace_path: Path) -> VerificationStep | None:
    for candidate in ("test_smoke.py", "tests", "test"):
        target = workspace_path / candidate
        if target.exists():
            return VerificationStep(
                kind=VerificationStepKind.SHELL,
                name=f"pytest:{candidate}",
                command=[
                    *_python_command(workspace_path),
                    "-m",
                    "pytest",
                    candidate,
                    "-q",
                ],
                timeout_seconds=240,
            )
    return None


def autodetect_steps(workspace_path: Path) -> list[VerificationStep]:
    """Derive a reasonable default spec when nothing is declared."""

    workspace_path = Path(workspace_path)
    steps: list[VerificationStep] = []

    pytest_step = _autodetect_pytest_step(workspace_path)
    if pytest_step is not None:
        steps.append(pytest_step)

    http_step = _autodetect_http_step(workspace_path)
    if http_step is not None:
        steps.append(http_step)
    elif (workspace_path / "app.py").exists():
        steps.append(
            VerificationStep(
                kind=VerificationStepKind.IMPORT_CHECK,
                name="import app",
                command=[*_python_command(workspace_path), "-c", "import app"],
                timeout_seconds=30,
            )
        )

    main_py = workspace_path / "main.py"
    if main_py.exists() and http_step is None:
        steps.append(
            VerificationStep(
                kind=VerificationStepKind.IMPORT_CHECK,
                name="import main",
                command=[*_python_command(workspace_path), "-c", "import main"],
                timeout_seconds=30,
                optional=False,
            )
        )

    if http_step is not None:
        entry_rel = http_step.name.split(":", 1)[-1]
        behavioral = _autodetect_fastapi_behavioral_step(
            workspace_path / entry_rel,
            http_step,
        )
        if behavioral is not None:
            steps.append(behavioral)

    # Require at least one non-template .pptx (generated artifact). Template-only
    # trees should not fail pptx_diff before any codegen has run.
    if non_template_pptx_paths(workspace_path):
        steps.append(
            VerificationStep(
                kind=VerificationStepKind.PPTX_DIFF,
                name="pptx_diff",
                output_glob="*.pptx",
                optional=False,
                timeout_seconds=30,
            )
        )

    # Always end with a real end-to-end smoke run if (a) we don't
    # already have a behavioural step (http_boot covers web apps,
    # pytest covers test suites, pptx_diff covers slide gen) and (b)
    # there is a concrete entrypoint we can actually invoke. This is
    # what the operator usually means by "verified" — the project
    # really runs, not just imports.
    has_behavioural = any(
        step.kind
        in {
            VerificationStepKind.SHELL,
            VerificationStepKind.HTTP_BOOT,
            VerificationStepKind.BEHAVIORAL_HTTP,
            VerificationStepKind.PPTX_DIFF,
            VerificationStepKind.INPUT_SENSITIVITY,
        }
        for step in steps
    )
    if not has_behavioural:
        smoke = _autodetect_smoke_shell_step(workspace_path)
        if smoke is not None:
            steps.append(smoke)

    return steps
