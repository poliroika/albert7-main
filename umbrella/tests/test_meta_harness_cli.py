"""Tests for Meta-Harness CLI."""

import argparse

import pytest

from umbrella.meta_harness.cli import (
    cmd_diff,
    cmd_failures,
    cmd_list,
    cmd_show,
    cmd_top,
)
from umbrella.meta_harness.models import (
    CandidateEval,
    CandidateManifest,
)
from umbrella.meta_harness.store import MetaHarnessStore


@pytest.fixture
def populated_store(tmp_path):
    store = MetaHarnessStore(tmp_path / ".umbrella" / "meta_harness")
    exp = store.create_experiment(name="test_exp", workspace_id="ws1")

    for i, (status, score) in enumerate(
        [("complete", 0.8), ("error", 0.2), ("complete", 0.6)]
    ):
        m = CandidateManifest(
            experiment_id=exp.id,
            task_id=f"task_{i}",
            workspace_id="ws1",
            run_status=status,
            write_calls=i + 1,
            cost_usd=0.1 * (i + 1),
            error="test error" if status == "error" else "",
        )
        store.save_candidate(m)
        store.save_eval(CandidateEval(candidate_id=m.candidate_id, avg_score=score))

    return store, tmp_path


def _make_args(tmp_path, **kwargs):
    ns = argparse.Namespace(repo_root=str(tmp_path), **kwargs)
    return ns


class TestCmdList:
    def test_lists_experiments(self, populated_store, capsys):
        store, tmp_path = populated_store
        args = _make_args(tmp_path)
        cmd_list(args)
        captured = capsys.readouterr()
        assert "test_exp" in captured.out or "exp_" in captured.out

    def test_empty_store(self, tmp_path, capsys):
        MetaHarnessStore(tmp_path / ".umbrella" / "meta_harness")
        args = _make_args(tmp_path)
        cmd_list(args)
        captured = capsys.readouterr()
        assert "No experiments" in captured.out


class TestCmdTop:
    def test_shows_top(self, populated_store, capsys):
        store, tmp_path = populated_store
        exp = store.get_latest_experiment()
        args = _make_args(tmp_path, experiment=exp.id, n=2, sort="score")
        cmd_top(args)
        captured = capsys.readouterr()
        assert "0.800" in captured.out


class TestCmdShow:
    def test_shows_candidate(self, populated_store, capsys):
        store, tmp_path = populated_store
        exp = store.get_latest_experiment()
        candidates = store.list_candidates(exp.id)
        cid = candidates[0].candidate_id
        args = _make_args(tmp_path, candidate_id=cid)
        cmd_show(args)
        captured = capsys.readouterr()
        assert cid in captured.out


class TestCmdDiff:
    def test_compares_candidates(self, populated_store, capsys):
        store, tmp_path = populated_store
        exp = store.get_latest_experiment()
        candidates = store.list_candidates(exp.id)
        args = _make_args(
            tmp_path,
            candidate_a=candidates[0].candidate_id,
            candidate_b=candidates[2].candidate_id,
        )
        cmd_diff(args)
        captured = capsys.readouterr()
        assert "Comparing" in captured.out


class TestCmdFailures:
    def test_shows_failures(self, populated_store, capsys):
        store, tmp_path = populated_store
        exp = store.get_latest_experiment()
        args = _make_args(tmp_path, experiment=exp.id, workspace="")
        cmd_failures(args)
        captured = capsys.readouterr()
        assert "error" in captured.out.lower()
