import asyncio
import logging
import os
import signal
from contextlib import suppress
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from src.config import Settings
from src.core.dto.jobs import Claim
from src.core.interfaces.repositories.jobs import JobRepositoryProtocol
from src.core.logging import configure_logging
from src.infrastructure.adapters.storage.redis import RedisWakeup
from src.infrastructure.database.session import make_engine, make_sessions
from src.infrastructure.repositories.jobs import JobRepository

logger = logging.getLogger(__name__)


async def execute(claim: Claim) -> str | None:
    """Pure simulated computation. The only durable effect happens in complete()."""
    await asyncio.sleep(claim.spec.duration_ms / 1000)
    return "simulated_training_failure" if claim.spec.simulate_failure else None


async def renew(repository: JobRepositoryProtocol, claim: Claim, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        if not await repository.heartbeat(claim):
            return


async def process(repository: JobRepositoryProtocol, claim: Claim, interval: float) -> None:
    computation = asyncio.create_task(execute(claim))
    heartbeat = asyncio.create_task(renew(repository, claim, interval))
    try:
        done, _ = await asyncio.wait({computation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            heartbeat.result()  # Propagate DB failure; never continue on an uncertain lease.
            logger.info("lease_lost job=%s fence=%s", claim.job_id, claim.fence)
            return
        error = computation.result()
        accepted = await repository.complete(claim, error=error)
        logger.info(
            "completion job=%s fence=%s accepted=%s error=%s",
            claim.job_id,
            claim.fence,
            accepted,
            error,
        )
    finally:
        computation.cancel()
        heartbeat.cancel()
        await asyncio.gather(computation, heartbeat, return_exceptions=True)


async def run(settings: Settings) -> None:
    owner = f"{os.getpid()}-{uuid4()}"
    engine = make_engine(settings)
    repository = JobRepository(make_sessions(engine), settings)
    wakeup = RedisWakeup(settings.redis_url, settings.redis_channel)
    listener = asyncio.create_task(wakeup.listen())
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)

    async def consume() -> None:
        logger.info("worker_started owner=%s", owner)
        while not stopped.is_set():
            try:
                claim = await repository.claim(owner)
                if claim is None:
                    await wakeup.wait(settings.poll_seconds)
                else:
                    logger.info(
                        "claimed job=%s fence=%s owner=%s", claim.job_id, claim.fence, owner
                    )
                    await process(repository, claim, settings.heartbeat_seconds)
            except (SQLAlchemyError, OSError, TimeoutError):
                logger.exception("worker_database_operation_failed")
                await asyncio.sleep(settings.poll_seconds)

    consumer = asyncio.create_task(consume())
    stop_waiter = asyncio.create_task(stopped.wait())
    try:
        done, _ = await asyncio.wait({consumer, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if consumer in done:
            consumer.result()  # Programming errors must terminate visibly, not disappear in a task.
    finally:
        for task in (consumer, stop_waiter, listener):
            task.cancel()
        await asyncio.gather(consumer, stop_waiter, listener, return_exceptions=True)
        await wakeup.close()
        await engine.dispose()
        logger.info("worker_stopped owner=%s", owner)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    with suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
