"""
Tests for UmbrellaRuntimeConfig, YAML loading, budget enforcement,
and artifact-based evaluation.
"""

import tempfile
from pathlib import Path


from umbrella.config import UmbrellaRuntimeConfig, load_runtime_config


class TestUmbrellaRuntimeConfig:
    """Tests for the unified runtime config model."""

    def test_defaults(self):
        cfg = UmbrellaRuntimeConfig()
        assert cfg.max_budget_usd is None
        assert cfg.max_iterations is None
        assert cfg.max_duration_seconds is None
        assert cfg.quality_completion_threshold == 0.85
        assert cfg.min_article_word_count == 1500
        assert cfg.required_artifact_types == ["report"]
        assert cfg.self_improve_after_stalled_iterations == 2
        assert cfg.self_improve_max_total_iterations == 50
        assert "outline_approved" in cfg.human_review_stages
        assert "final_draft" in cfg.human_review_stages
        assert cfg.human_review_timeout_seconds == 0
        assert cfg.auto_retrieve_gmas_context is False
        assert cfg.instance_cleanup_enabled is True
        assert cfg.keep_recent_runs_per_instance == 2
        assert cfg.keep_recent_snapshots_per_instance == 1
        assert cfg.keep_recent_reports_per_instance == 4
        assert cfg.keep_latest_detached_instances == 1
        assert cfg.heartbeat_interval_seconds == 30.0

    def test_override_fields(self):
        cfg = UmbrellaRuntimeConfig(
            max_budget_usd=5.0,
            max_iterations=20,
            quality_completion_threshold=0.95,
            human_review_stages=["draft"],
            auto_retrieve_gmas_context=True,
        )
        assert cfg.max_budget_usd == 5.0
        assert cfg.max_iterations == 20
        assert cfg.quality_completion_threshold == 0.95
        assert cfg.human_review_stages == ["draft"]
        assert cfg.auto_retrieve_gmas_context is True


class TestLoadRuntimeConfig:
    """Tests for YAML + override loading."""

    def test_load_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "policy.yaml"
            yaml_path.write_text(
                "runtime:\n"
                "  max_budget_usd: 10.0\n"
                "  quality_completion_threshold: 0.90\n"
                "  human_review_stages:\n"
                "    - outline\n",
                encoding="utf-8",
            )
            cfg = load_runtime_config(policy_path=yaml_path)
            assert cfg.max_budget_usd == 10.0
            assert cfg.quality_completion_threshold == 0.90
            assert cfg.human_review_stages == ["outline"]

    def test_cli_overrides_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "policy.yaml"
            yaml_path.write_text(
                "runtime:\n  max_budget_usd: 10.0\n",
                encoding="utf-8",
            )
            cfg = load_runtime_config(
                policy_path=yaml_path,
                overrides={"max_budget_usd": 3.0},
            )
            assert cfg.max_budget_usd == 3.0

    def test_none_overrides_ignored(self):
        cfg = load_runtime_config(overrides={"max_budget_usd": None})
        assert cfg.max_budget_usd is None

    def test_missing_yaml_gives_defaults(self):
        cfg = load_runtime_config(policy_path=Path("/nonexistent/path.yaml"))
        assert cfg.quality_completion_threshold == 0.85

    def test_loads_from_default_policy(self):
        cfg = load_runtime_config()
        assert isinstance(cfg, UmbrellaRuntimeConfig)
        assert cfg.quality_completion_threshold == 0.85
        assert cfg.auto_retrieve_gmas_context is False


class TestBudgetEnforcement:
    """Tests for budget enforcement in the manager loop."""

    def test_runtime_limit_reason_with_budget(self):
        from umbrella.integration.runner import _runtime_limit_reason

        reason = _runtime_limit_reason(
            iteration=0,
            elapsed_seconds=0.0,
            max_iterations=None,
            max_duration_seconds=None,
        )
        assert reason is None

    def test_runtime_limit_reason_iteration_cap(self):
        from umbrella.integration.runner import _runtime_limit_reason

        reason = _runtime_limit_reason(
            iteration=10,
            elapsed_seconds=5.0,
            max_iterations=10,
            max_duration_seconds=None,
        )
        assert reason is not None
        assert "max iterations" in reason.lower()


class TestArtifactEval:
    """Sanity checks for the rewritten eval helpers."""

    def test_count_words(self):
        from umbrella.evals.runner import _count_words

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("one two three four five\n")
            f.flush()
            assert _count_words(Path(f.name)) == 5

    def test_count_sections(self):
        from umbrella.evals.runner import _count_sections

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("# Title\n## Section A\n## Section B\n### Sub\n")
            f.flush()
            assert _count_sections(Path(f.name)) == 4

    def test_has_citations(self):
        from umbrella.evals.runner import _has_citations

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("## References\n\n[1] Example\n")
            f.flush()
            assert _has_citations(Path(f.name))

    def test_scan_stage_notes_empty(self):
        from umbrella.evals.runner import _scan_stage_notes

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _scan_stage_notes(Path(tmpdir))
            assert result["total"] == 0
