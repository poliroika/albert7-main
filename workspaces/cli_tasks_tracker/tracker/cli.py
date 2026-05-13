#!/usr/bin/env python3
"""CLI layer for tasks tracker."""

import argparse
import sys
from datetime import datetime, timezone

from tracker.models import Priority
from tracker.service import TaskService


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="tracker",
        description="Personal task manager with priorities and tags"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True,
        metavar="<command>"
    )

    # add command
    add_parser = subparsers.add_parser("add", help="Add a new task")
    add_parser.add_argument(
        "title",
        help="Task title (non-empty)"
    )
    add_parser.add_argument(
        "--priority",
        choices=["low", "medium", "high"],
        default="medium",
        help="Task priority (default: medium)"
    )
    add_parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        help="Task tags (can be used multiple times)"
    )

    # list command
    list_parser = subparsers.add_parser("list", help="List tasks")
    list_parser.add_argument(
        "--priority",
        choices=["low", "medium", "high"],
        help="Filter by priority"
    )
    list_parser.add_argument(
        "--tag",
        help="Filter by tag"
    )
    list_parser.add_argument(
        "--done",
        choices=["true", "false", "True", "False"],
        help="Filter by completion status"
    )

    # done command
    done_parser = subparsers.add_parser("done", help="Mark task as completed")
    done_parser.add_argument("id", type=int, help="Task ID")

    # undo command
    undo_parser = subparsers.add_parser("undo", help="Mark task as not completed")
    undo_parser.add_argument("id", type=int, help="Task ID")

    # search command
    search_parser = subparsers.add_parser("search", help="Search tasks by title")
    search_parser.add_argument("query", help="Search query (substring match)")

    # stats command
    subparsers.add_parser("stats", help="Show task statistics")

    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    """Execute the CLI command."""
    service = TaskService()

    try:
        if args.command == "add":
            priority = Priority(args.priority.upper())
            task_id = service.add_task(
                title=args.title,
                priority=priority,
                tags=args.tags or []
            )
            print(task_id)

        elif args.command == "list":
            filters = {}
            if args.priority:
                filters["priority"] = Priority(args.priority.upper())
            if args.tag:
                filters["tag"] = args.tag
            if args.done:
                filters["done"] = args.done.lower() == "true"
            tasks = service.list_tasks(**filters)
            _print_tasks(tasks)

        elif args.command == "done":
            service.mark_done(args.id)

        elif args.command == "undo":
            service.mark_undone(args.id)

        elif args.command == "search":
            tasks = service.search_tasks(args.query)
            _print_tasks(tasks)

        elif args.command == "stats":
            stats = service.get_stats()
            _print_stats(stats)

        return 0

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _print_tasks(tasks: list) -> None:
    """Print tasks in a tabular format."""
    if not tasks:
        print("No tasks found.")
        return

    # Calculate column widths
    id_width = max(3, max(len(str(t.id)) for t in tasks))
    title_width = max(5, max(len(t.title) for t in tasks))
    done_width = 4
    priority_width = 8

    # Header
    print(f"{'ID':<{id_width}}  {'Title':<{title_width}}  {'Done':<{done_width}}  {'Priority':<{priority_width}}  {'Tags'}")

    # Rows
    for task in tasks:
        done_str = "Y" if task.done_at else "N"
        tags_str = ", ".join(task.tags) if task.tags else "-"
        print(
            f"{task.id:<{id_width}}  "
            f"{task.title:<{title_width}}  "
            f"{done_str:<{done_width}}  "
            f"{task.priority.value:<{priority_width}}  "
            f"{tags_str}"
        )


def _print_stats(stats: dict) -> None:
    """Print task statistics."""
    print(f"Total tasks: {stats['total']}")
    print(f"Completed: {stats['completed']}")
    print(f"By priority:")
    for priority in ["LOW", "MEDIUM", "HIGH"]:
        count = stats['by_priority'].get(priority, 0)
        print(f"  {priority.lower()}: {count}")
    print(f"Top tags:")
    for tag, count in stats['top_tags']:
        print(f"  {tag}: {count}")


def main() -> None:
    """Main entry point."""
    args = parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()