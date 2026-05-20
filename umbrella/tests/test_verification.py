"""Tests for the ``umbrella.verification`` package."""

import socket
import sys
import textwrap
from pathlib import Path

import pytest

from umbrella.verification import (
    VerificationReport,
    VerificationStatus,
    VerificationStep,
    VerificationStepKind,
    load_verification_spec,
    run_verification,
)
from umbrella.verification.spec_loader import (
    VerificationSpecError,
    autodetect_steps,
    format_workspace_verification_digest,
    load_verification_meta,
)
from umbrella.verification.runner import (
    _choose_python_executable,
    _mock_scaffold_hits,
    _run_shell_step,
)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestShellStep:
    def test_shell_pass(self, tmp_path: Path) -> None:
        step = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="echo",
            command=[sys.executable, "-c", "print('hi')"],
            timeout_seconds=10,
        )
        report = run_verification(tmp_path, [step])

        assert report.passed
        assert report.results[0].status == VerificationStatus.PASSED
        assert report.results[0].exit_code == 0
        assert "hi" in report.results[0].stdout

    def test_workspace_uses_repo_venv_python_when_available(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        workspace = repo / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        venv_python = (
            repo
            / ".venv"
            / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("", encoding="utf-8")

        assert _choose_python_executable(workspace) == [str(venv_python)]

    def test_shell_repairs_generated_bash_python_c_escaped_quotes(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n",
            encoding="utf-8",
        )
        step = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="import:stack",
            command=[
                "bash",
                "-c",
                (
                    "python -c 'value = 1; "
                    'print(\\"All modules imported successfully\\")'
                    "'"
                ),
            ],
            timeout_seconds=10,
        )

        report = run_verification(tmp_path, [step])

        assert report.passed, report.render_summary()
        result = report.results[0]
        assert result.status == VerificationStatus.PASSED
        assert "All modules imported successfully" in result.stdout
        assert result.step.command[:2] == [sys.executable, "-c"]
        assert '\\"' not in result.step.command[2]

    def test_shell_failure(self, tmp_path: Path) -> None:
        step = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="boom",
            command=[sys.executable, "-c", "import sys; sys.exit(7)"],
            timeout_seconds=10,
        )
        report = run_verification(tmp_path, [step])

        assert not report.passed
        assert report.results[0].exit_code == 7
        assert report.failed_steps

    def test_compileall_cant_list_is_failure_even_with_zero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_args, **_kwargs):
            import subprocess

            return subprocess.CompletedProcess(
                args=[sys.executable, "-m", "compileall", "-q", "missing_pkg"],
                returncode=0,
                stdout="",
                stderr="Can't list 'missing_pkg'\n",
            )

        monkeypatch.setattr("umbrella.verification.runner.subprocess.run", fake_run)
        step = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="compileall:missing",
            command=[sys.executable, "-m", "compileall", "-q", "missing_pkg"],
            timeout_seconds=10,
        )

        result = _run_shell_step(step, tmp_path, {})

        assert result.status == VerificationStatus.FAILED
        assert result.error == "python_compile_failed_cannot_list"

    def test_shell_timeout(self, tmp_path: Path) -> None:
        step = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="slow",
            command=[sys.executable, "-c", "import time; time.sleep(5)"],
            timeout_seconds=1,
        )
        report = run_verification(tmp_path, [step])

        result = report.results[0]
        assert result.status == VerificationStatus.FAILED
        assert result.error == "timeout"


class TestImportStep:
    def test_import_pass(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n", encoding="utf-8"
        )
        (tmp_path / "sample_mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        step = VerificationStep(
            kind=VerificationStepKind.IMPORT_CHECK,
            name="import sample",
            module="sample_mod",
            timeout_seconds=15,
        )
        report = run_verification(tmp_path, [step])

        assert report.passed

    def test_import_failure(self, tmp_path: Path) -> None:
        step = VerificationStep(
            kind=VerificationStepKind.IMPORT_CHECK,
            name="import missing",
            module="definitely_missing_module_xyz",
            timeout_seconds=15,
        )
        report = run_verification(tmp_path, [step])

        assert not report.passed
        assert report.results[0].status == VerificationStatus.FAILED

    def test_import_only_report_is_too_shallow(self, tmp_path: Path) -> None:
        (tmp_path / "sample_mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        step = VerificationStep(
            kind=VerificationStepKind.IMPORT_CHECK,
            name="import sample",
            module="sample_mod",
            timeout_seconds=15,
        )
        report = run_verification(tmp_path, [step])

        assert report.passed


class TestHttpBootStep:
    def test_http_boot_health_ok(self, tmp_path: Path) -> None:
        port = _pick_free_port()
        server_script = tmp_path / "tiny_server.py"
        server_script.write_text(
            textwrap.dedent(
                f"""
                from http.server import BaseHTTPRequestHandler, HTTPServer

                class H(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(b'ok')
                    def log_message(self, *a, **kw):
                        return

                HTTPServer(('127.0.0.1', {port}), H).serve_forever()
                """
            ),
            encoding="utf-8",
        )
        step = VerificationStep(
            kind=VerificationStepKind.HTTP_BOOT,
            name="tiny_server",
            command=[sys.executable, "tiny_server.py"],
            health_url=f"http://127.0.0.1:{port}/",
            startup_timeout_seconds=10,
        )
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n", encoding="utf-8"
        )
        report = run_verification(tmp_path, [step])

        assert report.passed, report.render_summary()

    def test_http_boot_never_healthy(self, tmp_path: Path) -> None:
        port = _pick_free_port()
        dummy = tmp_path / "not_a_server.py"
        dummy.write_text("print('not a server')\n", encoding="utf-8")
        step = VerificationStep(
            kind=VerificationStepKind.HTTP_BOOT,
            name="bad_server",
            command=[sys.executable, "not_a_server.py"],
            health_url=f"http://127.0.0.1:{port}/",
            startup_timeout_seconds=3,
        )
        report = run_verification(tmp_path, [step])

        assert not report.passed, report.render_summary()


class TestSpecLoader:
    def test_workspace_toml_explicit_spec(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                kind = "shell"
                command = ["python", "-c", "print(1)"]
                timeout_seconds = 5
                name = "echo1"
                """
            ).strip(),
            encoding="utf-8",
        )
        steps = load_verification_spec(tmp_path)

        assert len(steps) == 1
        assert steps[0].kind == VerificationStepKind.SHELL
        assert steps[0].name == "echo1"
        assert steps[0].timeout_seconds == 5

    def test_workspace_toml_table_command_string_is_split(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                kind = "shell"
                name = "string-command"
                command = "python -m pytest tests -q"
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = load_verification_spec(tmp_path)

        assert len(steps) == 1
        assert steps[0].command == ["python", "-m", "pytest", "tests", "-q"]

    def test_explicit_spec_is_augmented_with_existing_tests(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                kind = "shell"
                name = "cli-info"
                command = "python -m demo.cli info"
                """
            ).strip(),
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_demo.py").write_text(
            "def test_demo(): assert True\n", encoding="utf-8"
        )

        steps = load_verification_spec(tmp_path)

        assert any(step.name == "cli-info" for step in steps)
        assert any(step.name == "pytest:tests" for step in steps)

    def test_explicit_spec_test_augmentation_honours_skip_test_quality(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                skip_test_quality = true
                [[verification.steps]]
                kind = "shell"
                name = "cli-info"
                command = "python -m demo.cli info"
                """
            ).strip(),
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()

        steps = load_verification_spec(tmp_path)

        assert [step.name for step in steps] == ["cli-info"]

    def test_explicit_spec_does_not_get_auto_smoke_when_test_quality_skipped(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                skip_test_quality = true
                [[verification.steps]]
                kind = "file_exists"
                name = "readme"
                path = "README.md"
                """
            ).strip(),
            encoding="utf-8",
        )
        (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
        (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")

        steps = load_verification_spec(tmp_path)

        assert any(step.name == "readme" for step in steps)
        assert not any(step.name == "smoke_run:main.py" for step in steps)

    def test_workspace_toml_string_steps(self, tmp_path: Path) -> None:
        """Friendly format ``steps = ["cmd1", "cmd2"]`` must be accepted as
        a list of SHELL steps. Without this Ouroboros' rewrite of
        ``workspace.toml`` (which prefers the simpler list-of-strings form)
        silently produced "verification skipped" with zero steps.
        """
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [workspace]
                name = "demo"

                [verification]
                steps = [
                  "python -c 'print(1)'",
                  "python -m pytest tests -q",
                ]
                """
            ).strip(),
            encoding="utf-8",
        )
        steps = load_verification_spec(tmp_path)

        assert len(steps) == 2

    def test_workspace_toml_parse_error_is_not_silently_autodetected(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            r"""
            [verification]
            [[verification.steps]]
            kind = "shell"
            command = ["python", "-c", "import sys; sys.path.insert(0, r'C:\Users\demo\gmas\src')"]
            """,
            encoding="utf-8",
        )

        with pytest.raises(VerificationSpecError) as excinfo:
            load_verification_spec(tmp_path)

        assert "Invalid TOML" in str(excinfo.value)
        assert "workspace.toml" in str(excinfo.value)

    def test_workspace_toml_legacy_type_command_alias(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                name = "legacy-command"
                type = "command"
                command = ["python", "-c", "print(1)"]
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = load_verification_spec(tmp_path)

        assert len(steps) == 1
        assert steps[0].kind == VerificationStepKind.SHELL

    def test_workspace_toml_file_exists_step(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                name = "readme-present"
                type = "file_exists"
                path = "README.md"
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = load_verification_spec(tmp_path)
        report = run_verification(tmp_path, steps)

        assert steps[0].kind == VerificationStepKind.FILE_EXISTS
        assert report.passed, report.render_summary()

    def test_workspace_toml_file_exists_path_list_expands_to_n_steps(
        self, tmp_path: Path
    ) -> None:
        """A common agent mistake is ``path = ["a", "b"]``. Previously this
        was silently cast to ``"['a', 'b']"`` and produced an unfindable
        literal path. Now it expands into N separate file_exists steps.
        """
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "src" / "b.py").write_text("", encoding="utf-8")
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                name = "core_files"
                kind = "file_exists"
                path = ["src/a.py", "src/b.py"]
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = load_verification_spec(tmp_path)
        report = run_verification(tmp_path, steps)

        assert len(steps) == 2
        assert {s.path for s in steps} == {"src/a.py", "src/b.py"}
        assert all(s.kind == VerificationStepKind.FILE_EXISTS for s in steps)
        assert {s.name for s in steps} == {"core_files__0", "core_files__1"}
        assert report.passed, report.render_summary()

    def test_workspace_toml_file_exists_path_dict_raises_with_helpful_error(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                name = "broken"
                kind = "file_exists"
                [verification.steps.path]
                a = "src/a.py"
                """
            ).strip(),
            encoding="utf-8",
        )

        with pytest.raises(VerificationSpecError) as excinfo:
            load_verification_spec(tmp_path)
        assert "must be string or list[str]" in str(excinfo.value)

    def test_workspace_toml_path_list_rejected_for_shell_kind(
        self, tmp_path: Path
    ) -> None:
        """Only file_exists permits ``path`` as a list. For other kinds the
        loader must refuse with a clear message instead of silently
        stringifying it.
        """
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                name = "weird"
                kind = "shell"
                command = ["python", "--version"]
                path = ["a", "b"]
                """
            ).strip(),
            encoding="utf-8",
        )

        with pytest.raises(VerificationSpecError) as excinfo:
            load_verification_spec(tmp_path)
        assert "only 'file_exists' supports a list" in str(excinfo.value)

    def test_workspace_toml_unknown_fields_warned_not_fatal(
        self, tmp_path: Path, caplog
    ) -> None:
        """Unknown fields (e.g. ``dir`` for file_exists) get a loud warning
        so the agent can see the silent-mismatch class of bugs, but parsing
        continues.
        """
        import logging as _logging

        (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                name = "with-unknown"
                kind = "file_exists"
                path = "README.md"
                dir = "src/news_cards_ai"
                """
            ).strip(),
            encoding="utf-8",
        )
        with caplog.at_level(
            _logging.WARNING, logger="umbrella.verification.spec_loader"
        ):
            steps = load_verification_spec(tmp_path)
        assert len(steps) == 1
        assert any(
            "unknown fields" in rec.getMessage() and "'dir'" in rec.getMessage()
            for rec in caplog.records
        )

    def test_workspace_toml_steps_file_resolves_external_spec(
        self, tmp_path: Path
    ) -> None:
        """``steps_file = "verification.toml"`` used to be silently ignored.
        Now it must load and merge with any inline ``[[verification.steps]]``.
        """
        (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                steps_file = "verification.toml"
                [[verification.steps]]
                name = "inline-readme"
                kind = "file_exists"
                path = "README.md"
                """
            ).strip(),
            encoding="utf-8",
        )
        (tmp_path / "verification.toml").write_text(
            textwrap.dedent(
                """
                [[steps]]
                name = "external-main"
                kind = "file_exists"
                path = "src/main.py"
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = load_verification_spec(tmp_path)
        names = {s.name for s in steps}
        assert names == {"inline-readme", "external-main"}

    def test_workspace_toml_steps_file_outside_workspace_ignored(
        self, tmp_path: Path, caplog
    ) -> None:
        import logging as _logging

        (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                steps_file = "../escape.toml"
                [[verification.steps]]
                name = "inline-readme"
                kind = "file_exists"
                path = "README.md"
                """
            ).strip(),
            encoding="utf-8",
        )
        with caplog.at_level(
            _logging.WARNING, logger="umbrella.verification.spec_loader"
        ):
            steps = load_verification_spec(tmp_path)
        assert {s.name for s in steps} == {"inline-readme"}
        assert any("escapes workspace" in rec.getMessage() for rec in caplog.records)

    def test_verification_meta_enforces_test_quality_by_default(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n",
            encoding="utf-8",
        )
        meta = load_verification_meta(tmp_path)
        assert meta["skip_test_quality"] is False
        assert meta["skip_behavioral"] is True

    def test_verification_meta_can_skip_test_quality_explicitly(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_test_quality = true\n",
            encoding="utf-8",
        )
        meta = load_verification_meta(tmp_path)
        assert meta["skip_test_quality"] is True

    def test_verification_meta_enforce_test_quality_overrides_default(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nenforce_test_quality = true\n",
            encoding="utf-8",
        )
        meta = load_verification_meta(tmp_path)
        assert meta["skip_test_quality"] is False

    def test_workspace_toml_steps_all_invalid_logs_error(
        self, tmp_path: Path, caplog
    ) -> None:
        """When [verification] is declared but every step is unparseable
        (e.g. a list of ints) we want a loud ``ERROR`` log so the user can
        actually find the problem instead of seeing a silent
        ``verification: skipped``.
        """
        import logging as _logging

        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                steps = [1, 2, 3]
                """
            ).strip(),
            encoding="utf-8",
        )
        with caplog.at_level(
            _logging.ERROR, logger="umbrella.verification.spec_loader"
        ):
            steps = load_verification_spec(tmp_path)
        assert steps == []
        assert any(
            "verification will be SKIPPED" in rec.getMessage() for rec in caplog.records
        )

    def test_autodetect_test_smoke(self, tmp_path: Path) -> None:
        (tmp_path / "test_smoke.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
        steps = autodetect_steps(tmp_path)

        assert any(
            s.kind == VerificationStepKind.SHELL and "pytest" in " ".join(s.command)
            for s in steps
        )

    def test_autodetect_web_server(self, tmp_path: Path) -> None:
        (tmp_path / "web_server.py").write_text("print('stub')\n", encoding="utf-8")
        steps = autodetect_steps(tmp_path)

        assert any(s.kind == VerificationStepKind.HTTP_BOOT for s in steps)

    def test_autodetect_nested_fastapi_entrypoint(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "app"
        target.mkdir(parents=True)
        (target / "main.py").write_text(
            textwrap.dedent(
                """
                from fastapi import FastAPI
                import uvicorn

                app = FastAPI()

                @app.get("/health")
                async def health():
                    return {"status": "ok"}

                if __name__ == "__main__":
                    uvicorn.run(app, host="0.0.0.0", port=8080)
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = autodetect_steps(tmp_path)

        http_steps = [s for s in steps if s.kind == VerificationStepKind.HTTP_BOOT]
        assert len(http_steps) == 1
        assert http_steps[0].command == [sys.executable, "src/app/main.py"]
        assert http_steps[0].health_url == "http://127.0.0.1:8080/health"

    def test_autodetect_fastapi_behavior_uses_real_post_route(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "main.py").write_text(
            textwrap.dedent(
                """
                from fastapi import FastAPI
                from pydantic import BaseModel
                import uvicorn

                app = FastAPI()

                class CreateGameRequest(BaseModel):
                    human_name: str
                    bot_personalities: list[str] = []

                @app.get("/")
                async def root():
                    return {"status": "ok"}

                @app.post("/api/game/create")
                async def create_game(request: CreateGameRequest):
                    return {"player": request.human_name}

                if __name__ == "__main__":
                    uvicorn.run(app, host="0.0.0.0", port=8080)
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = autodetect_steps(tmp_path)

        behavioral = [
            s for s in steps if s.kind == VerificationStepKind.BEHAVIORAL_HTTP
        ]
        assert len(behavioral) == 1
        assert behavioral[0].name == "behavioral_http:/api/game/create"
        assert behavioral[0].request_url == "http://127.0.0.1:8080/api/game/create"
        assert behavioral[0].request_payloads[0]["human_name"].startswith("alpha")
        assert behavioral[0].request_payloads[1]["human_name"].startswith("beta")

    def test_autodetect_prefers_domain_route_over_generic_generate(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "main.py").write_text(
            textwrap.dedent(
                """
                from typing import Any
                from fastapi import FastAPI
                from pydantic import BaseModel
                import uvicorn

                app = FastAPI()

                class CreateGameRequest(BaseModel):
                    human_name: str

                @app.get("/")
                async def root():
                    return {"status": "ok"}

                @app.post("/generate")
                async def generate_text(request: dict[str, Any]):
                    return {"input": request.get("input")}

                @app.post("/api/game/create")
                async def create_game(request: CreateGameRequest):
                    return {"player": request.human_name}

                if __name__ == "__main__":
                    uvicorn.run(app, host="0.0.0.0", port=8080)
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = autodetect_steps(tmp_path)

        behavioral = [
            s for s in steps if s.kind == VerificationStepKind.BEHAVIORAL_HTTP
        ]
        assert len(behavioral) == 1
        assert behavioral[0].name == "behavioral_http:/api/game/create"

    def test_autodetect_fastapi_does_not_force_generate_route(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "main.py").write_text(
            textwrap.dedent(
                """
                from fastapi import FastAPI
                import uvicorn

                app = FastAPI()

                @app.get("/")
                async def root():
                    return {"status": "ok"}

                if __name__ == "__main__":
                    uvicorn.run(app, host="0.0.0.0", port=8080)
                """
            ).strip(),
            encoding="utf-8",
        )

        steps = autodetect_steps(tmp_path)

        assert any(s.kind == VerificationStepKind.HTTP_BOOT for s in steps)
        assert not any(
            s.kind == VerificationStepKind.BEHAVIORAL_HTTP for s in steps
        )

    def test_autodetect_skips_pptx_diff_when_only_template_pptx(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "Template_week.pptx").write_bytes(b"PK")
        steps = autodetect_steps(tmp_path)
        assert not any(s.kind == VerificationStepKind.PPTX_DIFF for s in steps)

    def test_autodetect_adds_pptx_diff_with_non_template_pptx(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "Template_week.pptx").write_bytes(b"PK")
        (tmp_path / "generated.pptx").write_bytes(b"PK2")
        steps = autodetect_steps(tmp_path)
        assert any(s.kind == VerificationStepKind.PPTX_DIFF for s in steps)

    def test_format_workspace_verification_digest_includes_steps(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "test_smoke.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
        digest = format_workspace_verification_digest(tmp_path)
        assert "[WORKSPACE_VERIFICATION_DIGEST]" in digest
        assert "pytest" in digest


class TestReportSummary:
    def test_render_summary_mentions_failures(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n", encoding="utf-8"
        )
        step_ok = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="ok",
            command=[sys.executable, "-c", "print('ok')"],
            timeout_seconds=20,
        )
        step_bad = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="bad",
            command=[
                sys.executable,
                "-c",
                "import sys; print('err', file=sys.stderr); sys.exit(2)",
            ],
            timeout_seconds=20,
        )
        report = run_verification(tmp_path, [step_ok, step_bad])

        summary = report.render_summary()
        assert "FAIL" in summary
        assert "bad" in summary
        assert not report.passed
        assert report.pass_rate == pytest.approx(2 / 3)


class TestReportDataclass:
    def test_empty_report_not_passed(self) -> None:
        report = VerificationReport(workspace_id="x", workspace_path="/tmp/x")
        assert report.passed is False
        assert report.pass_rate == 0.0


class TestHttpBootOutputCapture:
    """Regression tests for the empty-output and pipe-deadlock bugs."""

    def test_stderr_captured_when_server_exits_quickly(self, tmp_path: Path) -> None:
        """A server that prints to stderr and exits should show its output
        in ``result.stderr`` after the teardown finally-block runs.
        Before the fix, the ``return`` inside ``try`` read the buffers
        before ``finally`` populated them, leaving stderr empty.
        """
        port = _pick_free_port()
        script = tmp_path / "dies.py"
        script.write_text(
            textwrap.dedent(
                """
                import sys
                sys.stderr.write("BOOM: cannot start\\n")
                sys.stderr.flush()
                sys.exit(3)
                """
            ),
            encoding="utf-8",
        )
        step = VerificationStep(
            kind=VerificationStepKind.HTTP_BOOT,
            name="dies",
            command=[sys.executable, "dies.py"],
            health_url=f"http://127.0.0.1:{port}/",
            startup_timeout_seconds=5,
        )
        report = run_verification(tmp_path, [step])

        assert not report.passed, report.render_summary()
        http_results = [
            r for r in report.results if r.step.kind == VerificationStepKind.HTTP_BOOT
        ]
        assert http_results and "BOOM" in (http_results[0].stderr or "")

    def test_chatty_server_does_not_deadlock(self, tmp_path: Path) -> None:
        """Server that floods stdout must not block on its pipe.
        Before the fix, stdout=PIPE without draining could deadlock
        the child once the OS pipe buffer fills.
        """
        port = _pick_free_port()
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n", encoding="utf-8"
        )
        script = tmp_path / "chatty.py"
        # Write > 64KiB to stdout BEFORE opening the socket, then serve.
        script.write_text(
            textwrap.dedent(
                f"""
                import sys
                from http.server import BaseHTTPRequestHandler, HTTPServer
                # Flood stdout to exceed any reasonable pipe buffer.
                for _ in range(2000):
                    print("x" * 200, flush=False)
                sys.stdout.flush()

                class H(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'ok')
                    def log_message(self, *a, **kw):
                        return

                HTTPServer(('127.0.0.1', {port}), H).serve_forever()
                """
            ),
            encoding="utf-8",
        )
        step = VerificationStep(
            kind=VerificationStepKind.HTTP_BOOT,
            name="chatty",
            command=[sys.executable, "chatty.py"],
            health_url=f"http://127.0.0.1:{port}/",
            startup_timeout_seconds=10,
        )
        report = run_verification(tmp_path, [step])

        assert report.passed, report.render_summary()


class TestHttpBootPortCollision:
    def test_pre_existing_listener_is_rejected(self, tmp_path: Path) -> None:
        """If something is already answering on the health URL BEFORE the
        child is launched, we must fail with a clear port-collision error
        instead of producing a false positive. This regression came from a
        live run where a zombie Flask server from a previous session was
        answering 200 on the same port, masking the fact that the new
        ``web_server.py`` did not exist yet.
        """
        import http.server
        import socketserver
        import threading

        port = _pick_free_port()
        handler = http.server.SimpleHTTPRequestHandler
        # ``allow_reuse_address`` lets us tear down cleanly in the same test.

        class _ReusableTCPServer(socketserver.TCPServer):
            allow_reuse_address = True

        srv = _ReusableTCPServer(("127.0.0.1", port), handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            step = VerificationStep(
                kind=VerificationStepKind.HTTP_BOOT,
                name="collision",
                command=[sys.executable, "-c", "print('stub')"],
                health_url=f"http://127.0.0.1:{port}/",
                startup_timeout_seconds=3,
            )
            report = run_verification(tmp_path, [step])
        finally:
            srv.shutdown()
            srv.server_close()

        assert not report.passed, report.render_summary()


class TestBehavioralDepth:
    def test_mock_scaffold_markers_are_detected(self) -> None:
        text = '{"cards":[{"title":"News 1","bullets":["Point 1"],"image":"https://via.placeholder.com/400"}]}'

        hits = _mock_scaffold_hits(text)

        assert "numbered news placeholder" in hits
        assert "numbered point placeholder" in hits
        assert "placeholder image url" in hits

    def test_future_implementation_markers_are_detected(self) -> None:
        text = (
            "def run():\n    # Full pipeline will be implemented in phase 4\n    pass\n"
        )

        hits = _mock_scaffold_hits(text)

        assert "future implementation marker" in hits or "phase scaffold marker" in hits

    def test_behavioral_http_requires_topic_words_from_inputs(
        self, tmp_path: Path
    ) -> None:
        port = _pick_free_port()
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n", encoding="utf-8"
        )
        server_script = tmp_path / "canned_server.py"
        server_script.write_text(
            textwrap.dedent(
                f"""
                from http.server import BaseHTTPRequestHandler, HTTPServer

                class H(BaseHTTPRequestHandler):
                    calls = 0
                    def do_GET(self):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'ok')
                    def do_POST(self):
                        H.calls += 1
                        self.rfile.read(int(self.headers.get('Content-Length', '0')))
                        body = b'{{"result":"canned robotics"}}' if H.calls == 1 else b'{{"result":"canned medicine"}}'
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(body)
                    def log_message(self, *a, **kw):
                        return

                HTTPServer(('127.0.0.1', {port}), H).serve_forever()
                """
            ).strip(),
            encoding="utf-8",
        )
        step = VerificationStep(
            kind=VerificationStepKind.BEHAVIORAL_HTTP,
            name="behavioral",
            command=[sys.executable, "canned_server.py"],
            health_url=f"http://127.0.0.1:{port}/health",
            request_url=f"http://127.0.0.1:{port}/generate",
            request_payloads=[
                {"text": "volcano eruption ash cloud"},
                {"text": "quantum battery breakthrough"},
            ],
            startup_timeout_seconds=10,
        )

        report = run_verification(tmp_path, [step])

        assert not report.passed, report.render_summary()

    def test_behavioral_http_failure_includes_requests_and_error_body(
        self, tmp_path: Path
    ) -> None:
        port = _pick_free_port()
        server_script = tmp_path / "validation_server.py"
        server_script.write_text(
            textwrap.dedent(
                f"""
                from http.server import BaseHTTPRequestHandler, HTTPServer

                class H(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'ok')
                    def do_POST(self):
                        self.rfile.read(int(self.headers.get('Content-Length', '0')))
                        self.send_response(422)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(b'{{"detail":"game_id is required"}}')
                    def log_message(self, *a, **kw):
                        return

                HTTPServer(('127.0.0.1', {port}), H).serve_forever()
                """
            ).strip(),
            encoding="utf-8",
        )
        step = VerificationStep(
            kind=VerificationStepKind.BEHAVIORAL_HTTP,
            name="behavioral",
            command=[sys.executable, "validation_server.py"],
            health_url=f"http://127.0.0.1:{port}/health",
            request_url=f"http://127.0.0.1:{port}/generate",
            request_payloads=[{"text": "alpha"}, {"text": "beta"}],
            startup_timeout_seconds=10,
        )

        report = run_verification(tmp_path, [step])
        result = report.results[0]

        assert not report.passed
        assert "statuses=[422, 422]" in result.summary
        assert "--- request A ---" in result.stdout
        assert '"text": "alpha"' in result.stdout
        assert '"detail":"game_id is required"' in result.stdout

    def test_changed_source_mock_scaffold_fails_verification(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n",
            encoding="utf-8",
        )
        (workspace / "pipeline.py").write_text(
            "def build():\n    return {'cards': [{'title': 'News 1', 'point': 'Point 1'}]}\n",
            encoding="utf-8",
        )
        step = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="ok",
            command=[sys.executable, "-c", "print('ok')"],
        )

        report = run_verification(
            workspace,
            [step],
            workspace_id="demo",
            changed_files=["workspaces/demo/pipeline.py"],
        )

        assert not report.passed, report.render_summary()

    def test_explicit_source_policy_step_scans_workspace(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n",
            encoding="utf-8",
        )
        (tmp_path / "pipeline.py").write_text(
            "def build():\n    return {'title': 'Weekly summary'}\n",
            encoding="utf-8",
        )
        step = VerificationStep(
            kind=VerificationStepKind.SOURCE_POLICY,
            name="source_policy:mock_scaffold_scan",
        )

        report = run_verification(tmp_path, [step], workspace_id="demo")

        assert report.passed

    def test_source_policy_excludes_lesson_meta_files(self, tmp_path: Path) -> None:
        """Built-in skip-path for ``record_verification_lessons.py``.

        Real-world reproduction: the agent wrote a lesson body that
        described the patterns the scanner looks for (``Point 1\\nPoint 2``).
        The scanner then flagged its own lesson file as scaffolding,
        triggering an unfixable verification spiral. The skip-path
        glob list MUST exclude this and similar meta-files by default.
        """
        from umbrella.verification.source_policy import (
            scan_changed_files_for_mock_scaffold,
        )

        (tmp_path / "workspace.toml").write_text(
            "[verification]\nskip_behavioral = true\n",
            encoding="utf-8",
        )
        # Lesson file with a marker phrase the scanner normally catches.
        (tmp_path / "record_verification_lessons.py").write_text(
            "# Avoid 'Point 1\\nPoint 2\\nPoint 3' as test data.\n"
            "LESSON_BODY = 'Point 1, Point 2, Point 3 is a numbered point placeholder marker.'\n",
            encoding="utf-8",
        )
        # And one real source file with the same marker — must STILL be caught.
        (tmp_path / "real_app.py").write_text(
            "OUTPUT = 'Point 1\\nPoint 2\\nPoint 3'\n",
            encoding="utf-8",
        )

        hits = scan_changed_files_for_mock_scaffold(
            repo_root=tmp_path,
            workspace_path=tmp_path,
            changed_files=["record_verification_lessons.py", "real_app.py"],
        )
        assert any(h.startswith("real_app.py:") for h in hits), hits
        assert not any(h.startswith("record_verification_lessons.py:") for h in hits), (
            f"Lesson file should be excluded, got hits: {hits}"
        )

    def test_source_policy_excludes_memory_directory_by_default(
        self, tmp_path: Path
    ) -> None:
        """Anything inside ``.memory/`` is meta-context, not application
        source. The scanner used to catch e.g. ``.memory/drive/state/idea_*.json``
        when the agent's ``record_idea`` body quoted a marker token.
        """
        from umbrella.verification.source_policy import (
            scan_changed_files_for_mock_scaffold,
        )

        (tmp_path / "workspace.toml").write_text("[verification]\n", encoding="utf-8")
        memdir = tmp_path / ".memory" / "drive" / "state"
        memdir.mkdir(parents=True)
        (memdir / "idea_verification_fix_mock_scaffold.json").write_text(
            '{"title": "verification_fix:mock_scaffold", "body": "Look for Point 1, Point 2..."}\n',
            encoding="utf-8",
        )

        hits = scan_changed_files_for_mock_scaffold(
            repo_root=tmp_path,
            workspace_path=tmp_path,
            changed_files=[
                ".memory/drive/state/idea_verification_fix_mock_scaffold.json"
            ],
        )
        assert hits == [], f"Files under .memory/ must be skipped, got {hits}"

    def test_source_policy_honours_user_skip_paths_in_workspace_toml(
        self, tmp_path: Path
    ) -> None:
        """Users can extend the skip list via ``[verification.skip_paths]``.

        This covers the case where a workspace has its own conventions
        for fixtures / lesson dumps that look like scaffolding to the
        default heuristics.
        """
        from umbrella.verification.source_policy import (
            load_skip_path_patterns,
            scan_changed_files_for_mock_scaffold,
        )

        (tmp_path / "workspace.toml").write_text(
            '[verification]\nskip_paths = ["fixtures/**", "**/*.fixture.py"]\n',
            encoding="utf-8",
        )
        fix_dir = tmp_path / "fixtures"
        fix_dir.mkdir()
        (fix_dir / "sample.py").write_text("X = 'Point 1, Point 2'\n", encoding="utf-8")
        (tmp_path / "data.fixture.py").write_text(
            "Y = 'Point 3, Point 4'\n", encoding="utf-8"
        )
        (tmp_path / "real.py").write_text("Z = 'Point 5'\n", encoding="utf-8")

        patterns = load_skip_path_patterns(tmp_path)
        # The user patterns are appended to the built-ins.
        assert "fixtures/**" in patterns
        assert "**/*.fixture.py" in patterns

        hits = scan_changed_files_for_mock_scaffold(
            repo_root=tmp_path,
            workspace_path=tmp_path,
            changed_files=["fixtures/sample.py", "data.fixture.py", "real.py"],
        )
        assert any(h.startswith("real.py:") for h in hits), hits
        assert not any(h.startswith("fixtures/") for h in hits), hits
        assert not any("fixture.py" in h for h in hits), hits

    def test_source_policy_handles_invalid_workspace_toml_safely(
        self, tmp_path: Path
    ) -> None:
        """A malformed workspace.toml must not crash the scanner — the
        verification gate runs in production and a TOML typo cannot be
        allowed to take it down.
        """
        from umbrella.verification.source_policy import load_skip_path_patterns

        (tmp_path / "workspace.toml").write_text(
            "[verification\nskip_paths = [\n", encoding="utf-8"
        )
        patterns = load_skip_path_patterns(tmp_path)
        # Built-ins must still be present.
        assert ".memory/**" in patterns

    def test_workspace_policy_disables_gmas_skill_compliance(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [skills]
                multi_agent_gmas = false

                [verification]
                skip_behavioral = true
                """
            ).strip(),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
        step = VerificationStep(
            kind=VerificationStepKind.SHELL,
            name="ok",
            command=[sys.executable, "-c", "print('ok')"],
        )

        report = run_verification(
            tmp_path,
            [step],
            workspace_id="demo",
            detected_domains={"multi_agent_gmas"},
        )

        names = {result.step.name for result in report.results}
        assert "skill_compliance:multi_agent_gmas" not in names
        assert "skill_quality:multi_agent_gmas_no_mock_scaffold" not in names


class TestSpecLoaderTimeoutZero:
    def test_explicit_timeout_zero_is_preserved(self, tmp_path: Path) -> None:
        """``timeout_seconds = 0`` is falsy in Python; make sure we still
        honour it and do not silently fall back to the default of 180."""
        (tmp_path / "workspace.toml").write_text(
            textwrap.dedent(
                """
                [verification]
                [[verification.steps]]
                kind = "shell"
                name = "zero-timeout"
                command = ["python", "-c", "pass"]
                timeout_seconds = 0
                startup_timeout_seconds = 0
                """
            ).strip(),
            encoding="utf-8",
        )
        steps = load_verification_spec(tmp_path)

        assert len(steps) == 1
        assert steps[0].timeout_seconds == 0
        assert steps[0].startup_timeout_seconds == 0
