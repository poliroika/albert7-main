"""Tests for backup pruning in ``umbrella.control_plane.workspace_code_update``."""

import os
import time
from pathlib import Path

from umbrella.control_plane.workspace_code_update import (
    _prune_old_backups,
    backup_seed_workspace,
)


def _make_seed(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    (seed / "agents").mkdir(parents=True)
    (seed / "agents" / "demo.toml").write_text("name = 'demo'\n", encoding="utf-8")
    (seed / "workspace.toml").write_text("[workspace]\n", encoding="utf-8")
    return seed


def test_prune_old_backups_keeps_newest(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for idx in range(5):
        path = backup_dir / f"seed_backup_2026010{idx}_000000"
        path.mkdir()
        os.utime(path, (time.time() + idx, time.time() + idx))
    (backup_dir / "unrelated_artifact").mkdir()

    _prune_old_backups(backup_dir, keep_last=2)

    survivors = sorted(p.name for p in backup_dir.iterdir())
    assert "unrelated_artifact" in survivors
    seed_survivors = [s for s in survivors if s.startswith("seed_backup_")]
    assert len(seed_survivors) == 2
    assert "seed_backup_20260104_000000" in seed_survivors
    assert "seed_backup_20260103_000000" in seed_survivors


def test_prune_old_backups_no_op_when_under_limit(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "seed_backup_alpha").mkdir()
    (backup_dir / "seed_backup_beta").mkdir()

    _prune_old_backups(backup_dir, keep_last=10)

    assert {p.name for p in backup_dir.iterdir()} == {
        "seed_backup_alpha",
        "seed_backup_beta",
    }


def test_backup_seed_workspace_prunes_after_create(tmp_path: Path) -> None:
    seed = _make_seed(tmp_path)
    backup_dir = tmp_path / "all_backups"
    backup_dir.mkdir()

    for idx in range(4):
        existing = backup_dir / f"seed_backup_old_{idx}"
        existing.mkdir()
        os.utime(existing, (idx, idx))

    new_backup = backup_seed_workspace(seed, backup_dir, keep_last=2)

    assert new_backup.exists()
    surviving = {
        p.name for p in backup_dir.iterdir() if p.name.startswith("seed_backup_")
    }
    assert new_backup.name in surviving
    assert len(surviving) == 2
