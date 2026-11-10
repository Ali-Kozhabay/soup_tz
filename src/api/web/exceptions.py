import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from src.core.exceptions import (
    IdempotencyConflict,
    InvalidCursor,
    JobNotFound,
    TerminalConflict,
)

logger = logging.getLogger(__name__)


async def domain_error(request: Request, exc: Exception) -> JSONResponse:
    code = 404 if isinstance(exc, JobNotFound) else 400 if isinstance(exc, InvalidCursor) else 409
    detail = "Job not found" if isinstance(exc, JobNotFound) else str(exc)
    return JSONResponse(
        status_code=code, content={"detail": detail, "request_id": request.state.request_id}
    )


async def database_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("database_unavailable request_id=%s", request.state.request_id, exc_info=exc)
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={
            "detail": "Database temporarily unavailable",
            "request_id": request.state.request_id,
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    for error_type in (JobNotFound, InvalidCursor, IdempotencyConflict, TerminalConflict):
        app.add_exception_handler(error_type, domain_error)
    app.add_exception_handler(SQLAlchemyError, database_error)
