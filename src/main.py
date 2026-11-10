from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.web.exceptions import register_exception_handlers
from src.api.web.health.routers import router as health_router
from src.api.web.jobs.routers import router as jobs_router
from src.api.web.middleware import request_context
from src.config import Settings
from src.infrastructure.adapters.di.container import build_container


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with build_container(config) as container:
            application.state.container = container
            yield

    application = FastAPI(
        title="Soup Training Queue",
        version="0.1.0",
        lifespan=lifespan,
        description="Async job queue with idempotent creation, fenced leases, and atomic effects.",
    )
    application.middleware("http")(request_context)
    register_exception_handlers(application)
    application.include_router(health_router)
    application.include_router(jobs_router)
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8000)
