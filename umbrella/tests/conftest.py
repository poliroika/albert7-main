"""Shared pytest fixtures for Umbrella tests."""

import pytest


@pytest.fixture(autouse=True)
def _allow_volatile_palace_stub(monkeypatch):
    """MemPalace tests run without chromadb via in-memory stub."""
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
