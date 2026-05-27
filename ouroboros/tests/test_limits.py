"""Env-backed truncation limits."""

from ouroboros import limits


def test_tool_log_preview_default_increased() -> None:
    assert limits.TOOL_LOG_PREVIEW_CHARS >= 16000
    assert limits.TOOL_RESULT_TO_MODEL_CHARS >= 48000


def test_tool_log_preview_env_override(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_TOOL_LOG_PREVIEW_CHARS", "24000")
    assert limits._int_env("OUROBOROS_TOOL_LOG_PREVIEW_CHARS", 16000, minimum=2000) == 24000
