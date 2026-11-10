from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.core.dto.jobs import TrainingSpec
from src.core.enums.jobs import JobStatus


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: JobStatus
    payload: TrainingSpec
    fence: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class JobPage(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None
