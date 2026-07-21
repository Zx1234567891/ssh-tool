from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
import queue
import re
import threading
import time
import uuid

from .models import AppSettings


SESSION_ID = str(uuid.uuid4())
_listener: QueueListener | None = None
_lock = threading.Lock()
logging.getLogger("ssh_tunnel_manager").addHandler(logging.NullHandler())


_SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:access_?token|api_?key|password|secret|cookie)\s*[:=]\s*)[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(https?://[^\s/:]+:)[^@\s]+@"), r"\1<redacted>@"),
]


def redact(value: object) -> str:
    text = str(value)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        event_data = getattr(record, "event_data", None)
        if isinstance(event_data, dict):
            details = " ".join(
                f"{key}={value}" for key, value in event_data.items()
                if key != "event" and value not in (None, "")
            )
            if details:
                rendered += " " + details
        return redact(rendered)


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
            "session_id": SESSION_ID,
        }
        event_data = getattr(record, "event_data", None)
        if isinstance(event_data, dict):
            for key, value in event_data.items():
                if key.lower() in {"password", "secret", "token", "cookie", "authorization"}:
                    payload[key] = "<redacted>"
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    payload[key] = redact(value) if isinstance(value, str) else value
                else:
                    payload[key] = redact(value)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(log_dir: Path, settings: AppSettings) -> QueueListener:
    global _listener
    with _lock:
        if _listener is not None:
            return _listener
        log_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - max(1, int(settings.log_retention_days)) * 86400
        for pattern in ("app.log.*", "events.jsonl.*"):
            for old_log in log_dir.glob(pattern):
                try:
                    if old_log.stat().st_mtime < cutoff:
                        old_log.unlink()
                except OSError:
                    pass
        level = getattr(logging, settings.log_level.upper(), logging.INFO)
        text_handler = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=max(256 * 1024, int(settings.log_max_bytes)),
            backupCount=max(1, int(settings.log_backup_count)),
            encoding="utf-8",
        )
        text_handler.setFormatter(RedactingFormatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        event_handler = RotatingFileHandler(
            log_dir / "events.jsonl",
            maxBytes=max(256 * 1024, int(settings.log_max_bytes)),
            backupCount=max(1, int(settings.log_backup_count)),
            encoding="utf-8",
        )
        event_handler.setFormatter(JsonLineFormatter())
        records: queue.Queue = queue.Queue(-1)
        root = logging.getLogger("ssh_tunnel_manager")
        root.handlers.clear()
        root.setLevel(level)
        root.propagate = False
        root.addHandler(QueueHandler(records))
        _listener = QueueListener(records, text_handler, event_handler, respect_handler_level=True)
        _listener.start()
        log_event(
            root, logging.INFO, "app.logging_started",
            log_dir=str(log_dir), configured_level=settings.log_level,
        )
        return _listener


def shutdown_logging() -> None:
    global _listener
    with _lock:
        listener = _listener
        _listener = None
    if listener is not None:
        listener.stop()
        for handler in listener.handlers:
            handler.close()
    root = logging.getLogger("ssh_tunnel_manager")
    root.handlers.clear()
    root.addHandler(logging.NullHandler())


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    logger.log(level, event, extra={"event_data": {"event": event, **fields}})
