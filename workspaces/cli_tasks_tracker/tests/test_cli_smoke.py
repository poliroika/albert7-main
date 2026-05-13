"""Smoke tests for CLI module."""
import sys


def test_cli_add():
    """Test that CLI module can be imported."""
    # Simple smoke test - just import and check main exists
    from tracker import cli
    assert hasattr(cli, 'main')


def test_task_imports():
    """Test that all model imports work."""
    from tracker.models import Task, Priority
    assert Task is not None
    assert Priority is not None
    assert Priority.LOW == "low"
    assert Priority.MEDIUM == "medium"
    assert Priority.HIGH == "high"