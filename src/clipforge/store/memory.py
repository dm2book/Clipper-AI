"""In-memory implementation of the store.

A test double and a way to run the decision layer without a database in front
of it. **Not a way to run the system.** It loses everything when the process
ends, which is the one thing the persistence layer exists to prevent.

It is held to the same contract as the Postgres implementation by
`tests/test_store_contract.py`, and it goes out of its way to be
*inconvenient* in the same places the real one is:

* reads return deep copies, so a caller that mutates a record it fetched has
  not silently mutated the store — against Postgres it would have changed
  nothing until it saved, and a double that behaves otherwise hides the bug;
* writes are checked against the unit of work's tenant, so cross-tenant
  mistakes fail here exactly as row-level security makes them fail there;
* rollback actually rolls back.
"""

from __future__ import annotations

import copy
from bisect import bisect_left, insort
from datetime import datetime, timedelta
from typing import Any

from ..publish.types import ensure_utc, utcnow
from .errors import Conflict, NotFound, TenantScopeError
from .records import (
    ChannelRecord,
    ChannelSourceUseRecord,
    ClipRecord,
    JobRecord,
    MetricSnapshotRecord,
    ProjectRecord,
    ScheduleRecord,
    SocialAccountRecord,
    SourceRecord,
    TenantRecord,
    UploadRecord,
    UserRecord,
    VideoRecord,
)
from .schema import TABLES, Table

__all__ = ["MemoryDatabase", "MemoryUnitOfWork"]


class _Store:
    """The shared mutable state. One per `MemoryDatabase`."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[Any, Any]] = {name: {} for name in TABLES}


class _MemoryRepository:
    def __init__(self, uow: MemoryUnitOfWork, table: Table) -> None:
        self._uow = uow
        self._table = table

    # -- plumbing ----------------------------------------------------------

    @property
    def _rows(self) -> dict[Any, Any]:
        return self._uow._table(self._table.name)

    def _scoped(self) -> list[Any]:
        tenant = self._uow.tenant_id
        column = self._table.tenant_column
        return [r for r in self._rows.values() if getattr(r, column) == tenant]

    def _check_tenant(self, record: Any) -> None:
        value = getattr(record, self._table.tenant_column)
        if value != self._uow.tenant_id:
            raise TenantScopeError(
                f"{self._table.name} record {value!r} written under "
                f"tenant {self._uow.tenant_id!r}"
            )

    def _key(self, record: Any) -> Any:
        parts = tuple(getattr(record, column) for column in self._table.key)
        return parts[0] if len(parts) == 1 else parts

    # -- Repository --------------------------------------------------------

    def get(self, key: str) -> Any:
        row = self._rows.get(key)
        if row is None:
            return None
        if getattr(row, self._table.tenant_column) != self._uow.tenant_id:
            # Present, but not this tenant's. Indistinguishable from absent,
            # which is the same thing row-level security does — and the same
            # thing it must do, because "exists but not yours" leaks the id.
            return None
        return copy.deepcopy(row)

    def require(self, key: str) -> Any:
        row = self.get(key)
        if row is None:
            raise NotFound(f"{self._table.name}: no {key!r}")
        return row

    def add(self, record: Any) -> Any:
        self._uow._writable()
        self._check_tenant(record)
        key = self._key(record)
        if key in self._rows:
            raise Conflict(f"{self._table.name}: {key!r} already exists")
        self._uow._touch(self._table.name)
        stored = copy.deepcopy(record)
        self._stamp(stored, created=True)
        self._rows[key] = stored
        return copy.deepcopy(stored)

    def save(self, record: Any) -> Any:
        self._uow._writable()
        self._check_tenant(record)
        key = self._key(record)
        existing = self._rows.get(key)
        if existing is not None and (
            getattr(existing, self._table.tenant_column) != self._uow.tenant_id
        ):
            raise TenantScopeError(f"{self._table.name}: {key!r} belongs elsewhere")
        self._uow._touch(self._table.name)
        stored = copy.deepcopy(record)
        self._stamp(stored, created=existing is None)
        if existing is not None and hasattr(stored, "created_at"):
            stored.created_at = existing.created_at
        self._rows[key] = stored
        return copy.deepcopy(stored)

    def delete(self, key: str) -> bool:
        self._uow._writable()
        row = self._rows.get(key)
        if row is None:
            return False
        if getattr(row, self._table.tenant_column) != self._uow.tenant_id:
            return False
        self._uow._touch(self._table.name)
        del self._rows[key]
        return True

    def all(self) -> tuple[Any, ...]:
        return tuple(copy.deepcopy(r) for r in self._scoped())

    def count(self) -> int:
        return len(self._scoped())

    def _stamp(self, record: Any, *, created: bool) -> None:
        """Stand in for the column default and the trigger."""

        now = self._uow._clock()
        if created and hasattr(record, "created_at"):
            record.created_at = now
        if "updated_at" not in self._table.absent and hasattr(record, "updated_at"):
            record.updated_at = now


# ---------------------------------------------------------------------------
# Per-entity query methods
# ---------------------------------------------------------------------------


class _MemoryTenants(_MemoryRepository):
    pass


class _MemoryUsers(_MemoryRepository):
    def by_email(self, email: str) -> UserRecord | None:
        for row in self._scoped():
            if row.email == email:
                return copy.deepcopy(row)
        return None

    def active(self) -> tuple[UserRecord, ...]:
        return tuple(copy.deepcopy(r) for r in self._scoped() if r.active)


class _MemoryProjects(_MemoryRepository):
    def live(self) -> tuple[ProjectRecord, ...]:
        return tuple(copy.deepcopy(r) for r in self._scoped() if not r.archived)


class _MemoryChannels(_MemoryRepository):
    def for_project(self, project_id: str) -> tuple[ChannelRecord, ...]:
        rows = [r for r in self._scoped() if r.project_id == project_id]
        return tuple(copy.deepcopy(r) for r in rows)

    def in_state(self, *states: str) -> tuple[ChannelRecord, ...]:
        wanted = set(states)
        return tuple(copy.deepcopy(r) for r in self._scoped() if r.state in wanted)


class _MemoryAccounts(_MemoryRepository):
    def for_channel(self, channel_id: str) -> tuple[SocialAccountRecord, ...]:
        rows = [r for r in self._scoped() if r.channel_id == channel_id]
        return tuple(copy.deepcopy(r) for r in rows)

    def on_platform(self, platform: str) -> tuple[SocialAccountRecord, ...]:
        rows = [r for r in self._scoped() if r.platform == platform]
        return tuple(copy.deepcopy(r) for r in rows)

    def refresh_expiring_before(
        self, moment: datetime
    ) -> tuple[SocialAccountRecord, ...]:
        moment = ensure_utc(moment)
        rows = [
            r
            for r in self._scoped()
            if r.refresh_valid_until is not None and r.refresh_valid_until < moment
        ]
        rows.sort(key=lambda r: r.refresh_valid_until)
        return tuple(copy.deepcopy(r) for r in rows)


class _MemorySources(_MemoryRepository):
    def by_fingerprint(self, fingerprint: str) -> SourceRecord | None:
        for row in self._scoped():
            if row.fingerprint == fingerprint:
                return copy.deepcopy(row)
        return None

    def add(self, record: SourceRecord) -> SourceRecord:
        if record.fingerprint and self.by_fingerprint(record.fingerprint):
            raise Conflict(f"sources: fingerprint {record.fingerprint!r} already held")
        return super().add(record)

    def rights_expiring_before(self, moment: datetime) -> tuple[SourceRecord, ...]:
        moment = ensure_utc(moment)
        rows = [
            r
            for r in self._scoped()
            if r.rights_expires_at is not None and r.rights_expires_at < moment
        ]
        rows.sort(key=lambda r: r.rights_expires_at)
        return tuple(copy.deepcopy(r) for r in rows)

    def mark_used(self, channel_id: str, source_id: str, used_at: datetime) -> None:
        self._uow._writable()
        uses = self._uow._table("channel_source_uses")
        key = (channel_id, source_id)
        if key in uses:
            return
        self._uow._touch("channel_source_uses")
        uses[key] = ChannelSourceUseRecord(
            tenant_id=self._uow.tenant_id,
            channel_id=channel_id,
            source_id=source_id,
            used_at=ensure_utc(used_at),
        )

    def used_by(self, channel_id: str) -> tuple[ChannelSourceUseRecord, ...]:
        uses = self._uow._table("channel_source_uses")
        rows = [
            r
            for r in uses.values()
            if r.tenant_id == self._uow.tenant_id and r.channel_id == channel_id
        ]
        rows.sort(key=lambda r: r.used_at)
        return tuple(copy.deepcopy(r) for r in rows)

    def unused_by(self, channel_id: str) -> tuple[SourceRecord, ...]:
        taken = {u.source_id for u in self.used_by(channel_id)}
        rows = [r for r in self._scoped() if r.id not in taken]
        rows.sort(key=lambda r: r.created_at)
        return tuple(copy.deepcopy(r) for r in rows)


class _MemoryVideos(_MemoryRepository):
    def for_clip(self, clip_id: str) -> tuple[VideoRecord, ...]:
        rows = [r for r in self._scoped() if r.clip_id == clip_id]
        rows.sort(key=lambda r: r.created_at)
        return tuple(copy.deepcopy(r) for r in rows)

    def in_state(self, *states: str) -> tuple[VideoRecord, ...]:
        wanted = set(states)
        return tuple(copy.deepcopy(r) for r in self._scoped() if r.state in wanted)


class _MemoryClips(_MemoryRepository):
    def for_channel(self, channel_id: str, limit: int = 0) -> tuple[ClipRecord, ...]:
        rows = [r for r in self._scoped() if r.channel_id == channel_id]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        if limit > 0:
            rows = rows[:limit]
        return tuple(copy.deepcopy(r) for r in rows)

    def for_source(self, source_id: str) -> tuple[ClipRecord, ...]:
        rows = [r for r in self._scoped() if r.source_id == source_id]
        rows.sort(key=lambda r: r.start_ms)
        return tuple(copy.deepcopy(r) for r in rows)


class _MemorySchedules(_MemoryRepository):
    def enabled_for_channel(self, channel_id: str) -> tuple[ScheduleRecord, ...]:
        rows = [
            r for r in self._scoped() if r.channel_id == channel_id and r.enabled
        ]
        return tuple(copy.deepcopy(r) for r in rows)


class _MemoryUploads(_MemoryRepository):
    def add(self, record: UploadRecord) -> UploadRecord:
        if record.idempotency_key and self.by_idempotency_key(record.idempotency_key):
            raise Conflict(
                f"uploads: idempotency key {record.idempotency_key!r} already held"
            )
        return super().add(record)

    def by_idempotency_key(self, key: str) -> UploadRecord | None:
        for row in self._scoped():
            if row.idempotency_key == key:
                return copy.deepcopy(row)
        return None

    def due(self, now: datetime, limit: int = 100) -> tuple[UploadRecord, ...]:
        now = ensure_utc(now)
        rows = [
            r
            for r in self._scoped()
            if r.state in ("scheduled", "retrying") and r.run_at <= now
        ]
        rows.sort(key=lambda r: r.run_at)
        return tuple(copy.deepcopy(r) for r in rows[:limit])

    def _between(
        self, attribute: str, value: str, start: datetime, end: datetime
    ) -> tuple[UploadRecord, ...]:
        start, end = ensure_utc(start), ensure_utc(end)
        rows = [
            r
            for r in self._scoped()
            if getattr(r, attribute) == value and start <= r.run_at < end
        ]
        rows.sort(key=lambda r: r.run_at)
        return tuple(copy.deepcopy(r) for r in rows)

    def for_account_between(
        self, account_id: str, start: datetime, end: datetime
    ) -> tuple[UploadRecord, ...]:
        return self._between("account_id", account_id, start, end)

    def for_channel_between(
        self, channel_id: str, start: datetime, end: datetime
    ) -> tuple[UploadRecord, ...]:
        return self._between("channel_id", channel_id, start, end)

    def in_state(self, *states: str) -> tuple[UploadRecord, ...]:
        wanted = set(states)
        rows = [r for r in self._scoped() if r.state in wanted]
        rows.sort(key=lambda r: r.run_at)
        return tuple(copy.deepcopy(r) for r in rows)


class _MemoryMetrics(_MemoryRepository):
    def append(self, record: MetricSnapshotRecord) -> MetricSnapshotRecord:
        for row in self._scoped():
            if row.upload_id == record.upload_id and row.age_hours == record.age_hours:
                raise Conflict(
                    f"metric_snapshots: {record.upload_id!r} already read at "
                    f"{record.age_hours}h"
                )
        return super().add(record)

    def for_upload(self, upload_id: str) -> tuple[MetricSnapshotRecord, ...]:
        rows = [r for r in self._scoped() if r.upload_id == upload_id]
        rows.sort(key=lambda r: r.age_hours)
        return tuple(copy.deepcopy(r) for r in rows)

    def latest_for_upload(self, upload_id: str) -> MetricSnapshotRecord | None:
        rows = self.for_upload(upload_id)
        return rows[-1] if rows else None

    def between(
        self, start: datetime, end: datetime
    ) -> tuple[MetricSnapshotRecord, ...]:
        start, end = ensure_utc(start), ensure_utc(end)
        rows = [r for r in self._scoped() if start <= r.taken_at < end]
        rows.sort(key=lambda r: r.taken_at)
        return tuple(copy.deepcopy(r) for r in rows)

    def at_age(
        self, age_hours: float, tolerance: float = 0.5
    ) -> tuple[MetricSnapshotRecord, ...]:
        rows = [
            r for r in self._scoped() if abs(r.age_hours - age_hours) <= tolerance
        ]
        rows.sort(key=lambda r: r.taken_at)
        return tuple(copy.deepcopy(r) for r in rows)


class _MemoryTranscriptions(_MemoryRepository):
    def for_source(self, source_id: str) -> Any:
        for row in self._scoped():
            if row.source_id == source_id:
                return copy.deepcopy(row)
        return None

    def add(self, record: Any) -> Any:
        # `unique(tenant_id, source_id)` in Postgres. Enforced here too, or the
        # in-memory tests pass on a second run row the database would refuse,
        # and the constraint error arrives instead from inside the queue.
        if record.source_id and self.for_source(record.source_id):
            raise Conflict(
                f"transcription_runs: source {record.source_id!r} already has a run"
            )
        return super().add(record)

    def save(self, record: Any) -> Any:
        if record.source_id:
            held = self.for_source(record.source_id)
            if held is not None and held.id != record.id:
                raise Conflict(
                    f"transcription_runs: source {record.source_id!r} already "
                    f"has run {held.id!r}"
                )
        return super().save(record)

    def in_state(self, *states: str) -> tuple[Any, ...]:
        wanted = set(states)
        rows = [r for r in self._scoped() if r.state in wanted]
        rows.sort(key=lambda r: r.created_at)
        return tuple(copy.deepcopy(r) for r in rows)


class _MemoryAcquisitions(_MemoryRepository):
    def for_ref(self, kind: str, ref_key: str, channel_id: str | None) -> Any:
        for row in self._scoped():
            if (row.kind, row.ref_key, row.channel_id) == (kind, ref_key, channel_id):
                return copy.deepcopy(row)
        return None

    def in_state(self, *states: str) -> tuple[Any, ...]:
        wanted = set(states)
        rows = [r for r in self._scoped() if r.state in wanted]
        rows.sort(key=lambda r: r.created_at)
        return tuple(copy.deepcopy(r) for r in rows)

    def for_source(self, source_id: str) -> tuple[Any, ...]:
        rows = [r for r in self._scoped() if r.source_id == source_id]
        return tuple(copy.deepcopy(r) for r in rows)


class _MemoryRevenue(_MemoryRepository):
    def for_period(self, period: str) -> tuple[Any, ...]:
        rows = [r for r in self._scoped() if r.period == period]
        rows.sort(key=lambda r: r.project_id)
        return tuple(copy.deepcopy(r) for r in rows)

    def for_project(self, project_id: str, period: str) -> Any:
        for row in self._scoped():
            if row.project_id == project_id and row.period == period:
                return copy.deepcopy(row)
        return None


class _MemoryPools(_MemoryRepository):
    def on_platform(self, platform: str) -> tuple[Any, ...]:
        rows = [r for r in self._scoped() if r.platform == platform]
        rows.sort(key=lambda r: r.id)
        return tuple(copy.deepcopy(r) for r in rows)


class _MemoryJobs(_MemoryRepository):
    def enqueue(self, record: JobRecord) -> JobRecord:
        if record.dedupe_key:
            for row in self._scoped():
                if row.dedupe_key == record.dedupe_key:
                    return copy.deepcopy(row)
        return super().add(record)

    def claim(
        self,
        owner: str,
        now: datetime,
        lease_s: int = 300,
        kinds: tuple[str, ...] = (),
        limit: int = 1,
    ) -> tuple[JobRecord, ...]:
        self._uow._writable()
        now = ensure_utc(now)
        wanted = set(kinds)
        ready = [
            r
            for r in self._scoped()
            if r.state == "queued"
            and r.run_after <= now
            and (not wanted or r.kind in wanted)
        ]
        ready.sort(key=lambda r: (r.priority, r.run_after))
        taken = []
        for row in ready[:limit]:
            self._uow._touch("jobs")
            row.state = "leased"
            row.lease_owner = owner
            row.lease_until = now + timedelta(seconds=lease_s)
            row.updated_at = now
            taken.append(copy.deepcopy(row))
        return tuple(taken)

    def heartbeat(self, job_id: str, owner: str, until: datetime) -> bool:
        self._uow._writable()
        row = self._rows.get(job_id)
        if row is None or row.tenant_id != self._uow.tenant_id:
            return False
        if row.lease_owner != owner or row.state not in ("leased", "running"):
            return False
        self._uow._touch("jobs")
        row.lease_until = ensure_utc(until)
        return True

    def succeed(
        self, job_id: str, result: dict | None, now: datetime
    ) -> JobRecord:
        row = self._mutate(job_id)
        now = ensure_utc(now)
        row.state = "succeeded"
        row.result = copy.deepcopy(result)
        row.finished_at = now
        row.updated_at = now
        row.lease_owner = ""
        row.lease_until = None
        return copy.deepcopy(row)

    def fail(
        self, job_id: str, error: str, retry_at: datetime | None, now: datetime
    ) -> JobRecord:
        row = self._mutate(job_id)
        now = ensure_utc(now)
        row.attempts += 1
        row.last_error = error
        row.lease_owner = ""
        row.lease_until = None
        row.updated_at = now
        if retry_at is not None and row.attempts < row.max_attempts:
            row.state = "queued"
            row.run_after = ensure_utc(retry_at)
        else:
            row.state = "dead"
            row.finished_at = now
        return copy.deepcopy(row)

    def reap(self, now: datetime, limit: int = 100) -> int:
        self._uow._writable()
        now = ensure_utc(now)
        expired = [
            r
            for r in self._scoped()
            if r.state in ("leased", "running")
            and r.lease_until is not None
            and r.lease_until < now
        ]
        for row in expired[:limit]:
            self._uow._touch("jobs")
            row.state = "queued"
            row.lease_owner = ""
            row.lease_until = None
            row.updated_at = now
        return min(len(expired), limit)

    def pending(self, limit: int = 100) -> tuple[JobRecord, ...]:
        rows = [r for r in self._scoped() if r.state == "queued"]
        rows.sort(key=lambda r: (r.priority, r.run_after))
        return tuple(copy.deepcopy(r) for r in rows[:limit])

    def in_state(self, *states: str) -> tuple[JobRecord, ...]:
        wanted = set(states)
        rows = [r for r in self._scoped() if r.state in wanted]
        rows.sort(key=lambda r: r.created_at)
        return tuple(copy.deepcopy(r) for r in rows)

    def _mutate(self, job_id: str) -> JobRecord:
        self._uow._writable()
        row = self._rows.get(job_id)
        if row is None or row.tenant_id != self._uow.tenant_id:
            raise NotFound(f"jobs: no {job_id!r}")
        self._uow._touch("jobs")
        return row


# ---------------------------------------------------------------------------
# Unit of work
# ---------------------------------------------------------------------------


class MemoryUnitOfWork:
    """A transaction over the in-memory store.

    Rollback is real: the first time a table is written, its previous contents
    are stashed, and abandoning the block puts them back. A double whose
    rollback is a no-op makes every error-path test a lie.
    """

    def __init__(self, database: MemoryDatabase, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("a unit of work needs a tenant")
        self.tenant_id = tenant_id
        self._database = database
        self._store = database._store
        self._undo: dict[str, dict[Any, Any]] = {}
        self._open = False
        self._rolled_back = False

        self.tenants = _MemoryTenants(self, TABLES["tenants"])
        self.users = _MemoryUsers(self, TABLES["users"])
        self.projects = _MemoryProjects(self, TABLES["projects"])
        self.channels = _MemoryChannels(self, TABLES["channels"])
        self.accounts = _MemoryAccounts(self, TABLES["social_accounts"])
        self.sources = _MemorySources(self, TABLES["sources"])
        self.videos = _MemoryVideos(self, TABLES["videos"])
        self.clips = _MemoryClips(self, TABLES["clips"])
        self.schedules = _MemorySchedules(self, TABLES["schedules"])
        self.uploads = _MemoryUploads(self, TABLES["uploads"])
        self.metrics = _MemoryMetrics(self, TABLES["metric_snapshots"])
        self.jobs = _MemoryJobs(self, TABLES["jobs"])
        self.revenue = _MemoryRevenue(self, TABLES["revenue_entries"])
        self.pools = _MemoryPools(self, TABLES["quota_pools"])
        self.acquisitions = _MemoryAcquisitions(self, TABLES["acquisition_runs"])
        self.transcriptions = _MemoryTranscriptions(
            self, TABLES["transcription_runs"]
        )

    # -- plumbing ----------------------------------------------------------

    def _clock(self) -> datetime:
        return self._database.clock()

    def _table(self, name: str) -> dict[Any, Any]:
        return self._store.tables[name]

    def _touch(self, name: str) -> None:
        if name not in self._undo:
            self._undo[name] = copy.deepcopy(self._store.tables[name])

    def _writable(self) -> None:
        if self._rolled_back:
            raise RuntimeError("this unit of work was rolled back")

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> MemoryUnitOfWork:
        if self._open:
            raise RuntimeError("unit of work is not re-entrant")
        self._open = True
        return self

    def __exit__(self, exc_type: object, *_: object) -> bool:
        self._open = False
        if exc_type is not None or self._rolled_back:
            self._restore()
        self._undo.clear()
        return False

    def rollback(self) -> None:
        self._restore()
        self._rolled_back = True

    def _restore(self) -> None:
        for name, rows in self._undo.items():
            self._store.tables[name] = rows
        self._undo.clear()


class MemoryDatabase:
    """Holds the tables. One instance is one "database"."""

    def __init__(self, clock: Any = None) -> None:
        self._store = _Store()
        self.clock = clock or utcnow

    def unit_of_work(self, tenant_id: str) -> MemoryUnitOfWork:
        return MemoryUnitOfWork(self, tenant_id)

    def close(self) -> None:
        """Nothing to close. Present so callers can be written against the
        protocol and swapped onto Postgres without a code change."""

    def clear(self) -> None:
        self._store = _Store()
