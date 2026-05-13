"""Tests for generic LLM API configuration."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


class _RecordingClient:
    def __init__(self, payload):
        self._payload = payload
        self.kwargs = None
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeResponse(self._payload)


class TestLLMConfig(unittest.TestCase):
    def test_llm_error_html_tunnel_detection(self):
        from ouroboros.llm import llm_error_looks_like_html_tunnel_page

        self.assertTrue(
            llm_error_looks_like_html_tunnel_page(
                "<!DOCTYPE html><title>Not Found</title>"
            )
        )
        self.assertTrue(
            llm_error_looks_like_html_tunnel_page(
                "<HTML><body>Faithfully yours, frp.</body>"
            )
        )
        self.assertFalse(
            llm_error_looks_like_html_tunnel_page('{"error":{"message":"invalid"}}')
        )

    def test_format_llm_exception_for_user_log_truncates_and_html(self):
        from ouroboros.llm import format_llm_exception_for_user_log

        html_exc = ValueError("<!DOCTYPE html><title>Not Found</title>")
        out = format_llm_exception_for_user_log(html_exc)
        self.assertIn("HTML/tunnel", out)
        self.assertNotIn("<!DOCTYPE", out)

        long_exc = RuntimeError("x" * 500)
        out2 = format_llm_exception_for_user_log(long_exc, max_len=80)
        self.assertLessEqual(len(out2), 80)
        self.assertTrue(out2.endswith("…"))

    def test_custom_base_url_uses_generic_key_and_skips_openrouter_extras(self):
        from ouroboros.llm import LLMClient

        payload = {
            "id": "resp_123",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        client = LLMClient(api_key="generic-key", base_url="https://api.example.com/v1")
        recorder = _RecordingClient(payload)
        client._client = recorder

        msg, usage = client.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="my-model",
        )

        self.assertEqual(msg["content"], "ok")
        self.assertEqual(usage["prompt_tokens"], 1)
        self.assertNotIn("extra_body", recorder.kwargs)

    def test_openrouter_keeps_reasoning_and_provider_hints(self):
        from ouroboros.llm import LLMClient

        payload = {
            "id": "gen_123",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.01},
        }
        client = LLMClient(api_key="or-key", base_url="https://openrouter.ai/api/v1")
        recorder = _RecordingClient(payload)
        client._client = recorder

        client.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="anthropic/claude-sonnet-4.6",
            reasoning_effort="high",
        )

        self.assertIn("extra_body", recorder.kwargs)
        self.assertEqual(recorder.kwargs["extra_body"]["reasoning"]["effort"], "high")
        self.assertEqual(
            recorder.kwargs["extra_body"]["provider"]["order"], ["Anthropic"]
        )

    def test_env_prefers_generic_llm_key_and_base_url(self):
        from ouroboros.llm import DEFAULT_LLM_BASE_URL, LLMClient

        with patch.dict(
            os.environ,
            {
                "OUROBOROS_LLM_API_KEY": "generic-env-key",
                "OUROBOROS_LLM_BASE_URL": "https://custom.example/v1",
                "OPENROUTER_API_KEY": "legacy-openrouter-key",
            },
            clear=False,
        ):
            client = LLMClient()
            self.assertEqual(client._api_key, "generic-env-key")
            self.assertEqual(client._base_url, "https://custom.example/v1")

        with patch.dict(os.environ, {}, clear=True):
            client = LLMClient(api_key="x")
            self.assertEqual(client._base_url, DEFAULT_LLM_BASE_URL)

    def test_strict_proxy_unrepairable_log_is_deduplicated(self):
        from ouroboros import llm

        llm._STRICT_PROXY_REPAIR_LOGGED.clear()
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "propose_task_plan",
                            "arguments": '{"steps":[{"title":"x","description":"unterminated}',
                        },
                    }
                ],
            }
        ]

        with patch.object(llm.log, "debug") as debug_log:
            llm.sanitize_messages_for_strict_openai_proxy(messages)
            llm.sanitize_messages_for_strict_openai_proxy(messages)

        debug_log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
