import logging
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import Request, Response

logger = logging.getLogger(__name__)


async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = str(uuid4())
    request.state.request_id = request_id
    start = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request id=%s method=%s status=%s duration_ms=%.2f",
        request_id,
        request.method,
        response.status_code,
        (time.monotonic() - start) * 1000,
    )
    return response
