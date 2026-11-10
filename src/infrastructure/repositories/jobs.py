from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings
from src.core.dto.jobs import Claim, JobRecord, TrainingSpec
from src.core.exceptions import (
    IdempotencyConflict,
    JobNotFound,
    TerminalConflict,
)
from src.infrastructure.database.models import Job, JobAttempt, JobEffect
from src.infrastructure.repositories.cursors import Cursor


class JobRepository:
    """Queue operations are transaction boundaries, each with its own async session.

    The job is the aggregate lock: all attempt/effect writes first lock its row.
    Keeping this protocol together is more useful than a generic CRUD repository.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions = sessions
        self.settings = settings

    async def create(self, key: str, spec: TrainingSpec) -> tuple[JobRecord, bool]:
        async with self.sessions.begin() as session:
            statement = (
                insert(Job)
                .values(
                    id=uuid4(),
                    idempotency_key=key,
                    payload_hash=spec.fingerprint(),
                    payload=spec.model_dump(),
                )
                .on_conflict_do_nothing(index_elements=[Job.idempotency_key])
                .returning(Job)
            )
            job = (await session.scalars(statement)).one_or_none()
            created = job is not None
            if job is None:
                # READ COMMITTED: this statement sees the concurrent winner's commit.
                job = (await session.scalars(select(Job).where(Job.idempotency_key == key))).one()
                if job.payload_hash != spec.fingerprint():
                    raise IdempotencyConflict("Idempotency-Key was used with a different payload")
        return self._record(job), created

    async def get(self, job_id: UUID) -> JobRecord:
        async with self.sessions() as session:
            job = await session.get(Job, job_id)
            if job is None:
                raise JobNotFound(str(job_id))
            return self._record(job)

    async def list(
        self, limit: int, after: str | None, status: str | None
    ) -> tuple[list[JobRecord], str | None]:
        cursor = Cursor.decode(after, status) if after else None
        async with self.sessions() as session:
            ceiling = cursor.ceiling if cursor else await self._now(session)
            statement = select(Job).where(Job.created_at <= ceiling)
            if status:
                statement = statement.where(Job.status == status)
            if cursor:
                statement = statement.where(
                    tuple_(Job.created_at, Job.id) > (cursor.timestamp, cursor.job_id)
                )
            jobs = list(
                (
                    await session.scalars(
                        statement.order_by(Job.created_at, Job.id).limit(limit + 1)
                    )
                ).all()
            )
            next_cursor = None
            if len(jobs) > limit:
                jobs = jobs[:limit]
                next_cursor = Cursor(
                    timestamp=jobs[-1].created_at,
                    job_id=jobs[-1].id,
                    ceiling=ceiling,
                    status=status,
                ).encode()
            return [self._record(job) for job in jobs], next_cursor

    async def cancel(self, job_id: UUID) -> JobRecord:
        async with self.sessions.begin() as session:
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise JobNotFound(str(job_id))
            if job.status == "cancelled":
                return self._record(job)
            if job.status in {"succeeded", "failed"}:
                raise TerminalConflict(job.status)
            now = await self._now(session)
            if job.status == "running":
                await self._end_attempt(session, job, "cancelled", now)
            self._finish(job, "cancelled", now)
        return self._record(job)

    @staticmethod
    def _record(job: Job) -> JobRecord:
        return JobRecord(
            id=job.id,
            idempotency_key=job.idempotency_key,
            payload_hash=job.payload_hash,
            payload=job.payload,
            status=job.status,
            fence=job.fence,
            lease_owner=job.lease_owner,
            lease_until=job.lease_until,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            error=job.error,
        )

    async def claim(self, owner: str) -> Claim | None:
        async with self.sessions.begin() as session:
            statement = (
                select(Job)
                .where(
                    or_(
                        Job.status == "queued",
                        and_(Job.status == "running", Job.lease_until <= func.clock_timestamp()),
                    )
                )
                .order_by(Job.created_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = (await session.scalars(statement)).one_or_none()
            if job is None:
                return None
            now = await self._now(session)
            if job.status == "running":
                await self._end_attempt(session, job, "expired", now)
            if job.fence >= self.settings.max_attempts:
                self._finish(job, "failed", now, "attempt_limit_exceeded")
                return None
            job.status = "running"
            job.fence += 1
            job.lease_owner = owner
            job.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
            job.started_at = job.started_at or now
            session.add(JobAttempt(job_id=job.id, fence=job.fence, owner=owner, claimed_at=now))
            claim = Claim(job.id, owner, job.fence, TrainingSpec.model_validate(job.payload))
        return claim

    async def heartbeat(self, claim: Claim) -> bool:
        async with self.sessions.begin() as session:
            job = await session.get(Job, claim.job_id, with_for_update=True)
            now = await self._now(session)
            if not self._owns(job, claim, now):
                return False
            assert job is not None
            job.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
        return True

    async def complete(self, claim: Claim, *, error: str | None = None) -> bool:
        async with self.sessions.begin() as session:
            job = await session.get(Job, claim.job_id, with_for_update=True)
            now = await self._now(session)
            if not self._owns(job, claim, now):
                return False
            assert job is not None
            outcome = "failed" if error else "succeeded"
            if error is None:
                session.add(
                    JobEffect(
                        job_id=job.id, fence=claim.fence, result=claim.spec.result(), created_at=now
                    )
                )
            await self._end_attempt(session, job, outcome, now)
            self._finish(job, outcome, now, error)
        return True

    @staticmethod
    async def _now(session: AsyncSession) -> datetime:
        return (await session.execute(select(func.clock_timestamp()))).scalar_one()  # type: ignore[no-any-return]

    @staticmethod
    def _owns(job: Job | None, claim: Claim, now: datetime) -> bool:
        return bool(
            job is not None
            and job.status == "running"
            and job.fence == claim.fence
            and job.lease_owner == claim.owner
            and job.lease_until is not None
            and job.lease_until > now
        )

    @staticmethod
    async def _end_attempt(session: AsyncSession, job: Job, outcome: str, now: datetime) -> None:
        await session.execute(
            update(JobAttempt)
            .where(JobAttempt.job_id == job.id, JobAttempt.fence == job.fence)
            .values(outcome=outcome, ended_at=now)
        )

    @staticmethod
    def _finish(job: Job, status: str, now: datetime, error: str | None = None) -> None:
        job.status = status
        job.finished_at = now
        job.error = error
        job.lease_owner = None
        job.lease_until = None
