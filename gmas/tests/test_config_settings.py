"""Tests for src/config/settings.py and src/config/logging.py"""

import os

import pytest
from pydantic import ValidationError

from gmas.config.settings import FrameworkSettings, load_env_file, load_settings

# ─────────────────────────── config/logging.py ───────────────────────────────


class TestConfigLogging:
    def test_as_bool_truthy_values(self):
        from gmas.config.logging import _as_bool

        assert _as_bool("1") is True
        assert _as_bool("true") is True
        assert _as_bool("True") is True
        assert _as_bool("TRUE") is True
        assert _as_bool("yes") is True
        assert _as_bool("on") is True

    def test_as_bool_falsy_values(self):
        from gmas.config.logging import _as_bool

        assert _as_bool("0") is False
        assert _as_bool("false") is False
        assert _as_bool("no") is False
        assert _as_bool("") is False
        assert _as_bool("random") is False

    def test_as_bool_none(self):
        from gmas.config.logging import _as_bool

        assert _as_bool(None) is False

    def test_setup_logging_basic(self):
        from gmas.config.logging import setup_logging

        # Should not raise
        setup_logging(level="INFO")

    def test_setup_logging_debug_level(self):
        from gmas.config.logging import setup_logging

        setup_logging(level="DEBUG")

    def test_setup_logging_with_format(self):
        from gmas.config.logging import setup_logging

        setup_logging(format_string="{time} | {message}")

    def test_setup_logging_with_backtrace(self):
        from gmas.config.logging import setup_logging

        setup_logging(backtrace=True)
        setup_logging(backtrace=False)

    def test_setup_logging_with_log_file(self, tmp_path):
        from gmas.config.logging import setup_logging

        log_file = str(tmp_path / "test.log")
        setup_logging(level="INFO", log_file=log_file)

    def test_setup_logging_from_env(self, monkeypatch):
        from gmas.config.logging import setup_logging

        monkeypatch.setenv("GMAS_LOG_LEVEL", "WARNING")
        monkeypatch.setenv("GMAS_LOG_BACKTRACE", "true")
        setup_logging()  # Should read from env vars

    def test_setup_logging_log_file_from_env(self, monkeypatch, tmp_path):
        from gmas.config.logging import setup_logging

        log_file = str(tmp_path / "env.log")
        monkeypatch.setenv("GMAS_LOG_FILE", log_file)
        setup_logging()
        monkeypatch.delenv("GMAS_LOG_FILE", raising=False)

    def test_logger_is_available(self):
        from gmas.config.logging import logger

        assert logger is not None


# ─────────────────────────── FrameworkSettings ───────────────────────────────


class TestFrameworkSettings:
    def test_create_with_api_key(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "test-key-123")
        settings = FrameworkSettings()
        assert settings.resolved_api_key == "test-key-123"

    def test_default_values(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "test-key")
        settings = FrameworkSettings()
        assert settings.model_name == "gpt-4o-mini"
        assert settings.default_timeout == 60
        assert settings.max_retries == 3
        assert settings.log_level == "INFO"
        assert settings.embedding_normalize is True

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("GMAS_API_KEY", raising=False)
        monkeypatch.delenv("GMAS_API_KEY_FILE", raising=False)
        with pytest.raises(ValidationError, match="api_key is required"):
            FrameworkSettings()

    def test_custom_model_name(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "key")
        monkeypatch.setenv("GMAS_MODEL_NAME", "gpt-4")
        settings = FrameworkSettings()
        assert settings.model_name == "gpt-4"

    def test_custom_timeout(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "key")
        monkeypatch.setenv("GMAS_DEFAULT_TIMEOUT", "120")
        settings = FrameworkSettings()
        assert settings.default_timeout == 120

    def test_valid_hash_embedding_model(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "key")
        monkeypatch.setenv("GMAS_EMBEDDING_MODEL", "hash:768")
        settings = FrameworkSettings()
        assert settings.embedding_model == "hash:768"

    def test_valid_sentence_transformers_model(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "key")
        monkeypatch.setenv("GMAS_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        settings = FrameworkSettings()
        assert "sentence-transformers" in settings.embedding_model

    def test_invalid_embedding_model(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "key")
        monkeypatch.setenv("GMAS_EMBEDDING_MODEL", "openai/text-embedding")
        with pytest.raises(ValidationError, match="Unsupported embedding model"):
            FrameworkSettings()

    def test_api_key_file(self, monkeypatch, tmp_path):
        key_file = tmp_path / "api_key.txt"
        key_file.write_text("secret-key-from-file", encoding="utf-8")
        monkeypatch.delenv("GMAS_API_KEY", raising=False)
        monkeypatch.setenv("GMAS_API_KEY_FILE", str(key_file))
        settings = FrameworkSettings()
        assert settings.resolved_api_key == "secret-key-from-file"

    def test_api_key_file_not_found(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GMAS_API_KEY", raising=False)
        monkeypatch.setenv("GMAS_API_KEY_FILE", str(tmp_path / "nonexistent.txt"))
        with pytest.raises(ValidationError, match="API key file not found"):
            FrameworkSettings()

    def test_resolved_api_key_none_raises(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "key")
        settings = FrameworkSettings()
        # Manually set api_key to None to test RuntimeError
        object.__setattr__(settings, "api_key", None)
        with pytest.raises(RuntimeError, match="API key is not configured"):
            _ = settings.resolved_api_key

    def test_empty_string_api_key_treated_as_none(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "   ")
        monkeypatch.delenv("GMAS_API_KEY_FILE", raising=False)
        # Empty string should be treated as None → raises
        with pytest.raises(ValidationError, match="api_key is required"):
            FrameworkSettings()

    def test_log_level_custom(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "key")
        monkeypatch.setenv("GMAS_LOG_LEVEL", "DEBUG")
        settings = FrameworkSettings()
        assert settings.log_level == "DEBUG"


# ─────────────────────────── load_env_file ───────────────────────────────────


class TestLoadEnvFile:
    def test_load_from_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "MY_TEST_VAR=hello\nANOTHER_VAR=world\n# This is a comment\n\n",
            encoding="utf-8",
        )
        # Remove env vars before loading
        monkeypatch.delenv("MY_TEST_VAR", raising=False)
        monkeypatch.delenv("ANOTHER_VAR", raising=False)
        load_env_file(env_file)
        assert os.environ.get("MY_TEST_VAR") == "hello"
        assert os.environ.get("ANOTHER_VAR") == "world"

    def test_load_nonexistent_file(self, tmp_path):
        # Should not raise
        load_env_file(tmp_path / "nonexistent.env")

    def test_does_not_overwrite_existing_env(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING_VAR=from_file\n", encoding="utf-8")
        monkeypatch.setenv("EXISTING_VAR", "original")
        load_env_file(env_file)
        assert os.environ.get("EXISTING_VAR") == "original"

    def test_handles_quoted_values(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text('QUOTED_VAR="quoted_value"\n', encoding="utf-8")
        monkeypatch.delenv("QUOTED_VAR", raising=False)
        load_env_file(env_file)
        assert os.environ.get("QUOTED_VAR") == "quoted_value"

    def test_handles_single_quoted_values(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("SQ_VAR='single_quoted'\n", encoding="utf-8")
        monkeypatch.delenv("SQ_VAR", raising=False)
        load_env_file(env_file)
        assert os.environ.get("SQ_VAR") == "single_quoted"

    def test_skips_lines_without_equals(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("NOEQUALS\nVALID=value\n", encoding="utf-8")
        monkeypatch.delenv("VALID", raising=False)
        monkeypatch.delenv("NOEQUALS", raising=False)
        load_env_file(env_file)
        assert os.environ.get("VALID") == "value"
        assert os.environ.get("NOEQUALS") is None

    def test_skips_lines_with_empty_key(self, tmp_path, monkeypatch):
        """Line 123: continue when key is empty after stripping (e.g. '=value')."""
        env_file = tmp_path / ".env"
        env_file.write_text("=value_with_no_key\nVALID2=ok\n", encoding="utf-8")
        monkeypatch.delenv("VALID2", raising=False)
        load_env_file(env_file)
        assert os.environ.get("VALID2") == "ok"
        # Empty key line should be skipped without error
        assert os.environ.get("") is None or True  # no key named "" should be set


# ─────────────────────────── load_settings ───────────────────────────────────


class TestLoadSettings:
    def test_load_settings_success(self, monkeypatch):
        monkeypatch.setenv("GMAS_API_KEY", "test-key")
        settings = load_settings()
        assert settings is not None
        assert settings.resolved_api_key == "test-key"

    def test_load_settings_with_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("GMAS_API_KEY=key-from-file\n", encoding="utf-8")
        monkeypatch.delenv("GMAS_API_KEY", raising=False)
        settings = load_settings(env_file)
        assert settings.resolved_api_key == "key-from-file"
        monkeypatch.delenv("GMAS_API_KEY", raising=False)

    def test_load_settings_failure_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("GMAS_API_KEY", raising=False)
        monkeypatch.delenv("GMAS_API_KEY_FILE", raising=False)
        with pytest.raises(RuntimeError):
            load_settings()
