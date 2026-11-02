"""API (§8.5): every endpoint, auth, upload limits, duplicate semantics, error shape."""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.main import create_app
from config.settings import Settings
from tests.platform.conftest import API_KEY, csv_statement, make_settings
from worker.runner import Worker


def _upload(client: TestClient, headers: dict[str, str], name: str, data: bytes,
            **form: str) -> Any:
    return client.post("/v1/statements", files={"file": (name, data, "application/octet-stream")},
                       data=form, headers=headers)


def _error(resp: Any, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    return body["error"]


@pytest.fixture
def processed(client: TestClient, headers: dict[str, str], factory: sessionmaker[Session],
              settings: Settings, gold_pdf: tuple[str, bytes]) -> dict[str, Any]:
    name, data = gold_pdf
    up = _upload(client, headers, name, data, company_name="Acme")
    assert up.status_code == 202
    Worker(factory, settings=settings).run_once()
    job = client.get(f"/v1/jobs/{up.json()['job_id']}", headers=headers).json()
    assert job["status"] == "succeeded"
    return {"upload": up.json(), "job": job, "run_id": job["run_id"]}


# ── health ───────────────────────────────────────────────────────────────────

def test_health_and_ready_need_no_auth(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_ready_reports_503_when_db_is_down(settings: Settings) -> None:
    class Broken:
        def __call__(self) -> Any:
            raise RuntimeError("db down")

    c = TestClient(create_app(settings=settings, session_factory=Broken()))  # type: ignore[arg-type]
    _error(c.get("/ready"), 503, "not_ready")


def test_openapi_lists_every_route(client: TestClient) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {
        "/v1/statements", "/v1/jobs/{job_id}", "/v1/runs/{run_id}/transactions",
        "/v1/runs/{run_id}/categorisation", "/v1/transactions/{txn_id}/category",
        "/v1/runs/{run_id}/dq", "/v1/runs/{run_id}/flags", "/v1/runs/{run_id}/summary",
        "/v1/runs/{run_id}/report", "/v1/runs/{run_id}/executions", "/health", "/ready",
    } <= paths


# ── auth ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hdrs", [{}, {"X-API-Key": "wrong"}])
def test_v1_requires_valid_key(client: TestClient, hdrs: dict[str, str]) -> None:
    _error(client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers=hdrs),
           401, "unauthorized")
    _error(_upload(client, hdrs, "a.csv", csv_statement(5)), 401, "unauthorized")


def test_auth_fails_closed_without_configured_hash(factory: sessionmaker[Session]) -> None:
    c = TestClient(create_app(settings=make_settings(api_key_hash=None), session_factory=factory))
    _error(c.get("/v1/jobs/00000000-0000-0000-0000-000000000000",
                 headers={"X-API-Key": API_KEY}), 503, "auth_not_configured")


# ── upload ───────────────────────────────────────────────────────────────────

def test_upload_202_then_duplicate_200(client: TestClient, headers: dict[str, str]) -> None:
    data = csv_statement(10)
    first = _upload(client, headers, "a.csv", data)
    assert first.status_code == 202 and first.json()["duplicate"] is False
    again = _upload(client, headers, "other-name.csv", data)
    assert again.status_code == 200
    assert again.json() == {**first.json(), "duplicate": True}


def test_upload_rejects_unsupported_type(client: TestClient, headers: dict[str, str]) -> None:
    _error(_upload(client, headers, "evil.exe", b"MZ..."), 415, "unsupported_type")


def test_upload_rejects_content_type_mismatch(client: TestClient, headers: dict[str, str]) -> None:
    _error(_upload(client, headers, "fake.pdf", b"not a pdf"), 415, "type_mismatch")


def test_upload_rejects_empty(client: TestClient, headers: dict[str, str]) -> None:
    _error(_upload(client, headers, "a.csv", b""), 422, "empty_file")


def test_upload_enforces_size_limit(factory: sessionmaker[Session], headers: dict[str, str]) -> None:
    c = TestClient(create_app(settings=make_settings(max_upload_bytes=1000), session_factory=factory))
    # Small enough to pass the Content-Length guard, too big for the route check.
    _error(_upload(c, headers, "a.csv", b"x" * 1500), 413, "too_large")
    # Far over the limit: rejected by the Content-Length guard before parsing.
    _error(_upload(c, headers, "a.csv", b"x" * 200_000), 413, "too_large")


def test_upload_validates_options(client: TestClient, headers: dict[str, str]) -> None:
    _error(_upload(client, headers, "a.csv", csv_statement(5), affiliates="{not json"),
           422, "invalid_request")
    err = _error(_upload(client, headers, "a.csv", csv_statement(5),
                         claims=json.dumps({"declared_customer_count": "many"})),
                 422, "invalid_request")
    assert "input" not in json.dumps(err["details"])  # submitted values are never echoed
    _error(_upload(client, headers, "a.csv", csv_statement(5),
                   affiliates=json.dumps([{"name": "X", "aliases": ["a" * 256]}])),
           422, "invalid_request")


def test_upload_path_traversal_filename_is_reduced_to_basename(
    client: TestClient, headers: dict[str, str], factory: sessionmaker[Session]
) -> None:
    from db.models import Document

    r = _upload(client, headers, "../../etc/x.csv", csv_statement(5))
    with factory() as s:
        doc = s.get(Document, uuid.UUID(r.json()["document_id"]))
        assert doc is not None and doc.filename == "x.csv"


# ── jobs ─────────────────────────────────────────────────────────────────────

def test_job_status_and_404(client: TestClient, headers: dict[str, str]) -> None:
    up = _upload(client, headers, "a.csv", csv_statement(10)).json()
    job = client.get(f"/v1/jobs/{up['job_id']}", headers=headers).json()
    assert job["status"] == "queued" and job["attempts"] == 0 and job["run_id"] is None
    _error(client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers=headers),
           404, "not_found")
    _error(client.get("/v1/jobs/not-a-uuid", headers=headers), 422, "invalid_request")


# ── runs ─────────────────────────────────────────────────────────────────────

def test_transactions_paginated_and_filtered(client: TestClient, headers: dict[str, str],
                                             processed: dict[str, Any]) -> None:
    rid = processed["run_id"]
    page = client.get(f"/v1/runs/{rid}/transactions?limit=5&offset=0", headers=headers).json()
    assert page["limit"] == 5 and len(page["items"]) == 5 and page["total"] > 5
    assert [t["txn_seq"] for t in page["items"]] == [0, 1, 2, 3, 4]
    nxt = client.get(f"/v1/runs/{rid}/transactions?limit=5&offset=5", headers=headers).json()
    assert nxt["items"][0]["txn_seq"] == 5
    cat = page["items"][0]["category"]
    filtered = client.get(f"/v1/runs/{rid}/transactions?category={cat}&limit=500",
                          headers=headers).json()
    assert filtered["total"] >= 1 and all(t["category"] == cat for t in filtered["items"])
    d = page["items"][0]["txn_date"]
    by_date = client.get(f"/v1/runs/{rid}/transactions?date_from={d}&date_to={d}&limit=500",
                         headers=headers).json()
    assert all(t["txn_date"] == d for t in by_date["items"])
    # Phase 4: v3 routing splits rows across sources (rule + ml with a model,
    # all rule on the v2 fallback). The per-source filter must partition the run.
    totals = {src: client.get(f"/v1/runs/{rid}/transactions?source={src}&limit=1",
                              headers=headers).json()["total"]
              for src in ("rule", "ml", "llm", "review", "default")}
    assert sum(totals.values()) == page["total"] and totals["rule"] >= 1
    # money is exact (Decimal → string), never a float
    assert isinstance(page["items"][0]["balance"], str)
    _error(client.get(f"/v1/runs/{rid}/transactions?limit=0", headers=headers), 422,
           "invalid_request")


def test_categorisation_summary(client: TestClient, headers: dict[str, str],
                                processed: dict[str, Any]) -> None:
    body = client.get(f"/v1/runs/{processed['run_id']}/categorisation", headers=headers).json()
    assert sum(body["by_source"].values()) == body["total"] == sum(body["by_category"].values())
    assert all(t["category_source"] in ("default", "llm") for t in body["review_queue"])


def test_manual_override_keeps_history(client: TestClient, headers: dict[str, str],
                                       processed: dict[str, Any],
                                       factory: sessionmaker[Session]) -> None:
    from sqlalchemy import select

    from db.models import Categorisation

    rid = processed["run_id"]
    txn = client.get(f"/v1/runs/{rid}/transactions?limit=1", headers=headers).json()["items"][0]
    original_source = txn["category_source"]   # Phase 4: rule or ml, depending on routing
    r = client.patch(f"/v1/transactions/{txn['id']}/category", json={"category": "TAX"},
                     headers=headers)
    assert r.status_code == 200
    assert (r.json()["category"], r.json()["category_source"]) == ("TAX", "manual")
    again = client.get(f"/v1/runs/{rid}/transactions?limit=1", headers=headers).json()["items"][0]
    assert (again["category"], again["category_source"]) == ("TAX", "manual")
    with factory() as s:
        rows = list(s.scalars(select(Categorisation).where(
            Categorisation.transaction_id == uuid.UUID(txn["id"]))))
    assert sorted((c.source, c.is_current) for c in rows) == sorted(
        [("manual", True), (original_source, False)])
    _error(client.patch(f"/v1/transactions/{txn['id']}/category", json={"category": "BRIBES"},
                        headers=headers), 422, "invalid_request")
    _error(client.patch("/v1/transactions/00000000-0000-0000-0000-000000000000/category",
                        json={"category": "TAX"}, headers=headers), 404, "not_found")


def test_dq_flags_summary_executions(client: TestClient, headers: dict[str, str],
                                     processed: dict[str, Any]) -> None:
    rid = processed["run_id"]
    dq = client.get(f"/v1/runs/{rid}/dq", headers=headers).json()
    assert dq["passed"] is True and len(dq["checks"]) == 9
    flags = client.get(f"/v1/runs/{rid}/flags", headers=headers).json()
    assert isinstance(flags, list)
    # Phase 5: kind=ml flags come from the ml_anomaly stage (only with a model).
    for f in flags:
        assert f["kind"] in ("rule", "ml") and f["severity"] in ("LOW", "MEDIUM", "HIGH")
        if f["kind"] == "ml":
            assert f["detector"] == "ml_anomaly" and f["score"] is not None
    if flags:
        sev = flags[0]["severity"]
        only = client.get(f"/v1/runs/{rid}/flags?severity={sev.lower()}", headers=headers).json()
        assert only and all(f["severity"] == sev for f in only)
    summary = client.get(f"/v1/runs/{rid}/summary", headers=headers).json()
    assert summary["status"] == "succeeded"
    assert {"metrics", "customer_analytics", "narrative"} <= set(summary["outputs"])
    assert summary["outputs"]["narrative"]["company_name"] == "Acme"
    execs = client.get(f"/v1/runs/{rid}/executions", headers=headers).json()
    ml_stage = any(e["stage"] == "ml_anomaly" for e in execs)
    assert len(execs) == 14 + ml_stage and all(e["status"] == "succeeded" for e in execs)
    if not ml_stage:
        assert all(f["kind"] == "rule" for f in flags)


@pytest.mark.parametrize("lens,fmt,magic", [
    ("angel", None, b"%PDF"), ("vc", "xlsx", b"PK"), ("vc", "pdf", b"%PDF"),
    ("workbench", None, b"PK"), ("workbench", "pdf", b"%PDF"),
])
def test_report_download(client: TestClient, headers: dict[str, str], processed: dict[str, Any],
                         lens: str, fmt: str | None, magic: bytes) -> None:
    q = f"lens={lens}" + (f"&format={fmt}" if fmt else "")
    r = client.get(f"/v1/runs/{processed['run_id']}/report?{q}", headers=headers)
    assert r.status_code == 200 and r.content.startswith(magic)
    assert "attachment" in r.headers["content-disposition"]


def test_report_errors(client: TestClient, headers: dict[str, str],
                       processed: dict[str, Any]) -> None:
    rid = processed["run_id"]
    _error(client.get(f"/v1/runs/{rid}/report?lens=angel&format=xlsx", headers=headers),
           404, "not_found")
    _error(client.get(f"/v1/runs/{rid}/report?lens=nope", headers=headers), 422,
           "invalid_request")


@pytest.mark.parametrize("path", ["transactions", "categorisation", "dq", "flags", "summary",
                                  "report?lens=angel", "executions"])
def test_unknown_run_404(client: TestClient, headers: dict[str, str], path: str) -> None:
    _error(client.get(f"/v1/runs/nope/{path}", headers=headers), 404, "not_found")


def test_failed_validation_run_exposes_dq_but_not_summary(
    client: TestClient, headers: dict[str, str], factory: sessionmaker[Session], settings: Settings
) -> None:
    up = _upload(client, headers, "broken.csv", csv_statement(30, break_at=10)).json()
    Worker(factory, settings=settings, render=None).run_once()
    job = client.get(f"/v1/jobs/{up['job_id']}", headers=headers).json()
    assert job["status"] == "failed" and job["last_error"] == "ValidationFailed: BALANCE_BREAK"
    dq = client.get(f"/v1/runs/{job['run_id']}/dq", headers=headers).json()
    rb = next(c for c in dq["checks"] if c["name"] == "running_balance")
    assert dq["passed"] is False and rb["count"] == 2
    txns = client.get(f"/v1/runs/{job['run_id']}/transactions?limit=500", headers=headers).json()
    by_id = {t["source_txn_id"]: t["txn_seq"] for t in txns["items"]}
    assert [by_id[i] for i in rb["sample_ids"]] == [10, 11]
    assert client.get(f"/v1/runs/{job['run_id']}/summary", headers=headers).json()["outputs"] == {}
