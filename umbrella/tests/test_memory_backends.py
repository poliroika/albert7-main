"""Durable backend factory and mode selection."""

import pytest

from umbrella.memory.backends.canonical import CanonicalMemoryBackend
from umbrella.memory.backends.dual_write import create_durable_backend
from umbrella.memory.backends.hindsight import HindsightBackend


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces" / "ws1").mkdir(parents=True)
    return tmp_path


def test_hindsight_only_requires_explicit_opt_in(repo, monkeypatch) -> None:
    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "hindsight")
    monkeypatch.delenv("UMBRELLA_ALLOW_UNSAFE_HINDSIGHT_ONLY", raising=False)

    backend = create_durable_backend(repo, workspace_id="ws1")
    assert isinstance(backend, CanonicalMemoryBackend)
    backend.close()


def test_hindsight_only_with_opt_in(repo, monkeypatch) -> None:
    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "hindsight")
    monkeypatch.setenv("UMBRELLA_ALLOW_UNSAFE_HINDSIGHT_ONLY", "1")

    backend = create_durable_backend(repo, workspace_id="ws1")
    assert isinstance(backend, HindsightBackend)


def test_dual_mode_returns_dual_write_wrapper(repo, monkeypatch) -> None:
    monkeypatch.setenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "dual")
    from umbrella.memory.backends.dual_write import _DualWriteBackend

    backend = create_durable_backend(repo, workspace_id="ws1")
    assert isinstance(backend, _DualWriteBackend)
    backend.close()
