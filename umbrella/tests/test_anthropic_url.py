"""Test for the ``/v1/v1/messages`` deduplication helper (P2-2)."""

import pytest

from umbrella.control_plane.code_analyzer import _anthropic_messages_url


@pytest.mark.parametrize(
    "base, expected",
    [
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1/", "https://api.anthropic.com/v1/messages"),
        # Bug we are guarding against: callers that already provided /v1
        # used to get /v1/v1/messages and a 401 from the proxy.
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/messages"),
        ("https://example.com/proxy/v1/", "https://example.com/proxy/v1/messages"),
        ("https://example.com/proxy", "https://example.com/proxy/v1/messages"),
    ],
)
def test_anthropic_messages_url_normalisation(base: str, expected: str) -> None:
    assert _anthropic_messages_url(base) == expected


def test_does_not_duplicate_v1_when_chained() -> None:
    once = _anthropic_messages_url("https://api.anthropic.com/v1")
    # Idempotency check: re-running the helper on its own output must
    # not introduce a third /v1.
    assert once.count("/v1/") == 1
