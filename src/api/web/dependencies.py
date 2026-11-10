from typing import Annotated

from fastapi import Depends, Request

from src.application.services.jobs import JobService


def job_service(request: Request) -> JobService:
    service: JobService = request.app.state.container.jobs
    return service


Service = Annotated[JobService, Depends(job_service)]
