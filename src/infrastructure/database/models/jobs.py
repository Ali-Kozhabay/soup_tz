from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.database.models.base import Base


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint("fence >= 0", name="ck_jobs_fence"),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL AND lease_until IS NULL)",
            name="ck_jobs_lease",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled')) = (finished_at IS NOT NULL)",
            name="ck_jobs_finished",
        ),
        CheckConstraint("(status = 'failed') = (error IS NOT NULL)", name="ck_jobs_error"),
        Index("ix_jobs_created", "created_at", "id"),
        Index("ix_jobs_status_created", "status", "created_at", "id"),
        Index("ix_jobs_queued", "created_at", "id", postgresql_where=text("status = 'queued'")),
        Index("ix_jobs_expired", "lease_until", postgresql_where=text("status = 'running'")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    fence: Mapped[int] = mapped_column(BigInteger, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('running','succeeded','failed','cancelled','expired')",
            name="ck_attempt_outcome",
        ),
        CheckConstraint("(outcome = 'running') = (ended_at IS NULL)", name="ck_attempt_ended"),
    )

    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    fence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16), server_default="running")


class JobEffect(Base):
    __tablename__ = "job_effects"
    __table_args__ = (CheckConstraint("fence > 0", name="ck_effect_fence"),)

    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    fence: Mapped[int] = mapped_column(BigInteger)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
