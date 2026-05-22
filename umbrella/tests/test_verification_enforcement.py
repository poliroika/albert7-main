from pathlib import Path

from umbrella.verification.models import VerificationStep, VerificationStepKind
from umbrella.verification.runner import run_verification


def test_code_task_without_tests_fails_quality_gate(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    report = run_verification(
        tmp_path,
        [],
        workspace_id="demo",
        changed_files=["src/app.py"],
    )

    assert not report.passed
    assert any(
        result.step.name == "test_quality_guard" and result.status.value == "failed"
        for result in report.results
    )


def test_mutation_smoke_fails_when_simple_mutant_survives(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def is_positive(value):\n"
        "    return value > 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_app.py").write_text(
        "from src.app import is_positive\n\n"
        "def test_positive_number():\n"
        "    assert is_positive(2) is True\n",
        encoding="utf-8",
    )

    step = VerificationStep(
        kind=VerificationStepKind.SHELL,
        name="pytest",
        command=["python", "-m", "pytest", "-q"],
    )
    report = run_verification(
        tmp_path,
        [step],
        workspace_id="demo",
        changed_files=["src/app.py"],
    )

    assert not report.passed
    assert any(result.step.name == "mutation_smoke:changed_python" for result in report.results)


def test_verification_step_cannot_mutate_tests(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_real():\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    step = VerificationStep(
        kind=VerificationStepKind.SHELL,
        name="tamper",
        command=[
            "python",
            "-c",
            "from pathlib import Path; Path('tests/test_app.py').write_text('def test_real():\\n    assert True\\n')",
        ],
    )

    report = run_verification(tmp_path, [step], workspace_id="demo")

    assert not report.passed
    assert any(
        result.step.name == "enforcement:verification_mutation"
        and "verification_test_mutation" in result.summary
        for result in report.results
    )
