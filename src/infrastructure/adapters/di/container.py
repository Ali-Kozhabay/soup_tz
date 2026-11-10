from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from src.application.services.jobs import JobService
from src.config import Settings
from src.infrastructure.adapters.storage.redis import RedisWakeup
from src.infrastructure.database.session import make_engine, make_sessions
from src.infrastructure.repositories.jobs import JobRepository


@dataclass(frozen=True, slots=True)
class Container:
    engine: AsyncEngine
    wakeup: RedisWakeup
    jobs: JobService


@asynccontextmanager
async def build_container(settings: Settings) -> AsyncIterator[Container]:
    """Own app-scoped resources; every repository call still owns its transaction."""
    engine = make_engine(settings)
    wakeup = RedisWakeup(settings.redis_url, settings.redis_channel)
    repository = JobRepository(make_sessions(engine), settings)
    try:
        yield Container(engine=engine, wakeup=wakeup, jobs=JobService(repository, wakeup))
    finally:
        await wakeup.close()
        await engine.dispose()
