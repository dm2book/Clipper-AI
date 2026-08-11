"""Persistence records — one dataclass per table.

These are deliberately *not* the domain objects. A `ScheduledPost` knows how to
back off after a rate-limit; an `UploadRecord` knows what a row of `uploads`
looks like. Keeping them apart means the schema can gain a column without
touching the publishing engine, and the engine can gain a computed property
without a migration.

Field names match column names exactly, which is what lets the mapping in
`schema.py` stay mechanical instead of being a hand-maintained list of
correspondences that drifts.

Two invariants every record upholds:

* every datetime is timezone-aware — `ensure_utc` refuses naive ones, because
  a naive datetime in a system that schedules across IANA zones is a post at
  the wrong hour twice a year;
* `tenant_id` is always populated, because it is the column every row-level
  security policy is a predicate over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..publish.types import utcnow

__all__ = [
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
]


@dataclass
class Record:
    """Base for every persisted row.

    `created_at` and `updated_at` are set by the database — a default and a
    trigger, respectively — so the values here are placeholders until a row is
    read back. They are not authoritative in memory and nothing should compare
    against them before a round-trip.
    """

    id: str = ""
    tenant_id: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


@dataclass
class TenantRecord(Record):
    name: str = ""
    plan: str = "starter"
    suspended: bool = False

    def __post_init__(self) -> None:
        # The one table keyed by `id` rather than `tenant_id`. Mirroring the
        # value keeps every repository able to read `record.tenant_id` without
        # a special case, and keeps the RLS scope check uniform.
        if not self.tenant_id:
            self.tenant_id = self.id


@dataclass
class UserRecord(Record):
    email: str = ""
    name: str = ""
    role: str = "viewer"
    active: bool = True
    project_ids: list[str] = field(default_factory=list)


@dataclass
class ProjectRecord(Record):
    """A brand or portfolio. The domain layer calls this a Brand."""

    name: str = ""
    timezone: str = "UTC"
    budget_cents: int = 0
    archived: bool = False


# ---------------------------------------------------------------------------
# Channels and connected accounts
# ---------------------------------------------------------------------------


@dataclass
class ChannelRecord(Record):
    project_id: str = ""
    name: str = ""
    niche: str = ""
    state: str = "draft"
    timezone: str = "UTC"
    topics: list[str] = field(default_factory=list)
    accepted_rights: list[str] = field(default_factory=list)
    monetised: bool = True
    cadence_override: int = 0
    quality_floor_override: float = 0.0
    budget_monthly_cents: int = 20_000
    budget_spent_cents: int = 0
    budget_period: str = ""
    consecutive_failures: int = 0
    circuit_opened_at: datetime | None = None
    last_error: str = ""
    total_items: int = 0
    total_published: int = 0
    total_blocked: int = 0
    total_failed: int = 0


@dataclass
class SocialAccountRecord(Record):
    channel_id: str | None = None
    platform: str = "tiktok"
    handle: str = ""
    external_id: str = ""
    timezone: str = "UTC"
    direct_post_approved: bool = False
    business_account: bool = False
    enabled: bool = True
    #: Ciphertext. The plaintext never reaches this record — see `SealedTokenStore`.
    access_token_sealed: str = ""
    refresh_token_sealed: str = ""
    token_expires_at: datetime | None = None
    refresh_valid_until: datetime | None = None
    token_obtained_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


@dataclass
class SourceRecord(Record):
    title: str = ""
    kind: str = ""
    url: str = ""
    creator: str = ""
    language: str = "en"
    topics: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    has_transcript: bool = False
    published_at: datetime | None = None
    fingerprint: str = ""
    rights_basis: str = "unverified"
    rights_reference: str = ""
    rights_attribution: str = ""
    commercial_use: bool = True
    derivatives: bool = True
    rights_verified_at: datetime | None = None
    rights_expires_at: datetime | None = None


@dataclass
class ChannelSourceUseRecord:
    """Which channel has already clipped which source.

    Not a `Record`: the primary key is the (channel, source) pair, there is no
    surrogate id, and nothing ever updates a row — it is written once when a
    source is consumed and read to keep the same material from being clipped
    twice.
    """

    tenant_id: str = ""
    channel_id: str = ""
    source_id: str = ""
    used_at: datetime = field(default_factory=utcnow)


@dataclass
class VideoRecord(Record):
    clip_id: str | None = None
    source_id: str | None = None
    state: str = "pending"
    storage_key: str = ""
    public_url: str = ""
    checksum: str = ""
    size_bytes: int = 0
    duration_s: float = 0.0
    width: int = 1080
    height: int = 1920
    fps: int = 60
    render_plan: dict[str, Any] | None = None
    render_error: str = ""
    rendered_at: datetime | None = None


@dataclass
class ClipRecord(Record):
    channel_id: str = ""
    source_id: str | None = None
    start_ms: int = 0
    end_ms: int = 0
    duration_s: float = 0.0
    title: str = ""
    transcript: str = ""
    virality_score: float = 0.0
    scores: dict[str, Any] | None = None
    features: dict[str, Any] | None = None
    signals: list[str] = field(default_factory=list)
    weights_version: str = ""
    hook_text: str = ""
    hook_type: str = ""
    hook_rank: int = 0
    hook_explored: bool = False
    predicted_lift: float = 0.0
    #: Every generated hook, not only the published one.
    hook_candidates: list[dict[str, Any]] | None = None
    caption_track: dict[str, Any] | None = None
    topic: str = ""


# ---------------------------------------------------------------------------
# Scheduling and publishing
# ---------------------------------------------------------------------------


@dataclass
class ScheduleRecord(Record):
    channel_id: str = ""
    frequency: str = "daily"
    timezone: str = "UTC"
    #: "17:00" strings, in `timezone`. Never a UTC cron.
    times_local: list[str] = field(default_factory=list)
    weekdays: list[int] = field(default_factory=list)
    month_days: list[int] = field(default_factory=list)
    interval: int = 1
    starts_on: datetime | None = None
    ends_on: datetime | None = None
    max_occurrences: int = 0
    nonexistent_time_policy: str = "shift"
    ambiguous_time_policy: str = "first"
    enabled: bool = True


@dataclass
class UploadRecord(Record):
    channel_id: str = ""
    account_id: str = ""
    clip_id: str | None = None
    video_id: str | None = None
    schedule_id: str | None = None
    platform: str = "tiktok"
    state: str = "scheduled"
    run_at: datetime = field(default_factory=utcnow)
    next_attempt_at: datetime | None = None
    lease_until: datetime | None = None
    lease_owner: str = ""
    title: str = ""
    caption: str = ""
    visibility: str = "public"
    metadata: dict[str, Any] | None = None
    #: Derived from account + asset + slot. Unique per tenant, which is what
    #: makes a double publish a constraint violation rather than a duplicate
    #: on someone's feed.
    idempotency_key: str = ""
    remote_post_id: str = ""
    attempt_count: int = 0
    attempts: list[dict[str, Any]] | None = None
    last_error: str = ""
    published_at: datetime | None = None


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@dataclass
class MetricSnapshotRecord(Record):
    """One reading of a post's counters at a known age. Append-only.

    The append-only rule is a database privilege, not a convention here: the
    app role holds no UPDATE or DELETE on this table. Matched-age comparison
    is the basis of every finding the analytics engine reports, and a snapshot
    rewritten after the fact cannot be recovered.
    """

    upload_id: str = ""
    taken_at: datetime = field(default_factory=utcnow)
    age_hours: float = 0.0
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    follows: int = 0
    impressions: int = 0
    watch_time_s: float = 0.0
    avg_watch_pct: float = 0.0
    #: Only YouTube reports one. Null rather than an imputed curve.
    retention_curve: list[float] | None = None

    # No `updated_at`: the table has no such column, precisely because nothing
    # updates it. `Record.updated_at` stays at its placeholder and is excluded
    # from the column list in `schema.py`.


# ---------------------------------------------------------------------------
# Work queue
# ---------------------------------------------------------------------------


@dataclass
class JobRecord(Record):
    channel_id: str | None = None
    kind: str = "render_video"
    state: str = "queued"
    priority: int = 100
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    run_after: datetime = field(default_factory=utcnow)
    lease_until: datetime | None = None
    lease_owner: str = ""
    attempts: int = 0
    max_attempts: int = 8
    last_error: str = ""
    #: Set by the producer so re-enqueueing the same logical work is a no-op
    #: rather than a second render of the same clip.
    dedupe_key: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# Empire: revenue and API allowance
# ---------------------------------------------------------------------------


@dataclass
class RevenueEntryRecord(Record):
    """Non-ad revenue for a brand, in cents, for one month.

    Supplied by an operator rather than measured — sponsorship and affiliate
    income arrives through channels this system cannot see, and inventing a
    number would be worse than showing zero. Which is precisely why it must
    survive a restart: it was typed in by a person, and a dashboard whose
    revenue silently returns to zero after a deploy is worse than one that
    never claimed to know.
    """

    project_id: str = ""
    #: "2026-04". Not a date — these are monthly totals, and a date would
    #: invite a reader to believe there is day-level detail behind them.
    period: str = ""
    sponsorship_cents: int = 0
    affiliate_cents: int = 0
    own_product_cents: int = 0
    services_cents: int = 0


@dataclass
class QuotaPoolRecord(Record):
    """One source of API allowance for one platform.

    YouTube's quota is scoped to the API *project*, not the account, so the
    whole installation shares one ceiling however many channels it runs. That
    ceiling is configuration an operator applied for and was granted; losing it
    on restart silently reverts every channel to the default allowance.
    """

    platform: str = "youtube"
    ownership: str = "shared_app"
    #: Zero means the platform's standard allowance.
    daily_units: int = 0


# ---------------------------------------------------------------------------
# Source acquisition
# ---------------------------------------------------------------------------


@dataclass
class AcquisitionRunRecord(Record):
    """One attempt to turn something an operator pasted into material.

    Separate from `SourceRecord` because the two answer different questions.
    A source is material the factory may clip; an acquisition is the *process*
    of getting it — including the ones that failed and the ones still running.
    Folding them together would put half-downloaded rows in the library.

    This row is also what makes a download resumable across a restart. The
    bytes live in `<media_path>.part` and that file's length is the resume
    offset, but `validator` — the `ETag` or `Last-Modified` — lives here, and
    resuming without it splices the tail of a new encode onto the head of an
    old one.
    """

    source_id: str | None = None
    channel_id: str | None = None
    kind: str = "media_url"
    state: str = "queued"
    #: The normalised reference: a bare video id, a feed item GUID, a path.
    ref_key: str = ""
    ref_raw: str = ""
    url: str = ""
    title: str = ""
    creator: str = ""
    external_id: str = ""
    published_at: datetime | None = None
    media_path: str = ""
    bytes_done: int = 0
    #: None when the server sent no Content-Length. Unknowable, not zero.
    bytes_total: int | None = None
    validator: str = ""
    content_type: str = ""
    checksum: str = ""
    resumable: bool = False
    #: None until measured. Zero is a number the clip detector divides by.
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False
    has_video: bool = False
    prober: str = ""
    thumbnail_path: str = ""
    thumbnail_origin: str = ""
    metadata: dict[str, Any] | None = None
    attempts: int = 0
    last_error: str = ""
    finished_at: datetime | None = None
