"""gmas_summarizer fallback and optional LLM path."""

from unittest.mock import patch

import pytest

from umbrella.retrieval.gmas_summarizer import summarize_chunk


def test_fallback_truncates_when_no_client() -> None:
    big = "x" * 50_000
    with patch(
        "umbrella.control_plane.code_analyzer.get_llm_client", return_value=None
    ):
        out = summarize_chunk(big, target_tokens=100, file_path="gmas/foo.py")
    assert len(out) < len(big)
    assert "truncated by fallback" in out.lower() or "[truncated" in out.lower()


def test_llm_path_uses_client_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class _C:
        def chat(self, messages, model=None):
            return {"role": "assistant", "content": "COMPRESSED"}, {}

    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "http://example.com/v1")

    with patch(
        "umbrella.control_plane.code_analyzer.get_llm_client", return_value=_C()
    ):
        out = summarize_chunk("def foo():\n  pass\n" * 100, 50, file_path="a.py")
    assert out == "COMPRESSED"
