import os
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.config import Settings
from src.infrastructure.database.session import make_engine, make_sessions
from src.infrastructure.repositories.jobs import JobRepository
from src.main import create_app


@pytest.fixture(scope="session")
def settings():
    local = Settings()
    default = (
        make_url(local.database_url).set(database="soup_test").render_as_string(hide_password=False)
    )
    url = os.environ.get("SOUP_TEST_DATABASE_URL", default)
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Refusing destructive test setup: database name must end in _test")
    config = Settings(database_url=url, lease_seconds=2, heartbeat_seconds=0.2, poll_seconds=0.05)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env={**os.environ, "SOUP_DATABASE_URL": url},
    )
    return config


@pytest.fixture
async def repository(settings):
    engine = make_engine(settings)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE job_effects, job_attempts, jobs"))
    yield JobRepository(make_sessions(engine), settings)
    await engine.dispose()


@pytest.fixture
async def client(repository, settings):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client
