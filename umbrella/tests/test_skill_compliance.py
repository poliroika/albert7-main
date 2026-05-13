"""Tests for ``umbrella.verification.skill_compliance``."""

from pathlib import Path

from umbrella.verification.skill_compliance import (
    build_skill_compliance_results,
    evaluate_gmas_compliance,
    evaluate_no_gmas_fallback,
    evaluate_no_mock_scaffold,
)


class TestEvaluateGmasCompliance:
    def test_passes_when_workspace_has_gmas_import(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "from gmas.execution import MACPRunner\n\n"
            "def boot():\n    return MACPRunner()\n",
            encoding="utf-8",
        )
        passed, summary, evidence = evaluate_gmas_compliance(tmp_path)
        assert passed is True
        assert "main.py" in evidence
        assert "Found gmas imports" in summary

    def test_passes_with_plain_import_gmas(self, tmp_path: Path) -> None:
        (tmp_path / "boot.py").write_text("import gmas\n", encoding="utf-8")
        passed, _summary, evidence = evaluate_gmas_compliance(tmp_path)
        assert passed is True
        assert "boot.py" in evidence

    def test_fails_when_only_fastapi(self, tmp_path: Path) -> None:
        (tmp_path / "web_server.py").write_text(
            "from fastapi import FastAPI\nimport httpx\napp = FastAPI()\n",
            encoding="utf-8",
        )
        passed, summary, _evidence = evaluate_gmas_compliance(tmp_path)
        assert passed is False
        assert "no `import gmas`" in summary
        assert "web_server.py" in summary

    def test_fails_on_empty_workspace(self, tmp_path: Path) -> None:
        passed, summary, _ = evaluate_gmas_compliance(tmp_path)
        assert passed is False
        assert "No Python files" in summary

    def test_skips_noise_directories(self, tmp_path: Path) -> None:
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "fake.py").write_text("import gmas\n", encoding="utf-8")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "cached.py").write_text(
            "from gmas import x\n", encoding="utf-8"
        )
        (tmp_path / "main.py").write_text(
            "from fastapi import FastAPI\n", encoding="utf-8"
        )
        passed, summary, _ = evaluate_gmas_compliance(tmp_path)
        assert passed is False
        assert "main.py" in summary
        assert ".venv" not in summary

    def test_diagnostic_script_import_does_not_satisfy_gmas_compliance(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "src" / "demo").mkdir(parents=True)
        (tmp_path / "src" / "demo" / "analyze_spec.py").write_text(
            "import gmas\n", encoding="utf-8"
        )
        (tmp_path / "src" / "demo" / "pipeline.py").write_text(
            "def run():\n    return 'ok'\n", encoding="utf-8"
        )

        passed, summary, evidence = evaluate_gmas_compliance(tmp_path)

        assert passed is False
        assert "src/demo/pipeline.py" in summary
        assert "src/demo/analyze_spec.py" not in evidence


class TestNoMockScaffold:
    def test_passes_when_app_code_has_no_mock_markers(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "from gmas.execution import MACPRunner\ndef boot():\n    return 'ok'\n",
            encoding="utf-8",
        )
        passed, summary = evaluate_no_mock_scaffold(tmp_path)
        assert passed is True
        assert "No obvious mock/scaffold markers" in summary

    def test_fails_on_mock_scaffold_markers(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "from gmas.execution import MACPRunner\n"
            "def mock_llm_caller(prompt):\n    return 'Mocked response'\n"
            "EXAMPLE = 'Example thesis'\n",
            encoding="utf-8",
        )
        passed, summary = evaluate_no_mock_scaffold(tmp_path)
        assert passed is False
        assert "mock/scaffold markers" in summary
        assert "main.py" in summary

    def test_fails_on_placeholder_llm_and_stub_content(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "content_tools.py").write_text(
            "def _create_mock_content(topic):\n"
            "    # This placeholder would use LLM in production\n"
            "    return 'Key impact analysis pending'\n",
            encoding="utf-8",
        )

        passed, summary = evaluate_no_mock_scaffold(tmp_path)

        assert passed is False
        assert "src/content_tools.py" in summary


class TestNoGmasFallback:
    def test_passes_when_gmas_import_fails_loudly(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "from gmas.execution import MACPRunner\n"
            "def boot():\n    return MACPRunner()\n",
            encoding="utf-8",
        )

        passed, summary = evaluate_no_gmas_fallback(tmp_path)

        assert passed is True
        assert "No silent GMAS fallback" in summary

    def test_fails_when_code_falls_back_to_object_basetool(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "tool.py").write_text(
            "try:\n"
            "    from gmas.tools.base import BaseTool\n"
            "    GMAS_AVAILABLE = True\n"
            "except ImportError:\n"
            "    GMAS_AVAILABLE = False\n"
            "    BaseTool = object\n",
            encoding="utf-8",
        )

        passed, summary = evaluate_no_gmas_fallback(tmp_path)

        assert passed is False
        assert "non-GMAS stubs" in summary
        assert "tool.py" in summary


class TestBuildSkillComplianceResults:
    def test_no_domains_no_results(self, tmp_path: Path) -> None:
        assert build_skill_compliance_results(tmp_path, set()) == []

    def test_with_gmas_domain_returns_compliance_results(self, tmp_path: Path) -> None:
        (tmp_path / "x.py").write_text(
            "from gmas.execution import MACPRunner\n", encoding="utf-8"
        )
        results = build_skill_compliance_results(tmp_path, {"multi_agent_gmas"})

        assert {r.step.name for r in results} == {
            "skill_compliance:multi_agent_gmas",
            "skill_quality:multi_agent_gmas_no_fallback",
            "skill_quality:multi_agent_gmas_no_mock_scaffold",
        }
        assert all(r.status.value == "passed" for r in results)

    def test_with_unknown_domain_no_results(self, tmp_path: Path) -> None:
        assert build_skill_compliance_results(tmp_path, {"unknown_skill"}) == []
