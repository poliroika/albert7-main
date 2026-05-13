"""Tests for TaskService."""
import pytest
import tempfile
import os


@pytest.fixture
def service():
    """Create a TaskService instance with temporary storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Change to temp directory to test with production default path
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        
        # Create data directory
        data_dir = os.path.join(tmpdir, "data")
        os.makedirs(data_dir)
        
        from tracker.service import TaskService, TaskNotFoundError
        service = TaskService()
        
        yield service
        
        os.chdir(original_cwd)

def test_add_task(service):
    """Test adding a task."""
    from tracker.models import Task
    
    task_id = service.add_task("Buy milk", priority="high", tags=["shopping"])
    assert task_id == 1
    
    tasks = service.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "Buy milk"
    assert tasks[0].priority.name == "HIGH"
    assert tasks[0].tags == ["shopping"]

def test_add_task_default_tags(service):
    """Test adding a task without tags."""
    task_id = service.add_task("Buy milk", priority="high")
    assert task_id == 1
    
    tasks = service.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].tags == []

def test_add_task_default_priority(service):
    """Test adding a task without priority."""
    task_id = service.add_task("Buy milk")
    assert task_id == 1
    
    tasks = service.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].priority.name == "MEDIUM"

def test_list_tasks_empty(service):
    """Test listing tasks when empty."""
    tasks = service.list_tasks()
    assert tasks == []

def test_list_tasks_filter_by_priority(service):
    """Test filtering tasks by priority."""
    service.add_task("Task 1", priority="high")
    service.add_task("Task 2", priority="low")
    service.add_task("Task 3", priority="high")
    
    high_tasks = service.list_tasks(priority="high")
    assert len(high_tasks) == 2
    
    low_tasks = service.list_tasks(priority="low")
    assert len(low_tasks) == 1

def test_list_tasks_filter_by_tag(service):
    """Test filtering tasks by tag."""
    service.add_task("Task 1", tags=["work"])
    service.add_task("Task 2", tags=["shopping"])
    service.add_task("Task 3", tags=["work"])
    
    work_tasks = service.list_tasks(tag="work")
    assert len(work_tasks) == 2
    
    shopping_tasks = service.list_tasks(tag="shopping")
    assert len(shopping_tasks) == 1

def test_list_tasks_filter_by_done(service):
    """Test filtering tasks by done status."""
    service.add_task("Task 1")
    service.add_task("Task 2")
    
    # Mark one task as done
    service.mark_done(1)
    
    done_tasks = service.list_tasks(done=True)
    assert len(done_tasks) == 1
    assert done_tasks[0].id == 1
    
    undone_tasks = service.list_tasks(done=False)
    assert len(undone_tasks) == 1
    assert undone_tasks[0].id == 2

def test_list_tasks_combined_filters(service):
    """Test combining multiple filters."""
    service.add_task("Task 1", priority="high", tags=["work"])
    service.add_task("Task 2", priority="low", tags=["work"])
    service.add_task("Task 3", priority="high", tags=["shopping"])
    
    tasks = service.list_tasks(priority="high", tag="work")
    assert len(tasks) == 1
    assert tasks[0].title == "Task 1"

def test_mark_done(service):
    """Test marking a task as done."""
    task_id = service.add_task("Buy milk")
    
    service.mark_done(task_id)
    
    tasks = service.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].done_at is not None

def test_mark_done_invalid_id(service):
    """Test marking a non-existent task as done."""
    from tracker.service import TaskNotFoundError
    with pytest.raises(TaskNotFoundError, match="Task.*not found"):
        service.mark_done(999)

def test_mark_undone(service):
    """Test marking a task as undone."""
    task_id = service.add_task("Buy milk")
    service.mark_done(task_id)
    
    service.mark_undone(task_id)
    
    tasks = service.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].done_at is None

def test_mark_undone_invalid_id(service):
    """Test marking a non-existent task as undone."""
    from tracker.service import TaskNotFoundError
    with pytest.raises(TaskNotFoundError, match="Task.*not found"):
        service.mark_undone(999)

def test_search_tasks(service):
    """Test searching tasks by title."""
    service.add_task("Buy milk")
    service.add_task("Milk the cows")
    service.add_task("Buy eggs")
    
    results = service.search_tasks("milk")
    assert len(results) == 2
    assert all("milk" in task.title.lower() for task in results)

def test_search_tasks_case_insensitive(service):
    """Test that search is case-insensitive."""
    service.add_task("Buy MILK")
    service.add_task("Milk the cows")
    
    results = service.search_tasks("milk")
    assert len(results) == 2
    
    results = service.search_tasks("MILK")
    assert len(results) == 2

def test_search_tasks_empty_query(service):
    """Test searching with empty query returns all tasks."""
    service.add_task("Task 1")
    service.add_task("Task 2")
    
    results = service.search_tasks("")
    assert len(results) == 2

def test_get_stats(service):
    """Test getting statistics."""
    service.add_task("Task 1", priority="high", tags=["work"])
    service.add_task("Task 2", priority="low", tags=["shopping"])
    service.add_task("Task 3", priority="high", tags=["work"])
    
    stats = service.get_stats()
    
    assert stats["total"] == 3
    assert stats["done"] == 0
    assert stats["by_priority"]["high"] == 2
    assert stats["by_priority"]["low"] == 1
    assert stats["by_priority"]["medium"] == 0
    assert stats["top_tags"]["work"] == 2
    assert stats["top_tags"]["shopping"] == 1

def test_get_stats_empty(service):
    """Test getting statistics with no tasks."""
    stats = service.get_stats()
    
    assert stats["total"] == 0
    assert stats["done"] == 0
    assert stats["by_priority"] == {"high": 0, "medium": 0, "low": 0}
    assert stats["top_tags"] == {}
