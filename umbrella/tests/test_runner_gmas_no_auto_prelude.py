from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_runner_does_not_inject_gmas_execute_prelude() -> None:
    runner_src = (REPO / "umbrella" / "orchestrator" / "runner.py").read_text(
        encoding="utf-8"
    )
    assert "_inject_gmas_prewrite_context" not in runner_src
    assert "umbrella_phase_prelude" not in runner_src
    assert "## Umbrella execute prelude: GMAS context" not in runner_src


def test_worker_gmas_hint_requires_explicit_agent_fetch() -> None:
    worker_src = (REPO / "umbrella" / "orchestrator" / "worker.py").read_text(
        encoding="utf-8"
    )
    assert "Umbrella уже подгрузил" not in worker_src
    assert "Umbrella already loaded" not in worker_src
    assert "get_gmas_context" in worker_src
