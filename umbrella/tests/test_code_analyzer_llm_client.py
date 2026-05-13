"""SimpleLLMClient must use OpenAI-compatible chat/completions for typical /v1 gateways."""

from unittest.mock import MagicMock, patch

import pytest


def test_openai_compatible_proxy_uses_bearer_and_chat_completions() -> None:
    calls: list[dict] = []

    def _capture_post(url, **kwargs):
        calls.append(
            {
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "json": kwargs.get("json"),
            }
        )
        mock = MagicMock()
        mock.raise_for_status = MagicMock()
        mock.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        return mock

    with patch.dict(
        "os.environ",
        {
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "http://garfield3.frontierai.ru:7080/v1",
            "LLM_MODEL": "test-model",
        },
        clear=False,
    ):
        with patch("httpx.post", side_effect=_capture_post):
            from umbrella.control_plane.code_analyzer import get_llm_client

            client = get_llm_client()
            assert client is not None
            msg, _ = client.chat([{"role": "user", "content": "hi"}])
            assert (msg.get("content") or "") == "ok"

    assert len(calls) == 1
    assert "/chat/completions" in calls[0]["url"]
    assert calls[0]["headers"].get("Authorization") == "Bearer test-key"
    assert calls[0]["json"].get("model")


def test_native_anthropic_uses_messages_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _capture_post(url, **kwargs):
        calls.append(str(url))
        mock = MagicMock()
        mock.raise_for_status = MagicMock()
        mock.json.return_value = {"content": [{"type": "text", "text": "anth"}]}
        return mock

    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("LLM_MODEL", "claude-test")

    with patch("httpx.post", side_effect=_capture_post):
        import importlib

        import umbrella.control_plane.code_analyzer as ca

        importlib.reload(ca)
        client = ca.get_llm_client()
        assert client is not None
        msg, _ = client.chat([{"role": "user", "content": "x"}])
        assert "anth" in (msg.get("content") or "")

    assert any("/v1/messages" in u for u in calls)
