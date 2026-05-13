"""Search set construction and persistence.

A search set is a curated collection of tasks used to evaluate harness
candidates.  Tasks can be sourced from memory failures, recent runs,
workspace TASK_MAIN files, or manual JSON definitions.
"""

import json
import logging
from pathlib import Path

from umbrella.meta_harness.models import SearchSet, SearchTask, generate_search_set_id

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_search_set(path: Path) -> SearchSet:
    data = json.loads(path.read_text(encoding="utf-8"))
    return SearchSet(**data)


def write_search_set(path: Path, search_set: SearchSet) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(search_set.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_search_set_from_memory(
    repo_root: Path,
    *,
    limit: int = 30,
    name: str = "memory_failures",
) -> SearchSet:
    """Build a search set from memory lessons tagged as failures/partial."""
    lessons_path = repo_root / ".umbrella" / "memory" / "lessons.jsonl"
    tasks: list[SearchTask] = []

    if not lessons_path.exists():
        return SearchSet(id=generate_search_set_id(), name=name, tasks=tasks)

    try:
        for line in lessons_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            tags = set(data.get("tags", []))
            failure_tags = {
                "eval_failure",
                "partial",
                "HIGH_COST_NO_GAIN",
                "RETRIEVAL_MISSES",
                "failure",
            }
            if not tags.intersection(failure_tags):
                continue

            workspace_id = data.get("workspace_id", "")
            if not workspace_id:
                continue

            task_text = data.get("change_summary", "") or data.get("conclusion", "")
            if not task_text:
                continue

            tasks.append(
                SearchTask(
                    task_id=data.get("task_id", data.get("id", "")),
                    workspace_id=workspace_id,
                    task_text=task_text[:1000],
                    source="memory_failure",
                    tags=tags,
                    difficulty=4,
                )
            )

            if len(tasks) >= limit:
                break
    except Exception:
        log.warning("Failed to build search set from memory", exc_info=True)

    return SearchSet(id=generate_search_set_id(), name=name, tasks=tasks)


def build_search_set_from_workspaces(
    repo_root: Path,
    *,
    limit: int = 30,
    name: str = "workspace_tasks",
) -> SearchSet:
    """Build a search set from workspace TASK_MAIN.md files."""
    workspaces_dir = repo_root / "workspaces"
    tasks: list[SearchTask] = []

    if not workspaces_dir.exists():
        return SearchSet(id=generate_search_set_id(), name=name, tasks=tasks)

    for ws_dir in sorted(workspaces_dir.iterdir()):
        if not ws_dir.is_dir():
            continue
        task_file = ws_dir / "TASK_MAIN.md"
        if not task_file.exists():
            continue

        try:
            task_text = task_file.read_text(encoding="utf-8")[:2000]
        except Exception:
            continue

        workspace_id = ws_dir.name

        # Check for validation commands
        validation_commands: list[list[str]] = []
        smoke_test = ws_dir / "test_smoke.py"
        if smoke_test.exists():
            validation_commands.append(
                ["python", "-m", "pytest", str(smoke_test), "-q"]
            )

        tasks.append(
            SearchTask(
                task_id=f"ws_{workspace_id}",
                workspace_id=workspace_id,
                task_text=task_text,
                source="manual",
                validation_commands=validation_commands,
                difficulty=3,
            )
        )

        if len(tasks) >= limit:
            break

    return SearchSet(id=generate_search_set_id(), name=name, tasks=tasks)


def build_search_set_from_recent_runs(
    repo_root: Path,
    *,
    limit: int = 30,
    name: str = "recent_runs",
) -> SearchSet:
    """Build a search set from recent workspace run results."""
    tasks: list[SearchTask] = []
    meta_harness_dir = repo_root / ".umbrella" / "meta_harness" / "experiments"

    if not meta_harness_dir.exists():
        return SearchSet(id=generate_search_set_id(), name=name, tasks=tasks)

    seen_workspaces: set[str] = set()

    for exp_dir in sorted(meta_harness_dir.iterdir(), reverse=True):
        candidates_dir = exp_dir / "candidates"
        if not candidates_dir.is_dir():
            continue
        for cand_dir in sorted(candidates_dir.iterdir(), reverse=True):
            manifest_path = cand_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            workspace_id = data.get("workspace_id", "")
            if not workspace_id or workspace_id in seen_workspaces:
                continue
            seen_workspaces.add(workspace_id)

            run_status = data.get("run_status", "")
            if run_status not in ("error", "incomplete"):
                continue

            task_text = data.get("task_description", "")[:1000]
            if not task_text:
                continue

            tasks.append(
                SearchTask(
                    task_id=data.get("task_id", ""),
                    workspace_id=workspace_id,
                    task_text=task_text,
                    source="workspace_run",
                    difficulty=4,
                )
            )

            if len(tasks) >= limit:
                break
        if len(tasks) >= limit:
            break

    return SearchSet(id=generate_search_set_id(), name=name, tasks=tasks)


def merge_search_sets(*sets: SearchSet, name: str = "merged") -> SearchSet:
    """Merge multiple search sets, deduplicating by task_id."""
    seen: set[str] = set()
    tasks: list[SearchTask] = []
    for ss in sets:
        for task in ss.tasks:
            if task.task_id not in seen:
                seen.add(task.task_id)
                tasks.append(task)
    return SearchSet(id=generate_search_set_id(), name=name, tasks=tasks)
