"""config/logging.py: JSON formatter fields, run/stage context, redaction."""
from __future__ import annotations

import json
import logging

import pytest

from config.logging import (
    QUIET_LOGGERS,
    ContextFilter,
    JsonFormatter,
    configure_logging,
    current_run_id,
    redact,
    run_context,
    safe_exc,
    stage_timer,
)


def _format(record: logging.LogRecord) -> dict[str, object]:
    ContextFilter().filter(record)
    out: dict[str, object] = json.loads(JsonFormatter().format(record))
    return out


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("bsaa.test", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_has_required_fields() -> None:
    payload = _format(_record(txns=12))
    for key in ("ts", "level", "logger", "msg", "run_id", "stage"):
        assert key in payload
    assert payload["msg"] == "hello"
    assert payload["txns"] == 12


def test_run_and_stage_context_propagate() -> None:
    with run_context("run123") as rid:
        assert rid == "run123" == current_run_id()
        with stage_timer("risk"):
            payload = _format(_record())
    assert payload["run_id"] == "run123"
    assert payload["stage"] == "risk"
    assert current_run_id() is None


def test_stage_timer_logs_duration(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="bsaa.pipeline"):
        with stage_timer("normalise") as ctx:
            ctx["txns"] = 5
    rec = next(r for r in caplog.records if r.getMessage() == "stage done")
    assert rec.txns == 5  # type: ignore[attr-defined]
    assert isinstance(rec.duration_ms, float)  # type: ignore[attr-defined]


def test_stage_timer_failure_logs_type_only(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="bsaa.pipeline"):
        with pytest.raises(ValueError):
            with stage_timer("ingest"):
                raise ValueError("ACME PVT LTD paid 123456")
    rec = next(r for r in caplog.records if r.getMessage() == "stage failed")
    assert rec.error == "ValueError"  # type: ignore[attr-defined]
    assert "ACME" not in str(rec.__dict__)


def test_exc_info_reports_type_not_traceback() -> None:
    try:
        raise KeyError("secret-counterparty")
    except KeyError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    payload = _format(rec)
    assert payload["exc_type"] == "KeyError"
    assert "secret-counterparty" not in json.dumps(payload)


def test_redact_is_stable_and_opaque() -> None:
    a = redact("Acme Pvt Ltd")
    assert a == redact("Acme Pvt Ltd")
    assert a != redact("Acme Pvt Ltd.")
    assert a.startswith("h:") and len(a) == 12
    assert "Acme" not in a


def test_safe_exc_is_type_name() -> None:
    assert safe_exc(RuntimeError("₹5,00,000 to FOUNDER")) == "RuntimeError"


def test_configure_logging_idempotent() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    old_level = root.level
    try:
        configure_logging("INFO", json_logs=True)
        configure_logging("INFO", json_logs=True)
        ours = [h for h in root.handlers if getattr(h, "_bsaa_handler", False)]
        assert len(ours) == 1
        assert isinstance(ours[0].formatter, JsonFormatter)
        configure_logging("DEBUG", json_logs=False)
        assert logging.getLogger("pdfminer").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("pdfminer.psparser").getEffectiveLevel() == logging.WARNING
    finally:
        for name in QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        root.setLevel(old_level)
