"""Structured JSON logging configuration for Modulo.

Configures python-json-logger with:
- Standard fields: timestamp, level, logger, module, function, line
- correlation_id from middleware contextvar
- Configurable log level per module via MODULO_LOG_LEVEL env var
- Configurable per-module override via MODULO_LOG_LEVEL_<MODULE_PATH>
- Sensitive field redaction (keys, secrets, tokens → "***")
"""

import logging
import os
import sys
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import jsonlogger

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "api_key", "api_secret", "access_key", "secret_key", "token",
    "password", "passwd", "secret", "private_key", "credential",
    "fernet_key", "auth_token", "bearer_token", "refresh_token",
    "client_secret", "client_id", "session_key", "encryption_key",
})


def redact_sensitive(extra: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *extra* with sensitive values replaced by '***'."""
    redacted: dict[str, Any] = {}
    for key, value in extra.items():
        if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
            redacted[key] = "***"
        elif isinstance(value, dict):
            redacted[key] = redact_sensitive(value)
        else:
            redacted[key] = value
    return redacted


class CorrelationIdFilter(logging.Filter):
    """Inject correlation_id from contextvar into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        cid = correlation_id_var.get()
        if cid is not None:
            record.correlation_id = cid
        else:
            record.correlation_id = ""
        return True


class SensitiveFieldFilter(logging.Filter):
    """Redact sensitive field values in the extra dict of each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in _SENSITIVE_KEYS:
            if hasattr(record, key):
                setattr(record, key, "***")
        return True


def _resolve_log_level(module_name: str) -> str:
    """Resolve log level for a module, checking per-module overrides first."""
    env_key = f"MODULO_LOG_LEVEL_{module_name.upper().replace('.', '_')}"
    override = os.environ.get(env_key)
    if override:
        return override.upper()
    return os.environ.get("MODULO_LOG_LEVEL", "INFO").upper()


def configure_logging() -> None:
    """Configure the root logger for structured JSON output.

    Call once at application startup.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

    formatter = jsonlogger.JsonFormatter(
        fmt="%(timestamp)s %(level)s %(name)s %(module)s %(funcName)s %(lineno)d %(message)s %(correlation_id)s",
        rename_fields={
            "timestamp": "timestamp",
            "level": "level",
            "name": "logger",
            "module": "module",
            "funcName": "function",
            "lineno": "line",
            "correlation_id": "correlation_id",
        },
        timestamp=True,
    )
    handler.setFormatter(formatter)
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(SensitiveFieldFilter())

    root_logger.addHandler(handler)

    _apply_per_module_levels()


def _apply_per_module_levels() -> None:
    """Apply per-module log levels from MODULO_LOG_LEVEL_<MODULE> env vars."""
    seen: set[str] = set()
    for env_name, value in os.environ.items():
        if not env_name.startswith("MODULO_LOG_LEVEL_"):
            continue
        module_path = env_name[len("MODULO_LOG_LEVEL_"):].lower().replace("_", ".")
        if module_path in seen:
            continue
        seen.add(module_path)
        level = value.upper()
        logger = logging.getLogger(module_path)
        logger.setLevel(getattr(logging, level, logging.INFO))
        logger.debug("Log level set to %s for %s", level, module_path)
