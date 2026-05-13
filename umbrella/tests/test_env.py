import os
from pathlib import Path

from umbrella.env import (
    get_llm_env_config,
    load_env,
    read_default_llm_model_from_repo_dotenv,
)


def test_load_env_prefers_repo_root(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo"
    cwd_root = tmp_path / "cwd"
    repo_root.mkdir()
    cwd_root.mkdir()
    (repo_root / ".env").write_text("LLM_MODEL=repo-model\n", encoding="utf-8")
    (cwd_root / ".env").write_text("LLM_MODEL=cwd-model\n", encoding="utf-8")

    monkeypatch.chdir(cwd_root)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    loaded_path = load_env(repo_root=repo_root)

    assert loaded_path == (repo_root / ".env").resolve()
    assert os.environ["LLM_MODEL"] == "repo-model"


def test_load_env_uses_extra_search_roots(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo"
    workspace_root = repo_root / "workspaces" / "agent_research"
    cwd_root = tmp_path / "cwd"
    workspace_root.mkdir(parents=True)
    cwd_root.mkdir()
    (workspace_root / ".env").write_text(
        "LLM_BASE_URL=http://workspace.local/v1\n", encoding="utf-8"
    )

    monkeypatch.chdir(cwd_root)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    loaded_path = load_env(repo_root=repo_root, extra_search_roots=(workspace_root,))

    assert loaded_path == (workspace_root / ".env").resolve()
    assert os.environ["LLM_BASE_URL"] == "http://workspace.local/v1"


def test_load_env_does_not_override_existing_values(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".env").write_text("LLM_API_KEY=file-key\n", encoding="utf-8")

    monkeypatch.setenv("LLM_API_KEY", "existing-key")

    load_env(repo_root=repo_root)

    assert os.environ["LLM_API_KEY"] == "existing-key"


def test_read_default_llm_model_from_repo_dotenv(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / ".env").write_text(
        "# x\nOUROBOROS_MODEL=GLM-5.1-FP8\nLLM_MODEL=ignored-when-ouro-set\n",
        encoding="utf-8",
    )
    assert read_default_llm_model_from_repo_dotenv(repo) == "GLM-5.1-FP8"

    (repo / ".env").write_text("LLM_MODEL=only-llm\n", encoding="utf-8")
    assert read_default_llm_model_from_repo_dotenv(repo) == "only-llm"


def test_get_llm_env_config_falls_back_to_openai_key(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gpt-oss")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://example.test/v1")

    assert get_llm_env_config() == (
        "gpt-oss",
        "openai-key",
        "http://example.test/v1",
    )
