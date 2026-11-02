"""GET /v1/jobs/{id}."""
from __future__ import annotations

import uuid

from fastapi import APIRouter

from api.deps import SessionDep
from api.errors import not_found
from api.schemas import JobResponse
from db.models import Job

router = APIRouter(prefix="/v1", tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: uuid.UUID, session: SessionDep) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise not_found("job", job_id)
    return job
