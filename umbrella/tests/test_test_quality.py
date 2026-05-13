"""Tests for ``umbrella.verification.test_quality.run_test_quality_guard`` (P1-1b)."""

import textwrap
from pathlib import Path


from umbrella.verification.test_quality import run_test_quality_guard
from umbrella.verification.models import VerificationStatus


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


class TestEmptyOrMissing:
    def test_workspace_without_tests_passes(self, tmp_path: Path) -> None:
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.PASSED
        assert "no test_*.py" in result.summary

    def test_test_files_without_test_functions_pass(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_helper.py",
            """
            def helper():
                return 1
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.PASSED
        assert "no `test_*` functions" in result.summary


class TestTrivialDetection:
    def test_assert_true_is_trivial(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_dumb.py",
            """
            def test_a():
                assert True

            def test_b():
                assert True
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.FAILED
        assert "trivial" in result.error

    def test_assert_is_not_none_is_trivial(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_weak.py",
            """
            def test_a():
                x = 1
                assert x is not None
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.FAILED

    def test_only_pass_or_docstring_is_trivial(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_skel.py",
            '''
            def test_a():
                """todo"""
                pass
            ''',
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.FAILED

    def test_print_only_smoke_call_is_trivial(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_print_only.py",
            """
            def build():
                return {"ok": True}

            def test_smoke():
                result = build()
                print(result)
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.FAILED

    def test_swallowed_exception_is_trivial(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_swallow.py",
            """
            def test_smoke():
                try:
                    raise RuntimeError("broken")
                except Exception as exc:
                    print(exc)
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.FAILED


class TestSubstantiveDetection:
    def test_real_assertions_pass(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_real.py",
            """
            def add(a, b):
                return a + b

            def test_add_two_numbers():
                assert add(1, 2) == 3

            def test_add_negative():
                assert add(-1, 1) == 0
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.PASSED, result.error
        assert "2/2 substantive" in result.summary

    def test_pytest_raises_counts_as_substantive(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_raises.py",
            """
            import pytest

            def fail():
                raise ValueError("bad")

            def test_error_path():
                with pytest.raises(ValueError):
                    fail()
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.PASSED, result.error

    def test_subprocess_check_true_counts_as_substantive(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_command.py",
            """
            import subprocess
            import sys

            def test_command_passes():
                subprocess.run([sys.executable, "-c", "print('ok')"], check=True)
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.PASSED, result.error

    def test_import_only_object_existence_fails_without_behavioral_evidence(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "test_import_only.py",
            """
            import math

            def test_imports_runner():
                assert hasattr(math, "sqrt")
            """,
        )

        result = run_test_quality_guard(tmp_path)

        assert result.status == VerificationStatus.FAILED
        assert "behavioral workflow" in result.error


class TestLayout:
    def test_test_files_under_src_fail_even_when_substantive(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "src" / "test_app.py",
            """
            def add(a, b):
                return a + b

            def test_add():
                assert add(1, 2) == 3
            """,
        )

        result = run_test_quality_guard(tmp_path)

        assert result.status == VerificationStatus.FAILED
        assert "instead of tests" in result.error


class TestWebShape:
    def test_web_workspace_without_http_tests_fails(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "web_server.py",
            """
            from fastapi import FastAPI
            app = FastAPI()
            """,
        )
        _write(
            tmp_path / "test_smoke.py",
            """
            def test_a():
                assert add() == 1
            def add():
                return 1
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.FAILED
        assert "HTTP" in result.error or "http" in result.error.lower()

    def test_web_workspace_with_http_tests_passes(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "web_server.py", "from fastapi import FastAPI\napp = FastAPI()\n"
        )
        _write(
            tmp_path / "test_api.py",
            """
            import httpx
            def test_health():
                resp = httpx.get('http://x/health')
                assert resp.status_code == 200
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.PASSED, result.error


class TestSyntaxErrorIsTrivial:
    def test_unparseable_test_file_counts_as_trivial(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "test_broken.py",
            "def test_a(:\n    pass\n",
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.FAILED


class TestSkippedDirectories:
    def test_venv_and_pycache_are_skipped(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".venv" / "lib" / "test_dont_scan.py",
            """
            def test_x():
                assert True
            """,
        )
        _write(
            tmp_path / "__pycache__" / "test_dont_scan.py",
            """
            def test_y():
                assert True
            """,
        )
        result = run_test_quality_guard(tmp_path)
        assert result.status == VerificationStatus.PASSED
