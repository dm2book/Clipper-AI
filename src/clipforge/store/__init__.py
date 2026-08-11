"""Persistence for ClipForge AI.

    from clipforge.store import open_database

    db = open_database()                     # DATABASE_URL, or in-memory
    with db.unit_of_work("ten_acme") as uow:
        uow.channels.save(channel)
        uow.jobs.enqueue(job)
    # committed on the way out; rolled back if the block raised

Two implementations behind one contract: `PostgresDatabase`, which is the
system, and `MemoryDatabase`, which is a test double. `tests/test_store_contract.py`
runs the same suite against both, so the in-memory tests are evidence about the
Postgres path rather than about themselves.

The schema is owned by Prisma, in `db/` — see `db/README.md`. Prisma's client
is TypeScript and is not used; its migration engine is, and it emits the plain
SQL in `db/migrations/`.
"""

from __future__ import annotations

import os
from typing import Any

from .errors import Conflict, NotFound, ReadOnly, StoreError, TenantScopeError
from .memory import MemoryDatabase, MemoryUnitOfWork
from .protocols import (
    ChannelRepository,
    ClipRepository,
    Database,
    JobRepository,
    MetricRepository,
    ProjectRepository,
    Repository,
    ScheduleRepository,
    SocialAccountRepository,
    SourceRepository,
    TenantRepository,
    UnitOfWork,
    UploadRepository,
    UserRepository,
    VideoRepository,
)
from .records import (
    AcquisitionRunRecord,
    ChannelRecord,
    ChannelSourceUseRecord,
    ClipRecord,
    JobRecord,
    MetricSnapshotRecord,
    ProjectRecord,
    QuotaPoolRecord,
    Record,
    RevenueEntryRecord,
    ScheduleRecord,
    SocialAccountRecord,
    SourceRecord,
    TenantRecord,
    UploadRecord,
    UserRecord,
    VideoRecord,
)
from .schema import TABLES, Table, table_for

__all__ = [
    "open_database",
    "MemoryDatabase",
    "MemoryUnitOfWork",
    "Database",
    "UnitOfWork",
    "Repository",
    "TenantRepository",
    "UserRepository",
    "ProjectRepository",
    "ChannelRepository",
    "SocialAccountRepository",
    "SourceRepository",
    "VideoRepository",
    "ClipRepository",
    "ScheduleRepository",
    "UploadRepository",
    "MetricRepository",
    "JobRepository",
    "Record",
    "TenantRecord",
    "UserRecord",
    "ProjectRecord",
    "ChannelRecord",
    "SocialAccountRecord",
    "SourceRecord",
    "ChannelSourceUseRecord",
    "VideoRecord",
    "ClipRecord",
    "ScheduleRecord",
    "UploadRecord",
    "MetricSnapshotRecord",
    "JobRecord",
    "RevenueEntryRecord",
    "QuotaPoolRecord",
    "AcquisitionRunRecord",
    "StoreError",
    "NotFound",
    "Conflict",
    "TenantScopeError",
    "ReadOnly",
    "TABLES",
    "Table",
    "table_for",
]


def open_database(dsn: str | None = None, **kwargs: Any) -> Database:
    """The database named by `dsn`, or by `DATABASE_URL`, or an in-memory one.

    Falling back to memory is a convenience for tests and for running the
    decision layer standalone. It is not a mode to deploy in — it loses
    everything when the process ends. `PostgresDatabase` is imported lazily so
    that fallback does not require psycopg to be installed.
    """

    dsn = dsn or os.environ.get("DATABASE_URL", "")
    if not dsn:
        return MemoryDatabase()
    from .postgres import PostgresDatabase

    return PostgresDatabase(dsn, **kwargs)
