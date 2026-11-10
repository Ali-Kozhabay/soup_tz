from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response

from src.api.web.dependencies import Service
from src.api.web.jobs.schemas import JobPage, JobResponse
from src.core.dto.jobs import TrainingSpec
from src.core.enums.jobs import JobStatus

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.post(
    "",
    response_model=JobResponse,
    status_code=201,
    responses={
        200: {"description": "Idempotent replay"},
        409: {"description": "Key reused with different payload"},
    },
)
async def create_job(
    payload: TrainingSpec,
    response: Response,
    jobs: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200, pattern=r"^[!-~]+$")],
) -> JobResponse:
    job, created = await jobs.create(idempotency_key, payload)
    response.status_code = 201 if created else 200
    response.headers["Location"] = f"/v1/jobs/{job.id}"
    response.headers["Idempotency-Replayed"] = str(not created).lower()
    return JobResponse.model_validate(job)


@router.get("", response_model=JobPage)
async def list_jobs(
    jobs: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    status: JobStatus | None = None,
) -> JobPage:
    rows, next_cursor = await jobs.list(limit, cursor, status)
    return JobPage(items=[JobResponse.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: UUID, jobs: Service) -> JobResponse:
    return JobResponse.model_validate(await jobs.get(job_id))


@router.post(
    "/{job_id}/cancel",
    response_model=JobResponse,
    responses={409: {"description": "Job already succeeded or failed"}},
)
async def cancel_job(job_id: UUID, jobs: Service) -> JobResponse:
    return JobResponse.model_validate(await jobs.cancel(job_id))
