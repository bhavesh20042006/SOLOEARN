"""Structured logging module for SOLOEARN.

Provides JSON-formatted structured logging with sensitive data redaction,
rotating file handlers, and contextual log enrichment.

Usage:
    from core.structured_logging import setup_logging, get_logger, LogContext

    setup_logging(log_level="INFO", json_format=True, log_file="logs/soloearn.log")
    logger = get_logger(__name__)

    with LogContext(request_id="abc-123", client_name="Acme"):
        logger.info("Processing request", extra={"amount": 5000})
"""

from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Generator


# ---------------------------------------------------------------------------
# Sensitive-pattern redaction
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # key=value patterns (headers, query strings, log messages)
    (re.compile(
        r'(?i)(upi_pin|mpin|password|passwd|pwd|otp|cvv|pin|secret|token|auth_key)'
        r'[\s]*[=:]\s*["\']?([^"\'\s,;}{)\]]+)',
    ), r'\1=***REDACTED***'),
    # JSON-style "key": "value" patterns
    (re.compile(
        r'(?i)("(?:upi_pin|mpin|password|passwd|pwd|otp|cvv|pin|secret|token|auth_key)"'
        r'\s*:\s*)"[^"]*"',
    ), r'\1"***REDACTED***"'),
]


def _redact(message: str) -> str:
    """Remove sensitive values from a log message string."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


# ---------------------------------------------------------------------------
# Redaction filter
# ---------------------------------------------------------------------------

class _RedactionFilter(logging.Filter):
    """Logging filter that redacts sensitive patterns from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _redact(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class _JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Merge extra context fields injected by LogContext or caller
        if hasattr(record, "_context_fields"):
            log_entry.update(record._context_fields)  # type: ignore[attr-defined]

        # Merge any explicit extra={} keys the caller passed, skipping stdlib internals
        _stdlib_keys = {
            "name", "msg", "args", "created", "relativeCreated",
            "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "pathname", "filename", "module", "levelno", "levelname",
            "thread", "threadName", "process", "processName",
            "message", "msecs", "taskName", "_context_fields",
        }
        for key, value in record.__dict__.items():
            if key not in _stdlib_keys and not key.startswith("_"):
                log_entry[key] = value

        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Human-readable console formatter
# ---------------------------------------------------------------------------

class _ConsoleFormatter(logging.Formatter):
    """Compact, colourless, human-readable format for console output."""

    _FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self._FMT, datefmt="%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Log context manager (thread-local context fields)
# ---------------------------------------------------------------------------

_context_var: threading.local = threading.local()


class _ContextInjector(logging.Filter):
    """Injects thread-local context fields into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        fields: dict[str, Any] = getattr(_context_var, "fields", {})
        if fields:
            record._context_fields = dict(fields)  # type: ignore[attr-defined]
        return True


@contextmanager
def LogContext(**fields: Any) -> Generator[None, None, None]:
    """Context manager that attaches extra fields to all log records within its scope.

    Usage:
        with LogContext(request_id="r-42", client_name="Acme", cycle_id=7):
            logger.info("Starting work")  # JSON output will include those keys
    """
    previous: dict[str, Any] = getattr(_context_var, "fields", {})
    merged = {**previous, **fields}
    _context_var.fields = merged
    try:
        yield
    finally:
        _context_var.fields = previous


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def setup_logging(
    log_level: str = "INFO",
    json_format: bool = True,
    log_file: str | None = None,
) -> None:
    """Configure the root logger for SOLOEARN.

    Args:
        log_level: One of DEBUG, INFO, WARNING, ERROR, CRITICAL.
        json_format: If True, the *file* handler uses JSON; console is always human-readable.
        log_file: Optional path for a rotating log file (JSON formatted).
                  Parent directories are created automatically.
    """
    root = logging.getLogger()
    level = getattr(logging, log_level.upper(), logging.INFO)
    root.setLevel(level)

    # Remove pre-existing handlers to allow safe re-initialisation
    root.handlers.clear()

    # Shared filters
    redaction_filter = _RedactionFilter()
    context_filter = _ContextInjector()

    # --- Console handler (always human-readable) ---
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(_ConsoleFormatter())
    console.addFilter(redaction_filter)
    console.addFilter(context_filter)
    root.addHandler(console)

    # --- Optional rotating file handler (JSON) ---
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        if json_format:
            file_handler.setFormatter(_JSONFormatter())
        else:
            file_handler.setFormatter(_ConsoleFormatter())
        file_handler.addFilter(redaction_filter)
        file_handler.addFilter(context_filter)
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  Call ``setup_logging()`` first to configure handlers."""
    return logging.getLogger(name)
