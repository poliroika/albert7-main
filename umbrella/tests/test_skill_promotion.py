from pathlib import Path

import pytest

from umbrella.skills.promotion import promote_skill


class _Experiment:
    def __init__(self):
        self.id = "exp_1"
        self.workspace_id = "agent_research"
        self.baseline_candidate_id = "cand_base"
        self.best_candidate_id = "cand_best"
        self.candidate_ids = ["cand_base", "cand_best"]


class _Store:
    def get_latest_experiment(self):
        return _Experiment()

    def get_search_set(self, experiment_id: str):
        return type("SearchSet", (), {"id": "ss_1", "tasks": [object()]})()


class _Eval:
    def __init__(self, score: float):
        self.avg_score = score


class _DecisionValue:
    def __init__(self, value: str):
        self.value = value


class _Decision:
    def __init__(self, value: str):
        self.decision = _DecisionValue(value)
        self.passes_runtime_verification = True
        self.reasoning = "ok"


def _seed_candidate_skill(repo_root: Path) -> None:
    path = repo_root / "umbrella" / "skills" / "library" / "gmas-role-bootstrap"
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        (
            "---\n"
            "name: gmas-role-bootstrap\n"
            "status: candidate\n"
            "domains: [multi_agent_gmas]\n"
            "when_to_use: use role graph\n"
            "---\n\n"
            "## Steps\n1. bootstrap\n"
        ),
        encoding="utf-8",
    )


def test_promote_skill_uses_meta_harness_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    _seed_candidate_skill(repo_root)

    monkeypatch.setattr(
        "umbrella.skills.promotion.get_default_store", lambda _root: _Store()
    )
    monkeypatch.setattr(
        "umbrella.skills.promotion.evaluate_candidate_on_search_set",
        lambda _repo_root, candidate_id, search_set, store=None: _Eval(
            0.8 if candidate_id == "cand_best" else 0.5
        ),
    )
    monkeypatch.setattr(
        "umbrella.skills.promotion.decide_candidate_promotion",
        lambda *args, **kwargs: _Decision("promote"),
    )

    result = promote_skill(repo_root, "gmas-role-bootstrap")
    assert result.status == "promoted"
    text = (
        repo_root
        / "umbrella"
        / "skills"
        / "library"
        / "gmas-role-bootstrap"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "status: active" in text
