import json
import sys
import tempfile
import types
from pathlib import Path

from umbrella.integration.ouroboros_bridge import (
    resolve_ouroboros_repo_root,
    sync_umbrella_context_to_drive,
    workspace_drive_root,
)
from umbrella.integration.ouroboros_launcher import OuroborosLauncher
from umbrella.memory.models import (
    MemoryConfig,
    WorkspaceLessonRecord,
    generate_lesson_id,
)
from umbrella.memory.store import MemoryStore


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_sync_umbrella_context_to_drive_writes_state_and_memory_bridge() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        (repo_root / "ouroboros").mkdir(parents=True, exist_ok=True)
        memory_root = repo_root / ".umbrella" / "memory"
        store = MemoryStore(
            MemoryConfig(
                memory_root=memory_root,
                lessons_path=memory_root / "lessons.jsonl",
                gaps_path=memory_root / "gaps.jsonl",
                signals_path=memory_root / "signals.jsonl",
            )
        )
        store.add_lesson(
            WorkspaceLessonRecord(
                id=generate_lesson_id(),
                task_id="task_bridge",
                workspace_id="agent_research",
                change_summary="Use retrieval before patching",
                expected_effect="Avoid blind edits",
                observed_effect="Reduced retry churn",
                conclusion="Retrieval-first patches converge faster",
                evidence_summary="Observed across repeated retries",
                tags={"retrieval", "workspace"},
            )
        )

        drive_root = repo_root / ".umbrella" / "ouroboros_drive"
        sync_umbrella_context_to_drive(
            repo_root,
            drive_root,
            workspace_id="agent_research",
            task_input="Summarize the workspace and propose improvements",
            task_id="task_bridge",
        )

        state_payload = json.loads(
            (drive_root / "state" / "state.json").read_text(encoding="utf-8")
        )
        knowledge_index = (drive_root / "memory" / "knowledge" / "_index.md").read_text(
            encoding="utf-8"
        )

        assert Path(state_payload["host_repo_root"]).resolve() == repo_root.resolve()
        assert (
            Path(state_payload["ouroboros_repo_root"]).resolve()
            == (repo_root / "ouroboros").resolve()
        )
        assert "Umbrella Memory Bridge" in knowledge_index
        assert "retrieval-first patches converge faster" in knowledge_index.lower()


def test_workspace_drive_root_is_scoped_to_workspace() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        expected = repo_root / "workspaces" / "news_cards_ai" / ".memory" / "drive"
        assert workspace_drive_root(repo_root, "news_cards_ai") == expected.resolve()
        assert (
            workspace_drive_root(repo_root, "")
            == (repo_root / ".umbrella" / "ouroboros_drive").resolve()
        )


def test_launcher_uses_real_ouroboros_repo_with_host_root(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class _FakeAgent:
        def handle_task(self, task: dict[str, object]) -> list[dict[str, object]]:
            captured["task_id"] = str(task["id"])
            return []

    def _fake_make_agent(
        *,
        repo_dir: str,
        drive_root: str,
        host_repo_root: str | None = None,
        event_queue=None,
    ):
        captured["repo_dir"] = repo_dir
        captured["drive_root"] = drive_root
        captured["host_repo_root"] = host_repo_root or ""
        return _FakeAgent()

    fake_module = types.ModuleType("ouroboros.agent")
    fake_module.make_agent = _fake_make_agent
    monkeypatch.setitem(sys.modules, "ouroboros.agent", fake_module)

    repo_root = _repo_root()
    with tempfile.TemporaryDirectory() as tmpdir:
        launcher = OuroborosLauncher(repo_root=repo_root, drive_root=Path(tmpdir))
        result = launcher._process_task({"id": "task_real_repo", "input": "inspect"})

    assert result["status"] == "complete"
    assert captured["repo_dir"] == str(resolve_ouroboros_repo_root(repo_root))
    assert captured["host_repo_root"] == str(repo_root)


def test_launcher_recovers_from_stale_ouroboros_namespace(monkeypatch) -> None:
    """A prior failed import can leave a namespace package in sys.modules.

    The launcher must clear that stale parent before importing the real
    standalone Ouroboros package, otherwise live runs fail before any tools
    can execute.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        pkg_dir = repo_root / "ouroboros" / "ouroboros"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "agent.py").write_text(
            "class _Agent:\n"
            "    def handle_task(self, task):\n"
            "        return []\n\n"
            "def make_agent(**kwargs):\n"
            "    return _Agent()\n",
            encoding="utf-8",
        )

        stale_parent = types.ModuleType("ouroboros")
        stale_parent.__path__ = [str(repo_root / "ouroboros")]
        monkeypatch.setitem(sys.modules, "ouroboros", stale_parent)
        monkeypatch.delitem(sys.modules, "ouroboros.agent", raising=False)

        launcher = OuroborosLauncher(
            repo_root=repo_root,
            drive_root=repo_root / ".umbrella" / "ouroboros_drive",
        )
        result = launcher._process_task({"id": "task_stale_import", "input": "inspect"})

    assert result["status"] == "complete"


def test_launcher_bridges_task_fields_and_memory_payload(monkeypatch) -> None:
    class _FakeAgent:
        def handle_task(self, task: dict[str, object]) -> list[dict[str, object]]:
            return []

    fake_module = types.ModuleType("ouroboros.agent")
    fake_module.make_agent = lambda **kwargs: _FakeAgent()
    monkeypatch.setitem(sys.modules, "ouroboros.agent", fake_module)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        (repo_root / "ouroboros").mkdir(parents=True, exist_ok=True)
        launcher = OuroborosLauncher(
            repo_root=repo_root,
            drive_root=repo_root / ".umbrella" / "ouroboros_drive",
        )
        result = launcher._process_task(
            {
                "id": "task_bridge_payload",
                "task": "Summarize the workspace and use Umbrella memory.",
                "user_message": "Prefer the retrieval-backed path.",
                "memory": {
                    "task_context": "Bridge payload smoke",
                    "retrieval_summary": "Relevant files already identified by Umbrella.",
                },
            }
        )

        knowledge_index = (
            repo_root
            / ".umbrella"
            / "ouroboros_drive"
            / "memory"
            / "knowledge"
            / "_index.md"
        ).read_text(encoding="utf-8")
        state_payload = json.loads(
            (
                repo_root / ".umbrella" / "ouroboros_drive" / "state" / "state.json"
            ).read_text(encoding="utf-8")
        )

    assert result["status"] == "complete"
    assert "Live Umbrella Task Context" in knowledge_index
    assert "Bridge payload smoke" in knowledge_index
    assert "Prefer the retrieval-backed path." in knowledge_index
    assert (
        state_payload["current_task"]["task_input"]
        == "Summarize the workspace and use Umbrella memory."
    )
    assert state_payload["current_task"]["memory"]["retrieval_summary"] == (
        "Relevant files already identified by Umbrella."
    )
