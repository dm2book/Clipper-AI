"""The HTTP surface.

The gap this closes was the largest one left: every layer underneath — stores,
acquisition, transcription, rendering, publishing, auth — was a library that
nothing turned an HTTP request into a call on. `authenticate()` returned a
`Principal` and no request ever produced one.

## Errors have one shape

Every failure leaves here as `{"error": {"code", "message"}}`, whether it
started as an `AuthError`, an `HTTPException`, or something unhandled. A client
that has to branch on three error shapes writes two of them wrong, and the one
it gets wrong is the rare path nobody tests.

`code` is for machines and stable. `message` is for a person and may be
rewritten. An unhandled exception becomes a generic 500 whose message says
nothing about the failure — the detail goes to the log, because a stack trace
in a response body is a map of the system for anyone who can provoke one.

## CORS is explicit

The dashboard runs on a different origin in development, so CORS is needed and
the allowed origins are named. `allow_origins=["*"]` with credentials is
refused by browsers anyway and, worse, works fine until the first cookie —
which is why the list is configuration rather than a default.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..auth.types import AuthError, RateLimited
from .deps import Services

log = logging.getLogger("clipforge.api")

__all__ = ["create_app", "build_services", "DEFAULT_ORIGINS"]

DEFAULT_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)

API_PREFIX = "/api/v1"


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    body.update({k: v for k, v in extra.items() if v is not None})
    return JSONResponse(status_code=status, content={"error": body})


def create_app(services: Services, *, origins: tuple[str, ...] = ()) -> FastAPI:
    """Build the app around already-constructed services.

    Services are injected rather than built here so a test can hand in an
    in-memory database and an in-memory auth store, and so two apps can exist
    in one process. An app that constructs its own database on import is an app
    whose tests need a database on import.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Not closed here: the caller built the services and may still be
        # using them. `run.py` closes them; a test closes its own.

    app = FastAPI(
        title="ClipForge AI",
        version="0.1.0",
        summary="Long-form content in, short-form vertical clips out.",
        lifespan=lifespan,
        # Served under a prefix so the dashboard can be hosted from the same
        # origin in production without a path collision.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
    )
    app.state.services = services

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins or _origins_from_env()),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        # So a browser can read the pagination total on a cross-origin read.
        expose_headers=["X-Total-Count"],
    )

    # -- error handling ----------------------------------------------------

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, error: AuthError) -> JSONResponse:
        return _error(
            error.status, error.code, error.message,
            retry_after_s=getattr(error, "retry_after_s", None),
        )

    @app.exception_handler(RateLimited)
    async def _rate_limited(request: Request, error: RateLimited) -> JSONResponse:
        response = _error(429, error.code, error.message,
                          retry_after_s=error.retry_after_s)
        response.headers["Retry-After"] = str(int(error.retry_after_s))
        return response

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, error: HTTPException) -> JSONResponse:
        detail = error.detail
        if isinstance(detail, dict) and "code" in detail:
            response = _error(error.status_code, detail["code"],
                              detail.get("message", ""))
        else:
            response = _error(error.status_code, _code_for(error.status_code),
                              str(detail))
        for key, value in (error.headers or {}).items():
            response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # Named to the field rather than echoed raw: pydantic's default body
        # includes the input that failed, which for a login is the password.
        fields = ", ".join(
            ".".join(str(p) for p in item["loc"][1:]) or "body"
            for item in error.errors()
        )
        return _error(422, "INVALID_REQUEST",
                      f"Check these fields: {fields}.")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, error: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error(
            500, "INTERNAL",
            "Something went wrong on our side. The failure has been logged.",
        )

    # -- routes ------------------------------------------------------------

    from .routes import (
        analytics,
        auth,
        channels,
        overview,
        settings,
        sources,
        uploads,
    )

    from .schemas import ErrorResponse

    # Declared on every router so the failure shape is in the OpenAPI document
    # and therefore in the generated TypeScript. A contract that describes only
    # the happy path leaves every client to invent its own error handling.
    errors = {
        400: {"model": ErrorResponse, "description": "Bad request"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Forbidden"},
        404: {"model": ErrorResponse, "description": "Not found"},
        409: {"model": ErrorResponse, "description": "Conflict"},
        429: {"model": ErrorResponse, "description": "Rate limited"},
    }
    for module in (auth, overview, channels, sources, uploads, analytics,
                   settings):
        app.include_router(module.router, prefix=API_PREFIX, responses=errors)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, Any]:
        """Liveness and a real readiness check.

        Actually touches the database rather than returning a constant. A
        health check that cannot fail is a health check that tells a load
        balancer nothing.
        """

        healthy = True
        detail = "ok"
        try:
            services.database.unit_of_work("__health__").__enter__().rollback()
        except Exception as error:                          # noqa: BLE001
            healthy, detail = False, f"{type(error).__name__}"
        return {"status": "ok" if healthy else "degraded", "database": detail}

    return app


def _code_for(status: int) -> str:
    return {
        400: "BAD_REQUEST", 401: "NOT_AUTHENTICATED", 403: "FORBIDDEN",
        404: "NOT_FOUND", 409: "CONFLICT", 422: "INVALID_REQUEST",
        429: "RATE_LIMITED",
    }.get(status, "ERROR")


def _origins_from_env() -> tuple[str, ...]:
    raw = os.environ.get("CLIPFORGE_API_ORIGINS", "").strip()
    if not raw:
        return DEFAULT_ORIGINS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def build_services(
    *,
    dsn: str = "",
    auth_dsn: str = "",
    in_memory: bool = False,
) -> Services:
    """Construct the real services from configuration.

    `in_memory=True` gives a working API with nothing persisted — useful for
    the demo and for a first run, and never for a deployment, which is why it
    has to be asked for explicitly rather than being what happens when a DSN
    is missing.
    """

    from ..auth import AccessTokenIssuer, AuthService, MemoryAuthStore
    from ..auth.config import config_from_env
    from ..store import MemoryDatabase

    config = config_from_env()

    if in_memory:
        database: Any = MemoryDatabase()
        auth_store: Any = MemoryAuthStore()
    else:
        from ..auth.postgres import PostgresAuthStore
        from ..store.postgres import PostgresDatabase

        dsn = dsn or os.environ.get("CLIPFORGE_DSN", "")
        auth_dsn = auth_dsn or os.environ.get("CLIPFORGE_AUTH_DSN", "")
        if not dsn or not auth_dsn:
            raise RuntimeError(
                "CLIPFORGE_DSN and CLIPFORGE_AUTH_DSN are both required. They "
                "are deliberately separate connections as separate roles: the "
                "request path must not be able to read a password hash."
            )
        database = PostgresDatabase(dsn)
        auth_store = PostgresAuthStore(auth_dsn)

    auth = AuthService(
        auth_store,
        AccessTokenIssuer(
            config.keyring, issuer=config.issuer, audience=config.audience,
            ttl_s=config.access_ttl_s,
        ),
        config=config,
    )
    return Services(database=database, auth=auth)
