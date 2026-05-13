# CLI Tasks Tracker

A simple command-line task tracker with priorities, tags, and JSON storage. Built with pure Python standard library - no external dependencies.

## Features

- Add tasks with title, priority, and tags
- List tasks with filtering by priority, tags, and done status
- Mark tasks as done/undone
- Search tasks by title
- View task statistics
- Persistent JSON storage with atomic writes

## Requirements

- Python 3.11 or higher
- No external dependencies (standard library only)

## Installation

1. Clone or navigate to the workspace directory
2. Create a virtual environment (recommended):

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. No pip install needed - all code uses Python standard library

## Usage

Add a new task:
```bash
python main.py add "Buy groceries" --priority high --tag shopping
```

List all tasks:
```bash
python main.py list
```

List completed tasks only:
```bash
python main.py list --done true
```

List high priority tasks:
```bash
python main.py list --priority high
```

List tasks with specific tag:
```bash
python main.py list --tag shopping
```

Mark task as done:
```bash
python main.py done 1
```

Mark task as undone:
```bash
python main.py undo 1
```

Search tasks by title:
```bash
python main.py search "groceries"
```

View statistics:
```bash
python main.py stats
```

## Command Reference

### add
Create a new task.
```
python main.py add "<title>" [--priority low|medium|high] [--tag <name>...]
```
- `--priority`: Task priority (default: medium)
- `--tag`: One or more tags (optional)

### list
List tasks with optional filtering.
```
python main.py list [--priority <priority>] [--tag <name>] [--done true|false]
```
- `--priority`: Filter by priority level
- `--tag`: Filter by tag
- `--done`: Filter by done status (true/false, case-insensitive)

### done
Mark a task as done.
```
python main.py done <id>
```
- Exits with error if task ID doesn't exist

### undo
Remove done status from a task.
```
python main.py undo <id>
```
- Exits with error if task ID doesn't exist

### search
Search tasks by title substring.
```
python main.py search "<query>"
```
- Case-insensitive substring search in task titles

### stats
Display task statistics.
```
python main.py stats
```
Shows:
- Total tasks
- Completed tasks
- Tasks per priority level
- Top 5 tags by task count

## Data Storage

Tasks are stored in `data/tasks.json` in JSON format. The file is created automatically on first run. All writes are atomic (uses temporary file + `os.replace`).

## Testing

Run the test suite:
```bash
python -m pytest tests -q
```

## Architecture

- `main.py` - Entry point
- `tracker/models.py` - Data models (Task, Priority enum)
- `tracker/storage.py` - JSON storage with atomic writes
- `tracker/service.py` - Business logic layer
- `tracker/cli.py` - CLI interface with argparse