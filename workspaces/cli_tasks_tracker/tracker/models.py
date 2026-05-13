"""Task model for CLI Tasks Tracker."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Priority(str, Enum):
    """Task priority levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Task:
    """Task model with all required fields."""
    id: int
    title: str
    priority: Priority
    created_at: str  # ISO-8601 UTC
    tags: list[str] = field(default_factory=list)
    done_at: Optional[str] = None  # ISO-8601 UTC or None
