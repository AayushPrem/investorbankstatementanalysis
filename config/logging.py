"""Structured (JSON) logging, run/stage context, and redaction helpers.

* :func:`configure_logging` installs a JSON (or plain) handler on the root
  logger. Only entry points call it (``demo/app.py``, CLIs). Library code
  never touches the root logger.
* :func:`run_context` / :func:`stage_timer` set ``run_id`` and ``stage`` in
  :mod:`contextvars`. Every record emitted inside them carries both fields, and
  ``stage_timer`` logs the stage's ``duration_ms`` when it exits.
* :func:`redact` and :func:`safe_exc` keep PII out of logs (UPGRADE_GUIDE
  §1.9). Log IDs, counts, hashes and durations, never descriptions, names,
  amounts or balances. Exception *messages* can echo row content or LLM
  output, so failures are logged by exception type only.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_run_id: ContextVar[str | None] = ContextVar("bsaa_run_id", default=None)
_stage: ContextVar[str | None] = ContextVar("bsaa_stage", default=None)

# Attributes every LogRecord has. Anything else came from ``extra=``.
_STD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message", "asctime", "taskName",
}

_log = logging.getLogger("bsaa.pipeline")


# ── Redaction ────────────────────────────────────────────────────────────────

def redact(value: object) -> str:
    """Stable, non-reversible token for a sensitive value: ``h:<sha256[:10]>``.

    Use it when two log lines need to refer to the same thing (a counterparty,
    a filename) without revealing it.
    """
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]
    return f"h:{digest}"


def safe_exc(exc: BaseException) -> str:
    """Exception *type* only. Messages can contain row text, amounts or LLM output."""
    return type(exc).__name__


# ── Context ──────────────────────────────────────────────────────────────────

def current_run_id() -> str | None:
    return _run_id.get()


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@contextlib.contextmanager
def run_context(run_id: str | None = None) -> Iterator[str]:
    """Bind a run_id for the duration of the block (and any threads it awaits)."""
    rid = run_id or new_run_id()
    token = _run_id.set(rid)
    try:
        yield rid
    finally:
        _run_id.reset(token)


@contextlib.contextmanager
def stage_timer(stage: str, logger: logging.Logger | None = None, **counts: Any) -> Iterator[dict[str, Any]]:
    """Time a pipeline stage and log ``stage done`` with ``duration_ms``.

    The yielded dict can be filled with extra *counts* (ints, never values)
    that are added to the log line, e.g. ``ctx["txns"] = len(doc.transactions)``.
    On an exception it logs ``stage failed`` with the exception type, then re-raises.
    """
    lg = logger or _log
    token = _stage.set(stage)
    extra: dict[str, Any] = dict(counts)
    start = time.perf_counter()
    try:
        yield extra
    except BaseException as exc:
        lg.warning(
            "stage failed",
            extra={**extra, "duration_ms": _ms(start), "error": safe_exc(exc)},
        )
        raise
    else:
        lg.info("stage done", extra={**extra, "duration_ms": _ms(start)})
    finally:
        _stage.reset(token)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


# ── Formatting ───────────────────────────────────────────────────────────────

class ContextFilter(logging.Filter):
    """Copy run_id/stage from contextvars onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = _run_id.get()
        if not hasattr(record, "stage"):
            record.stage = _stage.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, run_id, stage, + extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": getattr(record, "run_id", None),
            "stage": getattr(record, "stage", None),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            # Type only. The traceback text can contain values.
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str, ensure_ascii=False)


_HANDLER_MARK = "_bsaa_handler"

# Third-party loggers that emit document content at DEBUG/INFO. pdfminer dumps
# the PDF token stream (transaction descriptions, names, amounts), and the HTTP
# clients log request details. They are pinned to WARNING whatever LOG_LEVEL
# says, so LOG_LEVEL=DEBUG can't leak statement content (UPGRADE_GUIDE §1.9).
QUIET_LOGGERS = ("pdfminer", "pdfplumber", "PIL", "httpx", "httpcore", "anthropic", "urllib3")


def configure_logging(level: str | int | None = None, json_logs: bool | None = None) -> None:
    """Install the BSAA handler on the root logger (idempotent).

    Defaults come from ``config.settings`` (``LOG_LEVEL``, ``LOG_JSON``).
    """
    if level is None or json_logs is None:
        from config.settings import get_settings

        s = get_settings()
        level = s.log_level if level is None else level
        json_logs = s.log_json if json_logs is None else json_logs

    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_MARK, False):
            root.removeHandler(h)
    handler = logging.StreamHandler()
    setattr(handler, _HANDLER_MARK, True)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        JsonFormatter() if json_logs
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(run_id)s/%(stage)s] %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
