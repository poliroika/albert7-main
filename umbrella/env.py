import logging
import os
from pathlib import Path
from collections.abc import Iterable

log = logging.getLogger(__name__)

DEFAULT_WORKSPACE_LLM_MODEL = "gpt-4.1-mini"
DEFAULT_CODE_ANALYZER_MODEL = "claude-opus-4"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

WATCHER_BUDGET_ENABLED_ENV = "UMBRELLA_WATCHER_BUDGET_ENABLED"


def env_truthy(name: str) -> bool:
    """Return whether ``os.environ[name]`` is a truthy flag (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def watcher_budget_enforcement_enabled() -> bool:
    """Whether watcher enforces phase ``{id}.budget.json`` time/tool-call limits.

    Configure via ``UMBRELLA_WATCHER_BUDGET_ENABLED`` in repo ``.env`` (see ``load_env``).
    """
    return env_truthy(WATCHER_BUDGET_ENABLED_ENV)


def _iter_env_candidates(
    *,
    repo_root: Path,
    extra_search_roots: Iterable[Path] = (),
) -> list[Path]:
    candidates = [
        repo_root / ".env",
        *(Path(root) / ".env" for root in extra_search_roots),
        Path.cwd() / ".env",
    ]
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def _load_simple_env_file(env_path: Path, *, override: bool = False) -> None:
    """Load a minimal .env file without requiring python-dotenv."""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not override and key in os.environ:
            continue
        os.environ[key] = value


def read_default_llm_model_from_repo_dotenv(repo_root: Path) -> str | None:
    """Read ``OUROBOROS_MODEL`` / ``LLM_MODEL`` from ``<repo_root>/.env`` only (no ``os.environ``).

    Used by web bridge so the Settings UI matches the file after edits even when
    ``load_dotenv(override=False)`` left stale model ids from the parent shell.
    """
    path = Path(repo_root).resolve() / ".env"
    if not path.is_file():
        return None
    ouro, llm = "", ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if key not in ("OUROBOROS_MODEL", "LLM_MODEL"):
            continue
        value = rest.split("#", 1)[0].strip() if rest else ""
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key == "OUROBOROS_MODEL":
            ouro = value
        else:
            llm = value
    pick = (ouro or llm).strip()
    return pick or None


def load_env(
    *,
    repo_root: Path | None = None,
    extra_search_roots: Iterable[Path] = (),
    override: bool = False,
) -> Path | None:
    """Load the first available .env file into os.environ."""
    search_root = (repo_root or Path(__file__).resolve().parents[1]).resolve()

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    except Exception as exc:
        log.debug("Failed to import python-dotenv: %s", exc)
        load_dotenv = None

    for env_path in _iter_env_candidates(
        repo_root=search_root,
        extra_search_roots=(Path(root).resolve() for root in extra_search_roots),
    ):
        if not env_path.exists():
            continue
        if load_dotenv is not None:
            load_dotenv(env_path, override=override)
        else:
            _load_simple_env_file(env_path, override=override)
        log.info("Loaded environment variables from %s", env_path)
        # Debug: log key Ouroboros env vars
        log.debug(
            "LLM_API_KEY: %s", "SET" if os.environ.get("LLM_API_KEY") else "NOT SET"
        )
        log.debug("LLM_BASE_URL: %s", os.environ.get("LLM_BASE_URL"))
        log.debug("OUROBOROS_MODEL_LIGHT: %s", os.environ.get("OUROBOROS_MODEL_LIGHT"))
        log.debug(
            "OUROBOROS_LLM_BASE_URL: %s", os.environ.get("OUROBOROS_LLM_BASE_URL")
        )
        return env_path

    return None


def get_llm_env_config() -> tuple[str | None, str | None, str | None]:
    """Return model, api key, and base URL from the current environment."""
    return (
        os.environ.get("LLM_MODEL"),
        (
            os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        ).strip(),
        (os.environ.get("LLM_BASE_URL") or "").strip(),
    )


def get_default_workspace_model() -> str:
    """Return the default model for workspace execution paths."""
    return (
        os.environ.get("LLM_MODEL") or DEFAULT_WORKSPACE_LLM_MODEL
    ).strip() or DEFAULT_WORKSPACE_LLM_MODEL


def get_code_analyzer_model() -> str:
    """Return the model used by code-analysis helpers."""
    return (
        os.environ.get("UMBRELLA_CODE_ANALYZER_MODEL")
        or os.environ.get("LLM_MODEL")
        or DEFAULT_CODE_ANALYZER_MODEL
    ).strip() or DEFAULT_CODE_ANALYZER_MODEL


def get_openai_base_url(base_url: str | None = None) -> str:
    """Return an OpenAI-compatible base URL with a stable default."""
    return (
        base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_OPENAI_BASE_URL
    ).strip() or DEFAULT_OPENAI_BASE_URL


def get_anthropic_base_url(base_url: str | None = None) -> str:
    """Return an Anthropic-compatible base URL with a stable default."""
    return (
        base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_ANTHROPIC_BASE_URL
    ).strip() or DEFAULT_ANTHROPIC_BASE_URL
