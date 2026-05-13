"""Dataclasses that describe verification steps and their results."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VerificationStepKind(str, Enum):
    SHELL = "shell"
    FILE_EXISTS = "file_exists"
    HTTP_BOOT = "http_boot"
    IMPORT_CHECK = "import_check"
    BEHAVIORAL_HTTP = "behavioral_http"
    INPUT_SENSITIVITY = "input_sensitivity_check"
    PPTX_DIFF = "pptx_diff"
    SOURCE_POLICY = "source_policy"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class VerificationStep:
    """Declarative description of a single verification step.

    Only ``kind`` and ``name`` are always used.  The remaining fields are
    interpreted by the runner based on ``kind``:

    - ``shell``:        ``command``, ``timeout_seconds``.
    - ``http_boot``:    ``command`` (to launch), ``health_url``,
                        ``startup_timeout_seconds``.
    - ``import_check``: ``module`` (dotted name from workspace root) or
                        ``command`` (custom python -c "...") + ``timeout_seconds``.
    """

    kind: VerificationStepKind
    name: str
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 180
    health_url: str = ""
    startup_timeout_seconds: int = 30
    module: str = ""
    path: str = ""
    optional: bool = False
    env: dict[str, str] = field(default_factory=dict)
    # http_boot-only: extra assertions after the process becomes healthy
    expect_status: int = 200
    expect_json_keys: list[str] = field(default_factory=list)
    expect_json_path: dict[str, str] = field(default_factory=dict)
    expect_min_items: dict[str, int] = field(default_factory=dict)
    extra_health_urls: list[str] = field(default_factory=list)
    request_url: str = ""
    request_payloads: list[dict[str, Any]] = field(default_factory=list)
    output_glob: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "command": list(self.command),
            "timeout_seconds": self.timeout_seconds,
            "health_url": self.health_url,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "module": self.module,
            "path": self.path,
            "optional": self.optional,
            "env": dict(self.env),
            "expect_status": self.expect_status,
            "expect_json_keys": list(self.expect_json_keys),
            "expect_json_path": dict(self.expect_json_path),
            "expect_min_items": dict(self.expect_min_items),
            "extra_health_urls": list(self.extra_health_urls),
            "request_url": self.request_url,
            "request_payloads": list(self.request_payloads),
            "output_glob": self.output_glob,
        }


@dataclass
class VerificationStepResult:
    step: VerificationStep
    status: VerificationStatus
    exit_code: int | None = None
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    summary: str = ""
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.status == VerificationStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.step.name,
            "kind": self.step.kind.value,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "summary": self.summary,
            "stdout_tail": _tail(self.stdout, 2000),
            "stderr_tail": _tail(self.stderr, 2000),
            "error": self.error,
            "optional": self.step.optional,
            "request_payload_count": len(self.step.request_payloads),
        }


@dataclass
class VerificationReport:
    workspace_id: str
    workspace_path: str
    results: list[VerificationStepResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def passed(self) -> bool:
        """Pass only when every non-optional step passed."""
        if not self.results:
            return False
        for result in self.results:
            if result.step.optional:
                continue
            if result.status != VerificationStatus.PASSED:
                return False
        return True

    @property
    def required_steps(self) -> list[VerificationStepResult]:
        return [r for r in self.results if not r.step.optional]

    @property
    def failed_steps(self) -> list[VerificationStepResult]:
        return [
            r
            for r in self.results
            if r.status in (VerificationStatus.FAILED, VerificationStatus.ERROR)
            and not r.step.optional
        ]

    @property
    def pass_rate(self) -> float:
        required = self.required_steps
        if not required:
            return 0.0
        passed = sum(1 for r in required if r.status == VerificationStatus.PASSED)
        return passed / len(required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_path": self.workspace_path,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 3),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(max(0.0, self.finished_at - self.started_at), 3),
            "results": [r.to_dict() for r in self.results],
        }

    def render_summary(self, *, limit_chars: int = 4000) -> str:
        """Short markdown summary suitable for prompt injection."""
        lines: list[str] = []
        status_label = "PASS" if self.passed else "FAIL"
        lines.append(
            f"Verification: **{status_label}** "
            f"({sum(1 for r in self.required_steps if r.passed)}/"
            f"{len(self.required_steps)} required steps passed)"
        )
        for result in self.results:
            tag = "[optional]" if result.step.optional else "[required]"
            icon = "ok" if result.passed else result.status.value
            lines.append(
                f"- {tag} `{result.step.name}` ({result.step.kind.value}) -> {icon}"
                + (f" exit={result.exit_code}" if result.exit_code is not None else "")
            )
            if result.summary:
                lines.append(f"  {result.summary}")
            if not result.passed:
                tail_stderr = _tail(result.stderr, 600).strip()
                tail_stdout = _tail(result.stdout, 400).strip()
                if tail_stderr:
                    lines.append(
                        "  stderr:\n    " + tail_stderr.replace("\n", "\n    ")
                    )
                if tail_stdout:
                    lines.append(
                        "  stdout:\n    " + tail_stdout.replace("\n", "\n    ")
                    )
                if result.error:
                    lines.append(f"  error: {result.error}")

        text = "\n".join(lines)
        if len(text) > limit_chars:
            text = text[:limit_chars].rstrip() + "\n\n[summary truncated]"
        return text


def _tail(text: str, limit: int) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]
