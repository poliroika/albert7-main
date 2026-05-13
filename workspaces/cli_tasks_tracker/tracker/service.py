"""Service layer for CLI Tasks Tracker with business logic."""

from datetime import datetime, timezone
from typing import Optional

from tracker.models import Priority, Task
from tracker.storage import TaskStorage


class TaskNotFoundError(Exception):
    """Raised when a task with given ID doesn't exist."""
    pass


class TaskService:
    """Business logic layer for task management."""
    
    def __init__(self, storage: Optional[TaskStorage] = None, storage_file: Optional[str] = None):
        """Initialize task service with storage backend.
        
        Args:
            storage: TaskStorage instance (creates default if None).
            storage_file: Path to storage file (creates TaskStorage if provided).
        """
        if storage_file is not None:
            # Create a TaskStorage with the given file path
            self.storage = TaskStorage(storage_file=storage_file)
        else:
            self.storage = storage or TaskStorage()

    def add_task(self, title: str, priority: Optional[str] = None, tags: Optional[list[str]] = None) -> int:
        """Add a new task.
        
        Args:
            title: Task title.
            priority: Priority level (low, medium, high). Defaults to 'medium'.
            tags: List of tags. Defaults to empty list.
        
        Returns:
            The ID of the newly created task.
        """
        if priority is None:
            priority = "medium"
        if tags is None:
            tags = []
        
        priority_enum = Priority(priority.lower())
        task_id = self.storage.get_next_id()
        created_at = datetime.now(timezone.utc).isoformat()
        
        task = Task(
            id=task_id,
            title=title,
            priority=priority_enum,
            tags=tags,
            created_at=created_at,
            done_at=None
        )
        
        tasks = self.storage.load()
        tasks.append(task)
        self.storage.save(tasks)
        
        return task_id

    def list_tasks(self, priority: Optional[str] = None, tag: Optional[str] = None, 
                   done: Optional[bool] = None) -> list[Task]:
        """List tasks with optional filters.
        
        Args:
            priority: Filter by priority level.
            tag: Filter by tag (tasks must have this tag).
            done: Filter by done status.
        
        Returns:
            List of filtered Task objects.
        """
        tasks = self.storage.load()
        
        # Apply filters
        if priority is not None:
            priority_enum = Priority(priority.lower())
            tasks = [t for t in tasks if t.priority == priority_enum]
        
        if tag is not None:
            tasks = [t for t in tasks if tag in t.tags]
        
        if done is not None:
            if done:
                tasks = [t for t in tasks if t.done_at is not None]
            else:
                tasks = [t for t in tasks if t.done_at is None]
        
        return tasks

    def mark_done(self, task_id: int) -> None:
        """Mark a task as done.
        
        Args:
            task_id: ID of the task to mark as done.
        
        Raises:
            TaskNotFoundError: If task with given ID doesn't exist.
        """
        tasks = self.storage.load()
        task_found = False
        
        for task in tasks:
            if task.id == task_id:
                task.done_at = datetime.now(timezone.utc).isoformat()
                task_found = True
                break
        
        if not task_found:
            raise TaskNotFoundError(f"Task with ID {task_id} not found")
        
        self.storage.save(tasks)

    def mark_undone(self, task_id: int) -> None:
        """Mark a task as not done.
        
        Args:
            task_id: ID of the task to mark as not done.
        
        Raises:
            TaskNotFoundError: If task with given ID doesn't exist.
        """
        tasks = self.storage.load()
        task_found = False
        
        for task in tasks:
            if task.id == task_id:
                task.done_at = None
                task_found = True
                break
        
        if not task_found:
            raise TaskNotFoundError(f"Task with ID {task_id} not found")
        
        self.storage.save(tasks)

    def search_tasks(self, query: str) -> list[Task]:
        """Search tasks by title (case-insensitive substring).
        
        Args:
            query: Search query string.
        
        Returns:
            List of matching Task objects.
        """
        tasks = self.storage.load()
        query_lower = query.lower()
        
        return [t for t in tasks if query_lower in t.title.lower()]

    def get_stats(self) -> dict:
        """Get statistics about tasks.
        
        Returns:
            Dictionary with total, done, by_priority, and top_tags.
        """
        tasks = self.storage.load()
        
        total = len(tasks)
        done = sum(1 for t in tasks if t.done_at is not None)
        
        # Count by priority
        by_priority = {
            "high": 0,
            "medium": 0,
            "low": 0
        }
        
        for task in tasks:
            by_priority[task.priority.value] += 1
        
        # Count tags
        tag_counts = {}
        for task in tasks:
            for tag in task.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        
        # Get top 5 tags
        top_tags = dict(sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:5])
        
        return {
            "total": total,
            "done": done,
            "by_priority": by_priority,
            "top_tags": top_tags
        }
