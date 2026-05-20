def test_task_artifact_stem_is_windows_safe_for_phase_ids() -> None:
    from ouroboros.utils import task_artifact_stem
    from umbrella.artifacts.task_ids import task_artifact_stem as umbrella_task_stem

    stem = task_artifact_stem("phase_web_2c33fc4f:research")

    assert ":" not in stem
    assert stem.startswith("phase_web_2c33fc4f_research")
    assert task_artifact_stem("phase_web_2c33fc4f:research") == stem
    assert umbrella_task_stem("phase_web_2c33fc4f:research") == stem


def test_task_artifact_stem_preserves_plain_ids() -> None:
    from ouroboros.utils import task_artifact_stem

    assert task_artifact_stem("task_abc-123") == "task_abc-123"
