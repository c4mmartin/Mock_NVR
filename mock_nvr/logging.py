from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


def cleanup_logs(
    *,
    logs_dir: str | Path = "logs",
    retention_days: int | None = None,
    prefixes: tuple[str, ...] = ("mock_nvr_", "supervisor_", "ffmpeg_", "mediamtx_"),
) -> dict:
    """Delete old log files from logs_dir.

    Only touches files that:
    - start with one of `prefixes`, and
    - have a `.log` suffix or `.log.<n>` rotation suffix.

    Returns a dict with counts for observability.
    """

    if retention_days is None or retention_days <= 0:
        return {"enabled": False, "deleted": 0, "kept": 0, "errors": 0}

    logs_path = Path(logs_dir)
    if not logs_path.exists():
        return {"enabled": True, "deleted": 0, "kept": 0, "errors": 0}

    cutoff = time.time() - (retention_days * 86400)

    deleted = 0
    kept = 0
    errors = 0

    for p in logs_path.iterdir():
        if not p.is_file():
            continue

        name = p.name
        if not name.startswith(prefixes):
            continue

        if not (name.endswith(".log") or ".log." in name):
            continue

        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                deleted += 1
            else:
                kept += 1
        except Exception:
            errors += 1

    return {"enabled": True, "deleted": deleted, "kept": kept, "errors": errors}


def configure_logging(
    *,
    name: str,
    logs_dir: str | Path = "logs",
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 5,
    console: bool = True,
    retention_days: int | None = None,
) -> structlog.stdlib.BoundLogger:
    """Configure stdlib logging + structlog.

    - Console logs are human-readable.
    - File logs are rotated by size.

    Returns a structlog logger bound with common process metadata.
    """

    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)

    cleanup = cleanup_logs(logs_dir=logs_path, retention_days=retention_days)

    pid = os.getpid()
    log_path = logs_path / f"{name}_{pid}.log"

    root = logging.getLogger()
    root.handlers.clear()

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric_level)

    shared_pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)
    root.addHandler(file_handler)

    if console:
        console_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_pre_chain,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.dev.ConsoleRenderer(colors=False),
            ],
        )

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(console_formatter)
        root.addHandler(stream_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Hand off to ProcessorFormatter to render per-handler (console vs file)
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    logger = structlog.get_logger("mock_nvr").bind(process=name, pid=pid)
    logger.info(
        "logging_configured",
        log_file=str(log_path),
        level=level.upper(),
        max_bytes=max_bytes,
        backups=backups,
        retention_days=retention_days,
        cleanup=cleanup,
    )
    return logger


def get_logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("mock_nvr")
