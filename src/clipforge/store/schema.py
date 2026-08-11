"""Table descriptors: the single place that knows a record maps to a row.

Column lists are derived from the dataclasses rather than written out again.
A hand-maintained second copy of every column name is a list that drifts, and
the way it drifts is silent — a field added to a record and forgotten here
simply stops being saved, and nothing fails until someone notices the value is
missing after a restart. Deriving it means adding a field to a record is
enough, and a field with no column fails loudly at the first write.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from .records import (
    AcquisitionRunRecord,
    ChannelRecord,
    ChannelSourceUseRecord,
    ClipRecord,
    JobRecord,
    MetricSnapshotRecord,
    ProjectRecord,
    QuotaPoolRecord,
    RevenueEntryRecord,
    ScheduleRecord,
    SocialAccountRecord,
    SourceRecord,
    TenantRecord,
    UploadRecord,
    UserRecord,
    VideoRecord,
)

__all__ = ["Table", "TABLES", "table_for"]

#: Written by the database, never by the application: `created_at` from a
#: column default, `updated_at` from the trigger in migration 002.
_DB_MANAGED = frozenset({"created_at", "updated_at"})


@dataclass(frozen=True)
class Table:
    name: str
    record: type
    #: Primary key columns. Everything but the join table is keyed by `id`.
    key: tuple[str, ...] = ("id",)
    #: jsonb columns, which need psycopg's `Json` wrapper on the way in.
    json_columns: frozenset[str] = frozenset()
    #: Columns this table does not have, despite the record carrying the field.
    absent: frozenset[str] = frozenset()
    #: True when rows carry `tenant_id`; false only for `tenants`, which is
    #: scoped by its own `id`.
    tenant_column: str = "tenant_id"

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self.record) if f.name not in self.absent)

    @property
    def writable(self) -> tuple[str, ...]:
        """Columns the application supplies. Excludes the database-managed ones."""
        return tuple(c for c in self.columns if c not in _DB_MANAGED)

    @property
    def updatable(self) -> tuple[str, ...]:
        return tuple(c for c in self.writable if c not in self.key)


TABLES: dict[str, Table] = {
    # `tenants` is scoped by its own `id`. The record mirrors that into
    # `tenant_id` so repositories need no special case, but there is no such
    # column — a tenant does not belong to a tenant.
    "tenants": Table(
        "tenants",
        TenantRecord,
        tenant_column="id",
        absent=frozenset({"tenant_id"}),
    ),
    "users": Table("users", UserRecord),
    "projects": Table("projects", ProjectRecord),
    "channels": Table("channels", ChannelRecord),
    "social_accounts": Table("social_accounts", SocialAccountRecord),
    "sources": Table("sources", SourceRecord),
    "channel_source_uses": Table(
        "channel_source_uses",
        ChannelSourceUseRecord,
        key=("channel_id", "source_id"),
    ),
    "videos": Table("videos", VideoRecord, json_columns=frozenset({"render_plan"})),
    "clips": Table(
        "clips",
        ClipRecord,
        json_columns=frozenset(
            {"scores", "features", "hook_candidates", "caption_track"}
        ),
    ),
    "schedules": Table("schedules", ScheduleRecord),
    "uploads": Table(
        "uploads", UploadRecord, json_columns=frozenset({"metadata", "attempts"})
    ),
    "metric_snapshots": Table(
        "metric_snapshots",
        MetricSnapshotRecord,
        json_columns=frozenset({"retention_curve"}),
        # Append-only, so there is nothing for an `updated_at` to record. The
        # record inherits the field from `Record`; the table has no column.
        absent=frozenset({"updated_at"}),
    ),
    "jobs": Table("jobs", JobRecord, json_columns=frozenset({"payload", "result"})),
    "revenue_entries": Table("revenue_entries", RevenueEntryRecord),
    "quota_pools": Table("quota_pools", QuotaPoolRecord),
    "acquisition_runs": Table(
        "acquisition_runs",
        AcquisitionRunRecord,
        json_columns=frozenset({"metadata"}),
    ),
}


def table_for(record: Any) -> Table:
    """The table a record belongs to.

    Looked up by exact type. A subclass of a record is not silently treated as
    the parent: it would round-trip through the parent's column list and lose
    every field the subclass added, which is exactly the kind of quiet data
    loss this layer exists to prevent.
    """

    wanted = record if isinstance(record, type) else type(record)
    for table in TABLES.values():
        if table.record is wanted:
            return table
    raise KeyError(f"no table for {wanted.__name__}")
