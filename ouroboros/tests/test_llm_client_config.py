import os
import unittest
from unittest.mock import patch

from ouroboros.llm import (
    DEFAULT_LLM_CLIENT_RETRIES,
    DEFAULT_LLM_REQUEST_TIMEOUT,
    resolve_llm_client_retries,
    resolve_llm_request_timeout,
)


class TestLLMClientConfig(unittest.TestCase):
    def test_client_retries_default_to_visible_loop_retries_only(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_llm_client_retries(), DEFAULT_LLM_CLIENT_RETRIES)
            self.assertEqual(resolve_llm_client_retries(), 0)

    def test_client_retries_env_is_clamped_to_non_negative(self):
        with patch.dict(os.environ, {"OUROBOROS_LLM_CLIENT_RETRIES": "-5"}):
            self.assertEqual(resolve_llm_client_retries(), 0)
        with patch.dict(os.environ, {"OUROBOROS_LLM_CLIENT_RETRIES": "2"}):
            self.assertEqual(resolve_llm_client_retries(), 2)

    def test_request_timeout_is_env_configurable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_llm_request_timeout(), DEFAULT_LLM_REQUEST_TIMEOUT)
        with patch.dict(os.environ, {"OUROBOROS_LLM_REQUEST_TIMEOUT": "45.5"}):
            self.assertEqual(resolve_llm_request_timeout(), 45.5)

    def test_request_timeout_invalid_value_uses_default(self):
        with patch.dict(os.environ, {"OUROBOROS_LLM_REQUEST_TIMEOUT": "nope"}):
            self.assertEqual(resolve_llm_request_timeout(), DEFAULT_LLM_REQUEST_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
