"""Storage layer for CLI Tasks Tracker with atomic JSON persistence."""

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from tracker.models import Task, Priority


class TaskStorage:
    """Thread-safe storage for tasks using atomic JSON file writes."""

    DEFAULT_PATH = Path("data/tasks.json")

    def __init__(self, path: Optional[Path | str] = None, storage_file: Optional[str | Path] = None):
        """Initialize storage with given path (creates data directory if needed).
        
        Args:
            path: Path to the JSON storage file. If None, uses DEFAULT_PATH.
            storage_file: Alternative name for path (for backward compatibility).
        """
        # Support both parameter names for flexibility
        if storage_file is not None:
            self.path = Path(storage_file)
        elif path is not None:
            self.path = Path(path)
        else:
            self.path = self.DEFAULT_PATH
        
        self._lock = Lock()
        self._ensure_data_dir()
        self._id_counter = 0
        # Initialize counter on first load
        self._load_raw()

    def _ensure_data_dir(self) -> None:
        """Ensure the data directory exists."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_raw(self) -> dict[str, Any]:
        """Load raw data from JSON file, creating empty structure if missing.
        
        Returns:
            Dictionary with 'tasks' list and 'next_id' counter.
        """
        if not self.path.exists():
            # Create initial empty structure
            self._id_counter = 1
            return {"tasks": [], "next_id": 1}

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._id_counter = data.get("next_id", 1)
                return data
        except (json.JSONDecodeError, IOError) as e:
            # If file is corrupted, treat as empty
            print(f"Warning: Corrupted storage file, starting fresh: {e}")
            self._id_counter = 1
            return {"tasks": [], "next_id": 1}

    def _save_raw(self, data: dict[str, Any]) -> None:
        """Save data to JSON file atomically using temp file + os.replace.
        
        Args:
            data: Dictionary with 'tasks' list and 'next_id' counter.
        """
        temp_path = self.path.with_suffix(".tmp")
        
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            # Atomic replace
            os.replace(temp_path, self.path)
        except OSError as e:
            # Clean up temp file if it exists
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

    def load(self) -> list[Task]:
        """Load all tasks from storage.
        
        Returns:
            List of Task objects.
        """
        with self._lock:
            data = self._load_raw()
            
            tasks = []
            for task_data in data.get("tasks", []):
                # Convert to Task object
                # Priority might be stored as string or enum, normalize to enum
                priority_value = task_data["priority"]
                if isinstance(priority_value, str):
                    priority = Priority(priority_value.lower())
                else:
                    priority = priority_value
                    
                task = Task(
                    id=task_data["id"],
                    title=task_data["title"],
                    priority=priority,
                    tags=task_data.get("tags", []),
                    created_at=task_data["created_at"],
                    done_at=task_data.get("done_at")
                )
                tasks.append(task)
            
            return tasks

    def save(self, tasks: list[Task]) -> None:
        """Save all tasks to storage atomically.
        
        Args:
            tasks: List of Task objects to save.
        """
        with self._lock:
            tasks_data = []
            max_id = self._id_counter
            
            for task in tasks:
                # Convert priority to string for JSON serialization
                priority_value = task.priority.value if isinstance(task.priority, Priority) else task.priority
                
                task_dict = {
                    "id": task.id,
                    "title": task.title,
                    "priority": priority_value,
                    "tags": task.tags,
                    "created_at": task.created_at,
                    "done_at": task.done_at
                }
                tasks_data.append(task_dict)
                if task.id >= max_id:
                    max_id = task.id + 1
            
            data = {
                "tasks": tasks_data,
                "next_id": max_id
            }
            
            self._save_raw(data)
            self._id_counter = max_id

    def get_next_id(self) -> int:
        """Get the next available ID for a new task.
        
        Returns:
            Next auto-increment ID.
        """
        with self._lock:
            return self._id_counter
