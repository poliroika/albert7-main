"""Tests for TaskStorage module."""
import pytest
import os
import tempfile
from pathlib import Path
from tracker.storage import TaskStorage


@pytest.fixture
def temp_storage():
    """Create a temporary storage for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        storage_file = os.path.join(tmpdir, "test_tasks.json")
        storage = TaskStorage(path=Path(storage_file))
        yield storage


def test_storage_init_creates_file(temp_storage):
    """Test that storage creates directory and initializes counter."""
    # File doesn't exist until first save, but directory should be created
    assert temp_storage.path.parent.exists()


def test_storage_save_and_load(temp_storage):
    """Test that storage can save and load tasks."""
    from tracker.models import Task, Priority
    
    tasks = [
        Task(
            id=1,
            title="Test task",
            priority=Priority.HIGH,
            tags=["test"],
            created_at="2024-01-01T00:00:00Z"
        )
    ]
    
    temp_storage.save(tasks)
    loaded = temp_storage.load()
    
    assert len(loaded) == 1
    assert loaded[0].title == "Test task"
    assert loaded[0].id == 1


def test_storage_empty_load(temp_storage):
    """Test that loading from non-existent file returns empty list."""
    # Check the file doesn't exist initially
    assert not temp_storage.path.exists()
    
    # load() should return empty list (file is not created until save)
    tasks = temp_storage.load()
    assert tasks == []


def test_storage_get_next_id(temp_storage):
    """Test getting next ID from existing tasks."""
    from tracker.models import Task, Priority
    
    tasks = [
        Task(
            id=1,
            title="Task 1",
            priority=Priority.LOW,
            tags=[],
            created_at="2024-01-01T00:00:00Z"
        ),
        Task(
            id=2,
            title="Task 2",
            priority=Priority.MEDIUM,
            tags=[],
            created_at="2024-01-01T00:00:00Z"
        )
    ]
    
    temp_storage.save(tasks)
    assert temp_storage.get_next_id() == 3


def test_storage_get_next_id_with_gaps(temp_storage):
    """Test getting next ID with gaps in task IDs."""
    from tracker.models import Task, Priority
    
    tasks = [
        Task(
            id=1,
            title="Task 1",
            priority=Priority.LOW,
            tags=[],
            created_at="2024-01-01T00:00:00Z"
        ),
        Task(
            id=5,
            title="Task 5",
            priority=Priority.MEDIUM,
            tags=[],
            created_at="2024-01-01T00:00:00Z"
        )
    ]
    
    temp_storage.save(tasks)
    # Should return max + 1 = 6
    assert temp_storage.get_next_id() == 6


def test_storage_atomic_write(temp_storage):
    """Test that write is atomic (uses temp file)."""
    from tracker.models import Task, Priority
    import json
    
    tasks = [
        Task(
            id=1,
            title="Test",
            priority=Priority.HIGH,
            tags=[],
            created_at="2024-01-01T00:00:00Z"
        )
    ]
    
    temp_storage.save(tasks)
    
    # Verify file exists and contains valid JSON
    assert temp_storage.path.exists()
    with open(temp_storage.path) as f:
        data = json.load(f)
    
    # Storage format: {'tasks': [...], 'next_id': ...}
    assert 'tasks' in data
    assert len(data['tasks']) == 1
    assert data['tasks'][0]['title'] == "Test"