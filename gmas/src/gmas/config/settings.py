import os
from pathlib import Path

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["FrameworkSettings", "load_env_file", "load_settings"]


class FrameworkSettings(BaseSettings):
    """
    Framework settings loaded from the environment with the `GMAS_` prefix.

    Key fields:
        - `api_key` / `api_key_file`: secret key directly or path to a file.
        - `base_url`: base URL for the LLM service.
        - `model_name`: generation model identifier.
        - `embedding_model`: embedding model identifier.
        - `log_*`: logging parameters.
        - `default_timeout`, `max_retries`: network timeouts and retries.
    """

    model_config = SettingsConfigDict(env_prefix="GMAS_", extra="ignore")

    api_key: SecretStr | None = Field(default=None, description="API key for LLM service")
    api_key_file: Path | None = Field(
        default=None,
        description="Path to a file that stores the API key securely",
    )
    base_url: str | None = Field(default=None, description="Base URL for LLM service")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model identifier")
    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description="Embedding model identifier",
    )
    embedding_normalize: bool = Field(default=True, description="Normalize embeddings")
    embedding_batch_size: int = Field(default=32, description="Batch size for embedding inference")
    embedding_fallback_dim: int = Field(default=384, description="Fallback dimension")

    vector_store_type: str = Field(default="faiss", description="Vector store backend: faiss | qdrant")
    vector_top_k: int = Field(default=5, description="Default number of results for vector search")
    vector_score_threshold: float = Field(default=0.0, description="Minimum similarity score threshold")
    vector_max_context_tokens: int = Field(default=4000, description="Max tokens in assembled context")
    vector_citation_mode: str = Field(default="inline", description="Citation style: inline | footnote | none")
    vector_strict_context: bool = Field(default=False, description="Only use retrieved context, no LLM knowledge")
    vector_context_template: str = Field(
        default="[{id}] {text}",
        description="Template for formatting context chunks",
    )

    log_level: str = Field(default="INFO", description="Logging level")
    log_file: str | None = Field(default=None, description="Log file path")
    log_backtrace: bool = Field(default=False, description="Enable backtrace")
    default_timeout: int = Field(default=60, description="Default timeout in seconds")
    max_retries: int = Field(default=3, description="Max retries for LLM calls")

    @field_validator("embedding_model")
    @classmethod
    def _validate_embedding_model(cls, value: str) -> str:
        """Validate the embedding model name."""
        if value == "hash" or value.startswith("hash:"):
            return value
        if value.startswith(("sentence-transformers/", "sentence-transformers:")):
            return value
        msg = "Unsupported embedding model. Use 'sentence-transformers/<model>' or 'hash[:<dim>]'"
        raise ValueError(msg)

    @field_validator("*", mode="before")
    @classmethod
    def _handle_empty_strings(cls, value):
        """Convert empty strings to None for correct validation."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("api_key_file")
    @classmethod
    def _validate_api_key_file(cls, value: Path | None) -> Path | None:
        """Ensure the key file exists before reading."""
        if value is None:
            return None
        if not value.is_file():
            msg = f"API key file not found: {value}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _load_secret_key(self) -> "FrameworkSettings":
        """Load the key from a file if not set directly, and require its presence."""
        if self.api_key is None and self.api_key_file is not None:
            content = self.api_key_file.read_text(encoding="utf-8").strip()
            if not content:
                msg = "API key file is empty"
                raise ValueError(msg)
            object.__setattr__(self, "api_key", SecretStr(content))

        if self.api_key is None:
            msg = "api_key is required via GMAS_API_KEY or GMAS_API_KEY_FILE"
            raise ValueError(msg)

        return self

    @property
    def resolved_api_key(self) -> str:
        """Return the secret api_key value or raise an error if it is absent."""
        if self.api_key is None:
            msg = "API key is not configured"
            raise RuntimeError(msg)

        return self.api_key.get_secret_value()


def load_env_file(path: Path | str | None = None) -> None:
    """
    Load environment variables from a .env file (if it exists).

    Args:
        path: Path to the .env file; defaults to the current directory.

    """
    env_path = Path(path or ".env")
    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue

            cleaned = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = cleaned


def load_settings(path: Path | str | None = None) -> FrameworkSettings:
    """
    Read the .env file (if provided), load, and validate settings.

    Args:
        path: Path to the .env file for pre-loading the environment.

    Returns:
        Validated `FrameworkSettings` instance.

    Raises:
        RuntimeError: if settings validation failed.

    """
    load_env_file(path)

    try:
        settings = FrameworkSettings()
    except ValidationError as exc:
        messages = [err.get("msg", "invalid configuration value") for err in exc.errors()]
        detail = "; ".join(messages)
        raise RuntimeError(detail) from exc

    return settings
