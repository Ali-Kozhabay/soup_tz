import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, text, update

from src.core.dto.jobs import TrainingSpec
from src.core.exceptions import IdempotencyConflict, TerminalConflict
from src.infrastructure.database.models import Job, JobAttempt, JobEffect
from src.infrastructure.repositories.jobs import JobRepository

pytestmark = pytest.mark.integration


async def expire(repository, job_id):
    async with repository.sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )


async def effect_count(repository):
    async with repository.sessions() as session:
        return await session.scalar(select(func.count()).select_from(JobEffect))


async def test_concurrent_idempotency_creates_one_job(repository):
    results = await asyncio.gather(
        *(repository.create("same-key", TrainingSpec()) for _ in range(32))
    )
    assert len({job.id for job, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    with pytest.raises(IdempotencyConflict):
        await repository.create("same-key", TrainingSpec(seed=7))


async def test_simultaneous_claims_never_share_a_job(repository):
    job, _ = await repository.create("single", TrainingSpec())
    claims = await asyncio.gather(*(repository.claim(f"worker-{i}") for i in range(32)))
    winners = [claim for claim in claims if claim]
    assert len(winners) == 1
    assert winners[0].job_id == job.id
    assert winners[0].fence == 1


async def test_skip_locked_does_not_wait_for_busy_job(repository):
    first, _ = await repository.create("first", TrainingSpec())
    second, _ = await repository.create("second", TrainingSpec())
    async with repository.sessions.begin() as session:
        await session.get(Job, first.id, with_for_update=True)
        async with asyncio.timeout(1):
            claim = await repository.claim("other")
        assert claim.job_id == second.id


async def test_expired_zombie_cannot_renew_complete_or_fail(repository):
    job, _ = await repository.create("zombie", TrainingSpec())
    stale = await repository.claim("old")
    await expire(repository, job.id)
    assert not await repository.heartbeat(stale)
    assert not await repository.complete(stale)
    fresh = await repository.claim("new")
    assert fresh.fence == stale.fence + 1
    assert not await repository.complete(stale, error="stale failure")
    assert await repository.complete(fresh)
    assert not await repository.complete(stale)
    assert await effect_count(repository) == 1
    async with repository.sessions() as session:
        attempts = (await session.scalars(select(JobAttempt).order_by(JobAttempt.fence))).all()
        assert [attempt.outcome for attempt in attempts] == ["expired", "succeeded"]


async def test_owner_and_fence_are_both_checked(repository):
    await repository.create("fencing", TrainingSpec())
    claim = await repository.claim("real-owner")
    assert not await repository.complete(replace(claim, owner="impostor"))
    assert not await repository.complete(replace(claim, fence=claim.fence + 1))
    assert await repository.complete(claim)


async def test_heartbeat_extends_lease_and_prevents_reclaim(repository):
    job, _ = await repository.create("heartbeat", TrainingSpec())
    claim = await repository.claim("active")
    before = (await repository.get(job.id)).lease_until
    await asyncio.sleep(0.02)
    assert await repository.heartbeat(claim)
    assert (await repository.get(job.id)).lease_until > before
    assert await repository.claim("competitor") is None


async def test_completion_after_lease_expires_while_waiting_for_lock(repository):
    job, _ = await repository.create("waiter", TrainingSpec())
    claim = await repository.claim("waiting")
    async with repository.sessions.begin() as session:
        await session.get(Job, job.id, with_for_update=True)
        pending = asyncio.create_task(repository.complete(claim))
        await asyncio.sleep(0.05)
        await session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )
    assert not await pending
    assert await effect_count(repository) == 0


@pytest.mark.parametrize("running", [False, True])
async def test_cancellation_revokes_effect_permission(repository, running):
    job, _ = await repository.create("cancel", TrainingSpec())
    claim = await repository.claim("worker") if running else None
    assert (await repository.cancel(job.id)).status == "cancelled"
    assert (await repository.cancel(job.id)).status == "cancelled"
    if claim:
        assert not await repository.heartbeat(claim)
        assert not await repository.complete(claim)
    assert await repository.claim("next") is None
    assert await effect_count(repository) == 0


async def test_cancel_complete_race_is_linearizable(repository):
    for i in range(40):
        job, _ = await repository.create(f"race-{i}", TrainingSpec())
        claim = await repository.claim("racer")
        operations = [repository.cancel(job.id), repository.complete(claim)]
        if i % 2:
            operations.reverse()
        results = await asyncio.gather(*operations, return_exceptions=True)
        assert all(
            not isinstance(value, Exception) or isinstance(value, TerminalConflict)
            for value in results
        )
        final = await repository.get(job.id)
        async with repository.sessions() as session:
            effect = await session.get(JobEffect, job.id)
        assert final.status in {"cancelled", "succeeded"}
        assert (effect is not None) == (final.status == "succeeded")


async def test_effect_and_success_roll_back_together(repository):
    job, _ = await repository.create("rollback", TrainingSpec())
    claim = await repository.claim("worker")
    engine = repository.sessions.kw["bind"].sync_engine

    def fail_after_effect_insert(conn, cursor, statement, params, context, many):
        if statement.startswith("UPDATE job_attempts"):
            raise RuntimeError("injected crash between effect insert and terminal state")

    event.listen(engine, "after_cursor_execute", fail_after_effect_insert)
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            await repository.complete(claim)
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_effect_insert)
    assert (await repository.get(job.id)).status == "running"
    assert await effect_count(repository) == 0
    assert await repository.complete(claim)
    assert await effect_count(repository) == 1


async def test_lost_completion_receipt_cannot_duplicate_effect(repository):
    job, _ = await repository.create("lost-receipt", TrainingSpec())
    claim = await repository.claim("worker")
    assert await repository.complete(claim)
    assert not await repository.complete(claim)
    replay, created = await repository.create("lost-receipt", TrainingSpec())
    assert replay.id == job.id and not created
    assert replay.status == "succeeded"
    assert await effect_count(repository) == 1


async def test_expired_attempt_budget_fails_without_effect(repository, settings):
    limited = JobRepository(repository.sessions, settings.model_copy(update={"max_attempts": 2}))
    job, _ = await limited.create("limit", TrainingSpec())
    for _ in range(2):
        assert await limited.claim("crashing")
        await expire(limited, job.id)
    assert await limited.claim("reaper") is None
    final = await limited.get(job.id)
    assert final.status == "failed" and final.error == "attempt_limit_exceeded"
    assert final.lease_owner is None and final.finished_at
    assert await effect_count(repository) == 0


@pytest.mark.parametrize("offset_days", [-365, 365])
async def test_worker_wall_clock_skew_cannot_change_lease(repository, monkeypatch, offset_days):
    import src.infrastructure.repositories.jobs as module

    class SkewedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(days=offset_days)

    monkeypatch.setattr(module, "datetime", SkewedClock)
    job, _ = await repository.create("clock", TrainingSpec())
    claim = await repository.claim("skewed-worker")
    async with repository.sessions() as session:
        db_now = await session.scalar(select(func.clock_timestamp()))
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
    deadline = (await repository.get(job.id)).lease_until
    assert 0 < (deadline - db_now).total_seconds() <= repository.settings.lease_seconds
    assert await repository.complete(claim)


async def test_simulated_failure_has_no_success_effect(repository):
    job, _ = await repository.create("fail", TrainingSpec(simulate_failure=True))
    claim = await repository.claim("worker")
    assert await repository.complete(claim, error="simulated_training_failure")
    assert (await repository.get(job.id)).status == "failed"
    assert await effect_count(repository) == 0
    with pytest.raises(TerminalConflict):
        await repository.cancel(job.id)


async def test_real_frozen_worker_returns_as_fenced_zombie(repository, settings, tmp_path):
    job, _ = await repository.create("frozen-process", TrainingSpec(duration_ms=5000))
    with (tmp_path / "worker.log").open("w") as output:
        worker = subprocess.Popen(
            [sys.executable, "-m", "src.worker"],
            stdout=output,
            stderr=subprocess.STDOUT,
            env={
                **os.environ,
                "SOUP_DATABASE_URL": settings.database_url,
                "SOUP_REDIS_URL": settings.redis_url,
                "SOUP_LEASE_SECONDS": "2",
                "SOUP_HEARTBEAT_SECONDS": "0.2",
            },
        )
        try:
            async with asyncio.timeout(10):
                while (await repository.get(job.id)).status != "running":
                    await asyncio.sleep(0.02)
            os.kill(worker.pid, signal.SIGSTOP)
            await asyncio.sleep(2.1)
            fresh = await repository.claim("replacement")
            assert fresh and fresh.fence == 2
            os.kill(worker.pid, signal.SIGCONT)
            await asyncio.sleep(0.5)
            assert await effect_count(repository) == 0
            assert await repository.complete(fresh)
            assert await effect_count(repository) == 1
        finally:
            if worker.poll() is None:
                os.kill(worker.pid, signal.SIGCONT)
                worker.terminate()
            await asyncio.to_thread(worker.wait, 5)
