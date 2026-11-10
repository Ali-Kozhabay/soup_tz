from uuid import UUID

from src.core.dto.jobs import JobRecord, TrainingSpec
from src.core.interfaces.repositories.jobs import JobRepositoryProtocol
from src.core.interfaces.storage.notifications import Wakeup


class JobService:
    def __init__(self, repository: JobRepositoryProtocol, wakeup: Wakeup) -> None:
        self._repository = repository
        self._wakeup = wakeup

    async def create(self, key: str, spec: TrainingSpec) -> tuple[JobRecord, bool]:
        job, created = await self._repository.create(key, spec)
        if created:
            # The repository has committed. Notifications are only a latency optimization.
            await self._wakeup.publish()
        return job, created

    async def get(self, job_id: UUID) -> JobRecord:
        return await self._repository.get(job_id)

    async def list(
        self, limit: int, after: str | None, status: str | None
    ) -> tuple[list[JobRecord], str | None]:
        return await self._repository.list(limit, after, status)

    async def cancel(self, job_id: UUID) -> JobRecord:
        return await self._repository.cancel(job_id)
