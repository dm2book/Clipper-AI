"""Repository and unit-of-work interfaces.

Every method here is implemented twice — once against Postgres, once in
memory — and the two are held to the same contract by one shared test suite
(`tests/test_store_contract.py`). That shared suite is the point of the
protocols. An in-memory double that is merely *similar* to the real thing is a
test that passes while the production path is broken, which is the failure
mode this whole layer exists to avoid.

The in-memory implementation is a test double and a local-development
convenience. It is not a supported way to run the system: it loses everything
on restart, which is the one thing that must not happen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from .records import (  # noqa: F401 - referenced in Protocol annotations
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
    TranscriptionRunRecord,
    UploadRecord,
    UserRecord,
    VideoRecord,
)

__all__ = [
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
    "RevenueRepository",
    "QuotaPoolRepository",
    "AcquisitionRepository",
    "TranscriptionRepository",
    "UnitOfWork",
    "Database",
]

R = TypeVar("R")


@runtime_checkable
class Repository(Protocol[R]):
    """The operations every table supports.

    Reads are always tenant-scoped. Not by convention — the session sets
    `app.tenant_id` and row-level security ANDs it into the query, so a
    repository *cannot* return another tenant's row even if the SQL forgets to
    filter. The in-memory implementation filters explicitly to match.
    """

    def get(self, key: str) -> R | None:
        """The record, or None. Never raises for a missing row."""

    def require(self, key: str) -> R:
        """The record, or `NotFound`."""

    def add(self, record: R) -> R:
        """Insert. Raises `Conflict` if the key or a unique index already holds."""

    def save(self, record: R) -> R:
        """Insert or update. The idempotent write."""

    def delete(self, key: str) -> bool:
        """True if a row went away."""

    def all(self) -> tuple[R, ...]:
        """Every row for this tenant. For small tables and for tests."""

    def count(self) -> int: ...


class TenantRepository(Repository[TenantRecord], Protocol):
    pass


class UserRepository(Repository[UserRecord], Protocol):
    def by_email(self, email: str) -> UserRecord | None: ...
    def active(self) -> tuple[UserRecord, ...]: ...


class ProjectRepository(Repository[ProjectRecord], Protocol):
    def live(self) -> tuple[ProjectRecord, ...]:
        """Unarchived projects."""


class ChannelRepository(Repository[ChannelRecord], Protocol):
    def for_project(self, project_id: str) -> tuple[ChannelRecord, ...]: ...
    def in_state(self, *states: str) -> tuple[ChannelRecord, ...]: ...


class SocialAccountRepository(Repository[SocialAccountRecord], Protocol):
    def for_channel(self, channel_id: str) -> tuple[SocialAccountRecord, ...]: ...
    def on_platform(self, platform: str) -> tuple[SocialAccountRecord, ...]: ...

    def refresh_expiring_before(
        self, moment: datetime
    ) -> tuple[SocialAccountRecord, ...]:
        """Accounts whose refresh token dies before `moment`.

        A refresh token that lapses unnoticed is a channel that silently stops
        posting, and the customer finds out before the operator does.
        """


class SourceRepository(Repository[SourceRecord], Protocol):
    def by_fingerprint(self, fingerprint: str) -> SourceRecord | None:
        """The de-duplication lookup: has this material been ingested already?"""

    def rights_expiring_before(self, moment: datetime) -> tuple[SourceRecord, ...]: ...

    def mark_used(self, channel_id: str, source_id: str, used_at: datetime) -> None:
        """Record that a channel has clipped a source. Idempotent."""

    def used_by(self, channel_id: str) -> tuple[ChannelSourceUseRecord, ...]: ...

    def unused_by(self, channel_id: str) -> tuple[SourceRecord, ...]:
        """Sources this channel has not clipped yet."""


class VideoRepository(Repository[VideoRecord], Protocol):
    def for_clip(self, clip_id: str) -> tuple[VideoRecord, ...]: ...
    def in_state(self, *states: str) -> tuple[VideoRecord, ...]: ...


class ClipRepository(Repository[ClipRecord], Protocol):
    def for_channel(self, channel_id: str, limit: int = 0) -> tuple[ClipRecord, ...]: ...
    def for_source(self, source_id: str) -> tuple[ClipRecord, ...]: ...


class ScheduleRepository(Repository[ScheduleRecord], Protocol):
    def enabled_for_channel(self, channel_id: str) -> tuple[ScheduleRecord, ...]: ...


class UploadRepository(Repository[UploadRecord], Protocol):
    def by_idempotency_key(self, key: str) -> UploadRecord | None: ...

    def due(self, now: datetime, limit: int = 100) -> tuple[UploadRecord, ...]:
        """Scheduled posts whose time has come, soonest first."""

    def for_account_between(
        self, account_id: str, start: datetime, end: datetime
    ) -> tuple[UploadRecord, ...]:
        """The calendar's spacing and daily-cap query.

        Index-backed on `(tenant_id, account_id, run_at)`. This used to be a
        linear scan of an in-memory list, which is what made scheduling
        quadratic at empire scale.
        """

    def for_channel_between(
        self, channel_id: str, start: datetime, end: datetime
    ) -> tuple[UploadRecord, ...]: ...

    def in_state(self, *states: str) -> tuple[UploadRecord, ...]: ...


class MetricRepository(Protocol):
    """Append-only. No `save`, no `delete` — the app role has no privilege for
    either, and offering the method would only produce a runtime error further
    from the mistake."""

    def append(self, record: MetricSnapshotRecord) -> MetricSnapshotRecord:
        """Store a reading. `Conflict` if this post already has one at this age."""

    def for_upload(self, upload_id: str) -> tuple[MetricSnapshotRecord, ...]:
        """Every reading for a post, oldest first."""

    def latest_for_upload(self, upload_id: str) -> MetricSnapshotRecord | None: ...

    def between(
        self, start: datetime, end: datetime
    ) -> tuple[MetricSnapshotRecord, ...]: ...

    def at_age(
        self, age_hours: float, tolerance: float = 0.5
    ) -> tuple[MetricSnapshotRecord, ...]:
        """Readings taken at a comparable age.

        Matched-age is not a nicety: a post measured at 48h and one measured
        at 2h are not comparable, and comparing them is how an analytics engine
        concludes that posting at 3am is brilliant.
        """


class JobRepository(Protocol):
    """The durable work queue.

    In the database rather than in Redis because these rows are the record of
    what the system owes its customers. A render that vanishes on restart is a
    clip that silently never posts, and nobody notices until a creator asks
    why their Tuesday is empty.
    """

    def get(self, key: str) -> JobRecord | None: ...

    def enqueue(self, record: JobRecord) -> JobRecord:
        """Add work. A repeat of an existing `dedupe_key` returns the row that
        is already queued rather than queueing a second copy."""

    def claim(
        self,
        owner: str,
        now: datetime,
        lease_s: int = 300,
        kinds: tuple[str, ...] = (),
        limit: int = 1,
    ) -> tuple[JobRecord, ...]:
        """Take work, under a lease.

        A lease rather than a lock: a worker killed mid-job cannot release a
        lock, and a lock nobody releases is a queue that stops. A lease simply
        expires, and `reap` puts the job back.
        """

    def heartbeat(self, job_id: str, owner: str, until: datetime) -> bool:
        """Extend a lease. False if the lease was already lost to a reaper —
        which is the signal for the worker to abandon the job rather than
        finish work a second worker has already started."""

    def succeed(self, job_id: str, result: dict | None, now: datetime) -> JobRecord: ...

    def fail(self, job_id: str, error: str, retry_at: datetime | None,
             now: datetime) -> JobRecord:
        """Record a failure. Past `max_attempts` the job goes `dead` rather
        than retrying forever."""

    def reap(self, now: datetime, limit: int = 100) -> int:
        """Return expired leases to the queue. The count returned."""

    def pending(self, limit: int = 100) -> tuple[JobRecord, ...]: ...
    def in_state(self, *states: str) -> tuple[JobRecord, ...]: ...


class TranscriptionRepository(Repository["TranscriptionRunRecord"], Protocol):
    """Transcription runs: queued, running, finished and failed."""

    def for_source(self, source_id: str) -> Any:
        """The run for this source, or None. One per source by constraint."""

    def in_state(self, *states: str) -> tuple[Any, ...]: ...


class AcquisitionRepository(Repository["AcquisitionRunRecord"], Protocol):
    """The record of acquisitions: running, finished and failed."""

    def for_ref(self, kind: str, ref_key: str, channel_id: str | None) -> Any:
        """The existing run for this reference on this channel, or None."""

    def in_state(self, *states: str) -> tuple[Any, ...]: ...
    def for_source(self, source_id: str) -> tuple[Any, ...]: ...


class RevenueRepository(Repository["RevenueEntryRecord"], Protocol):
    def for_period(self, period: str) -> tuple[Any, ...]:
        """Every brand's entry for one month."""

    def for_project(self, project_id: str, period: str) -> Any:
        """One brand's entry for one month, or None."""


class QuotaPoolRepository(Repository["QuotaPoolRecord"], Protocol):
    def on_platform(self, platform: str) -> tuple[Any, ...]: ...


class UnitOfWork(Protocol):
    """One transaction, one tenant.

    The tenant is fixed for the life of the unit of work and pushed into the
    database session as `SET LOCAL app.tenant_id`. `SET LOCAL` is what makes
    this safe behind a connection pool: it dies with the transaction, so a
    pooled connection cannot carry one customer's tenant into the next
    customer's query.

    Used as a context manager. Leaving the block normally commits; leaving it
    by exception rolls back. There is no `commit()` to forget.
    """

    tenant_id: str

    tenants: TenantRepository
    users: UserRepository
    projects: ProjectRepository
    channels: ChannelRepository
    accounts: SocialAccountRepository
    sources: SourceRepository
    videos: VideoRepository
    clips: ClipRepository
    schedules: ScheduleRepository
    uploads: UploadRepository
    metrics: MetricRepository
    jobs: JobRepository
    revenue: RevenueRepository
    pools: QuotaPoolRepository
    acquisitions: AcquisitionRepository
    transcriptions: TranscriptionRepository

    def __enter__(self) -> UnitOfWork: ...
    def __exit__(self, *exc: object) -> bool: ...

    def rollback(self) -> None:
        """Abandon the work. The block will not commit on exit."""


class Database(Protocol):
    """Opens units of work. The thing the application holds."""

    def unit_of_work(self, tenant_id: str) -> UnitOfWork: ...
    def close(self) -> None: ...
