"""Tests for tracker.models module."""
import pytest
from datetime import datetime, timezone
from tracker.models import Task, Priority


def test_priority_values():
    """Test Priority enum has correct values."""
    assert Priority.LOW.value == "low"
    assert Priority.MEDIUM.value == "medium"
    assert Priority.HIGH.value == "high"


def test_task_creation():
    """Test Task dataclass creation."""
    task = Task(
        id=1,
        title="Test task",
        priority=Priority.HIGH,
        tags=["test"],
        created_at="2024-01-01T00:00:00+00:00"
    )
    assert task.id == 1
    assert task.title == "Test task"
    assert task.priority == Priority.HIGH
    assert task.tags == ["test"]
    assert task.created_at == "2024-01-01T00:00:00+00:00"
    assert task.done_at is None


def test_task_with_done_at():
    """Test Task with done_at set."""
    done_at = datetime.now(timezone.utc).isoformat()
    task = Task(
        id=2,
        title="Done task",
        priority=Priority.LOW,
        tags=[],
        created_at="2024-01-01T00:00:00+00:00",
        done_at=done_at
    )
    assert task.done_at == done_at


def test_task_empty_tags():
    """Test Task with empty tags list (default)."""
    task = Task(
        id=3,
        title="No tags task",
        priority=Priority.MEDIUM,
        tags=[],
        created_at="2024-01-01T00:00:00+00:00"
    )
    assert task.tags == []


def test_task_is_done():
    """Test Task is_done property (derived from done_at)."""
    task_not_done = Task(
        id=1,
        title="Not done",
        priority=Priority.MEDIUM,
        tags=[],
        created_at="2024-01-01T00:00:00+00:00",
        done_at=None
    )
    assert not task_not_done.done_at
    
    task_done = Task(
        id=2,
        title="Done",
        priority=Priority.MEDIUM,
        tags=[],
        created_at="2024-01-01T00:00:00+00:00",
        done_at="2024-01-02T00:00:00+00:00"
    )
    assert task_done.done_at is not None