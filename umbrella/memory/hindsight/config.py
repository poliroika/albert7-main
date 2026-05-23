"""Environment-backed Hindsight configuration."""

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class HindsightConfig:
    enabled: bool = False
    base_url: str = "http://localhost:8888"
    api_key: str = ""
    timeout_seconds: float = 30.0
    embedded: bool = False
    profile: str = "umbrella-dev"
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    retain_async: bool = True
    fail_closed: bool = False
    reflect_enabled: bool = False
    max_candidates: int = 3
    backend_mode: str = "canonical"

    @classmethod
    def from_env(cls) -> "HindsightConfig":
        return cls(
            enabled=_env_bool("UMBRELLA_HINDSIGHT_ENABLED", False),
            base_url=os.environ.get(
                "UMBRELLA_HINDSIGHT_BASE_URL", "http://localhost:8888"
            ).strip()
            or "http://localhost:8888",
            api_key=os.environ.get("UMBRELLA_HINDSIGHT_API_KEY", "").strip(),
            timeout_seconds=_env_float("UMBRELLA_HINDSIGHT_TIMEOUT_SECONDS", 30.0),
            embedded=_env_bool("UMBRELLA_HINDSIGHT_EMBEDDED", False),
            profile=os.environ.get(
                "UMBRELLA_HINDSIGHT_PROFILE", "umbrella-dev"
            ).strip()
            or "umbrella-dev",
            llm_provider=os.environ.get(
                "UMBRELLA_HINDSIGHT_LLM_PROVIDER", "openai"
            ).strip()
            or "openai",
            llm_model=os.environ.get(
                "UMBRELLA_HINDSIGHT_LLM_MODEL", "gpt-4o-mini"
            ).strip()
            or "gpt-4o-mini",
            retain_async=_env_bool("UMBRELLA_HINDSIGHT_RETAIN_ASYNC", True),
            fail_closed=_env_bool("UMBRELLA_HINDSIGHT_FAIL_CLOSED", False),
            reflect_enabled=_env_bool("UMBRELLA_HINDSIGHT_REFLECT_ENABLED", False),
            max_candidates=max(
                1, min(20, _env_int("UMBRELLA_HINDSIGHT_MAX_CANDIDATES", 3))
            ),
            backend_mode=os.environ.get(
                "UMBRELLA_MEMORY_DURABLE_BACKEND", "canonical"
            )
            .strip()
            .lower()
            or "canonical",
        )
