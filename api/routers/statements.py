"""POST /v1/statements: upload a statement (idempotent on content hash)."""
from __future__ import annotations

import json
from pathlib import PurePath
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.deps import FactoryDep, SettingsDep
from api.errors import APIError
from api.schemas import UploadResponse
from db.ingest import JobOptions, ingest_document

router = APIRouter(prefix="/v1", tags=["statements"])

# Suffix → required leading bytes (None = text formats, no magic check).
ALLOWED_TYPES: dict[str, tuple[bytes, ...] | None] = {
    ".pdf": (b"%PDF",),
    ".csv": None,
    ".xlsx": (b"PK\x03\x04",),
    ".xlsm": (b"PK\x03\x04",),
    ".xls": (b"\xd0\xcf\x11\xe0",),
    ".png": (b"\x89PNG",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
}


def _parse_json_field(name: str, raw: str | None) -> Any:
    if raw is None or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise APIError(422, "invalid_request", f"{name} must be valid JSON") from exc


@router.post(
    "/statements",
    response_model=UploadResponse,
    status_code=202,
    responses={200: {"model": UploadResponse, "description": "Duplicate: already ingested"}},
)
def upload_statement(
    factory: FactoryDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    company_name: Annotated[str | None, Form(max_length=255)] = None,
    affiliates: Annotated[str | None, Form(description="JSON list of {name, relationship?, aliases?}")] = None,
    claims: Annotated[str | None, Form(description="JSON object of CompanyClaims fields")] = None,
) -> Any:
    filename = PurePath((file.filename or "").replace("\\", "/")).name
    suffix = PurePath(filename).suffix.lower()
    if suffix not in ALLOWED_TYPES:
        raise APIError(415, "unsupported_type", "Unsupported file type",
                       {"allowed": sorted(ALLOWED_TYPES)})

    limit = settings.max_upload_bytes
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise APIError(413, "too_large", "File exceeds the upload limit", {"max_bytes": limit})
    if not data:
        raise APIError(422, "empty_file", "Uploaded file is empty")
    magic = ALLOWED_TYPES[suffix]
    if magic is not None and not data.startswith(magic):
        raise APIError(415, "type_mismatch", "File content does not match its extension")

    try:
        options = JobOptions.model_validate({
            "company_name": company_name,
            "affiliates": _parse_json_field("affiliates", affiliates) or [],
            "claims": _parse_json_field("claims", claims),
        })
    except ValidationError as exc:
        details = [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in exc.errors()]
        raise APIError(422, "invalid_request", "Invalid upload options", details) from exc

    result = ingest_document(factory, data, filename, mime=file.content_type, options=options,
                             max_attempts=settings.job_max_attempts)
    body = UploadResponse(
        document_id=result.document_id, job_id=result.job_id,
        sha256=result.sha256, duplicate=result.duplicate,
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=200 if result.duplicate else 202)
