"""ClipForge AI — the HTTP API.

    from clipforge.api import build_services, create_app

    app = create_app(build_services())
    # uvicorn clipforge.api.server:app

Every layer beneath this one was a library that nothing turned an HTTP request
into a call on. This is that layer: bearer-token authentication producing a
`Principal`, a tenant taken from the token rather than the URL, and reads that
go through the tenant-scoped unit of work so row-level security sits underneath
every query.

Served under `/api/v1`. The OpenAPI document at `/api/v1/openapi.json` is what
the dashboard's TypeScript types are generated from, so a field renamed here
breaks the dashboard's type check rather than appearing as `undefined` in a
table cell.
"""

from .app import API_PREFIX, DEFAULT_ORIGINS, build_services, create_app
from .deps import Context, Services

__all__ = [
    "API_PREFIX",
    "Context",
    "DEFAULT_ORIGINS",
    "Services",
    "build_services",
    "create_app",
]
