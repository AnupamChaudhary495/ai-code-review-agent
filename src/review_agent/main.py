"""FastAPI application entry point."""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from . import db, ingest
from .config import get_settings
from .logging_setup import configure_logging, correlation_id_var

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    db.init_schema()  # fail fast if PostgreSQL is unreachable or misconfigured
    logger.info("application started")
    yield


app = FastAPI(title="AI Code Review Agent", version="0.1.0", lifespan=lifespan)
app.include_router(ingest.router)


@app.middleware("http")
async def correlation_and_access_log(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # GitHub sends a unique delivery GUID per webhook; reuse it as the
    # correlation ID so log lines can be tied back to a specific delivery.
    correlation_id = request.headers.get("X-GitHub-Delivery") or str(uuid.uuid4())
    token = correlation_id_var.set(correlation_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        logger.info(
            "request handled",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        response.headers["X-Correlation-ID"] = correlation_id
        return response
    finally:
        correlation_id_var.reset(token)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
