import os
import sys

try:
    from loguru import logger as _logger
except ImportError:  # pragma: no cover - exercised when optional deps are absent
    import logging as _stdlib_logging

    class _StdlibLogger:
        def _format(self, message: object, *args: object, **kwargs: object) -> str:
            text = str(message)
            if args or kwargs:
                try:
                    text = text.format(*args, **kwargs)
                except Exception:
                    suffix = " ".join(str(item) for item in args)
                    text = f"{text} {suffix}".strip()
            return text

        def debug(self, message: object, *args: object, **kwargs: object) -> None:
            _stdlib_logging.getLogger("gmas").debug(
                self._format(message, *args, **kwargs)
            )

        def info(self, message: object, *args: object, **kwargs: object) -> None:
            _stdlib_logging.getLogger("gmas").info(
                self._format(message, *args, **kwargs)
            )

        def warning(self, message: object, *args: object, **kwargs: object) -> None:
            _stdlib_logging.getLogger("gmas").warning(
                self._format(message, *args, **kwargs)
            )

        def error(self, message: object, *args: object, **kwargs: object) -> None:
            _stdlib_logging.getLogger("gmas").error(
                self._format(message, *args, **kwargs)
            )

        def exception(self, message: object, *args: object, **kwargs: object) -> None:
            _stdlib_logging.getLogger("gmas").exception(
                self._format(message, *args, **kwargs)
            )

        def __getattr__(self, name: str):
            return getattr(_stdlib_logging.getLogger("gmas"), name)

        def add(self, *args, **kwargs) -> None:
            return None

        def remove(self, *args, **kwargs) -> None:
            return None

    _logger = _StdlibLogger()

__all__ = ["logger", "setup_logging"]

logger = _logger

_DEFAULT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def _as_bool(value: str | None) -> bool:
    """
    Convert a string value to a boolean.

    Interprets `1/true/yes/on` (any case) as `True`; all other values
    or the absence of the variable are interpreted as `False`.
    """
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(
    level: str | None = None,
    *,
    log_file: str | None = None,
    backtrace: bool | None = None,
    format_string: str | None = None,
) -> None:
    """
    Configure the loguru logger for the console and, optionally, a file.

    Args:
        level: Logging level (`DEBUG/INFO/WARNING/ERROR`). If not specified,
            taken from `GMAS_LOG_LEVEL` or defaults to `INFO`.
        log_file: Path to the log file; if `None`, can be set via `GMAS_LOG_FILE`.
        backtrace: Enable detailed backtrace; by default read from
            `GMAS_LOG_BACKTRACE` (`1/true/yes/on` => `True`).
        format_string: Message format; defaults to `GMAS_LOG_FORMAT` or `_DEFAULT_FORMAT`.

    Environment variables for file output:
        - `GMAS_LOG_ROTATION` (e.g. `10 MB`)
        - `GMAS_LOG_RETENTION` (e.g. `7 days`)
        - `GMAS_LOG_COMPRESSION` (e.g. `gz`)

    """
    configured_level = (level or os.getenv("GMAS_LOG_LEVEL") or "INFO").upper()
    configured_format = format_string or os.getenv("GMAS_LOG_FORMAT", _DEFAULT_FORMAT)
    configured_backtrace = backtrace if backtrace is not None else _as_bool(os.getenv("GMAS_LOG_BACKTRACE"))

    logger.remove()
    logger.add(
        sys.stderr,
        level=configured_level,
        format=configured_format,
        backtrace=configured_backtrace,
        diagnose=False,
        enqueue=True,
    )

    destination = log_file or os.getenv("GMAS_LOG_FILE")
    if destination:
        logger.add(
            destination,
            level=configured_level,
            format=configured_format,
            backtrace=configured_backtrace,
            diagnose=False,
            enqueue=True,
            rotation=os.getenv("GMAS_LOG_ROTATION", "10 MB"),
            retention=os.getenv("GMAS_LOG_RETENTION", "7 days"),
            compression=os.getenv("GMAS_LOG_COMPRESSION", "gz"),
        )
