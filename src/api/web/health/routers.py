import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    container = request.app.state.container
    try:
        async with asyncio.timeout(2):
            async with container.engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM jobs LIMIT 1"))
    except (SQLAlchemyError, TimeoutError, OSError):
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "down"})
    redis_status = "up"
    try:
        async with asyncio.timeout(0.25):
            await container.wakeup.redis.ping()
    except (RedisError, TimeoutError, OSError):
        redis_status = "down"
    return JSONResponse(
        content={
            "status": "ok" if redis_status == "up" else "degraded",
            "database": "up",
            "redis": redis_status,
        }
    )
