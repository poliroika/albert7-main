"""Auto-compaction when message history exceeds model context thresholds."""

from types import SimpleNamespace

import pytest

from ouroboros.loop import _auto_set_pending_compaction_for_overflow


def test_sets_aggressive_pending_at_95_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_CONTEXT_TOKENS", "10000")
    # ~12000 tokens: 48000 chars
    body = "a" * 48_000
    messages = [{"role": "user", "content": body}]
    ctx = SimpleNamespace(_pending_compaction=None)
    _auto_set_pending_compaction_for_overflow(messages, ctx)
    assert ctx._pending_compaction == 3


def test_sets_moderate_pending_at_86_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_CONTEXT_TOKENS", "100000")
    body = "b" * 360_000  # ~90000 tokens
    messages = [{"role": "user", "content": body}]
    ctx = SimpleNamespace(_pending_compaction=None)
    _auto_set_pending_compaction_for_overflow(messages, ctx)
    assert ctx._pending_compaction == 3


def test_respects_existing_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_CONTEXT_TOKENS", "1000")
    messages = [{"role": "user", "content": "x" * 10000}]
    ctx = SimpleNamespace(_pending_compaction=8)
    _auto_set_pending_compaction_for_overflow(messages, ctx)
    assert ctx._pending_compaction == 8
