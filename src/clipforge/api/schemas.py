"""The wire contract.

These models are the API's promise, and they are deliberately *not* the store
records. A record is an internal shape that changes when the schema changes; a
response model changes only when the contract does. Returning records directly
means a column rename is a breaking change for every client, and a column
added — `password_hash`, say — is a leak nobody reviewed.

Every field a client needs is named here explicitly. That is the point.

The TypeScript in `web/src/api/types.ts` is generated from the OpenAPI document
these models produce, so a field renamed here fails the dashboard's type check
rather than showing up as `undefined` in a table cell.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A slice of a list, with enough to build a pager.

    `total` is the count *before* the limit, so a table can say "showing 20 of
    413" — the number people actually want, and the one an endpoint returning
    a bare list can never provide.
    """

    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class ErrorBody(BaseModel):
    code: str
    message: str
    retry_after_s: float | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str
    #: Which workspace to open. Required only when the identity belongs to
    #: more than one; the API says so with a 409 rather than guessing.
    tenant_id: str = ""


class MembershipOut(BaseModel):
    user_id: str
    tenant_id: str
    tenant_name: str = ""
    role: str
    active: bool


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    session_id: str
    tenant_id: str = ""


class MfaChallengeOut(BaseModel):
    """Returned instead of tokens when a second factor is owed."""

    challenge_token: str
    expires_at: datetime
    kinds: list[str] = Field(default_factory=list)
    recovery_available: bool = False


class LoginResponse(BaseModel):
    """A completed sign-in, or a challenge. Never both.

    `tokens` is null when `mfa` is set. Modelled as one nullable field rather
    than two response shapes so a client that ignores MFA gets an obvious null
    where it wanted a token, instead of a 200 it misreads as success.
    """

    tokens: TokenPairOut | None = None
    memberships: list[MembershipOut] = Field(default_factory=list)
    #: True when the address is registered but unconfirmed. The UI nags.
    unverified: bool = False
    mfa: MfaChallengeOut | None = None
    #: The double-submit CSRF token, also set as a readable cookie. In the body
    #: too so a client need not parse cookies to find it.
    csrf_token: str = ""


class MfaVerifyRequest(BaseModel):
    challenge_token: str
    #: A TOTP code or a recovery code. The server decides which.
    code: str
    tenant_id: str = ""


class MfaEnrolRequest(BaseModel):
    label: str = ""


class MfaEnrolResponse(BaseModel):
    """Shown once. The secret is never readable again."""

    factor_id: str
    secret: str
    otpauth_uri: str


class MfaConfirmRequest(BaseModel):
    factor_id: str
    code: str


class MfaConfirmResponse(BaseModel):
    #: Displayed once and then unrecoverable — they are stored hashed.
    recovery_codes: list[str]
    message: str


class MfaFactorOut(BaseModel):
    factor_id: str
    kind: str
    label: str = ""
    active: bool
    created_at: datetime
    last_used_at: datetime | None = None


class MfaStatusOut(BaseModel):
    enabled: bool
    factors: list[MfaFactorOut] = Field(default_factory=list)
    recovery_codes_remaining: int = 0


class DeviceOut(BaseModel):
    device_id: str
    label: str = ""
    user_agent: str = ""
    last_ip: str = ""
    first_seen_at: datetime
    last_seen_at: datetime
    active: bool
    #: True for the device making this request, so the UI can say "this one".
    current: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


class SignUpRequest(BaseModel):
    email: str
    password: str


class EmailRequest(BaseModel):
    """An address and nothing else.

    Reset and resend used to borrow `SignUpRequest`, which made a caller invent
    a password for a form that has none — and made the generated client types
    describe a password field on a request that must never carry one.
    """

    email: str


class VerifyEmailRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    token: str
    new_password: str


class DeleteAccountRequest(BaseModel):
    """Deleting an account asks for the password again.

    Not ceremony. An unattended session is the ordinary way this endpoint is
    reached by someone who should not have it, and it is the only action here
    with no undo once the grace period expires.
    """

    password: str


class MessageResponse(BaseModel):
    """Used where the answer must not depend on whether an account exists."""

    message: str


class MeResponse(BaseModel):
    identity_id: str
    email: str
    tenant_id: str
    user_id: str
    role: str
    session_id: str
    expires_at: datetime
    memberships: list[MembershipOut]


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class StatOut(BaseModel):
    """One headline number, with what it means attached.

    `detail` exists because a bare count is unreadable: "14 sources" invites
    the question "is that good?", and "9 with transcripts" answers it without
    a second request.
    """

    key: str
    label: str
    value: float
    detail: str = ""
    unit: str = ""


class ActivityOut(BaseModel):
    at: datetime
    kind: str
    summary: str
    state: str = ""
    reference: str = ""


class PipelineStageOut(BaseModel):
    """One stage of acquisition → transcription → clips → uploads."""

    stage: str
    label: str
    total: int
    done: int
    failed: int
    in_flight: int


class OverviewResponse(BaseModel):
    tenant_id: str
    stats: list[StatOut]
    pipeline: list[PipelineStageOut]
    activity: list[ActivityOut]
    #: Things needing a human: circuit breakers, dead jobs, expiring rights.
    attention: list[str]
    generated_at: datetime


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class ChannelOut(BaseModel):
    id: str
    name: str
    niche: str
    state: str
    timezone: str
    topics: list[str]
    monetised: bool
    budget_monthly_cents: int
    budget_spent_cents: int
    budget_remaining_cents: int
    consecutive_failures: int
    circuit_opened_at: datetime | None = None
    last_error: str = ""
    total_items: int
    total_published: int
    total_blocked: int
    total_failed: int
    created_at: datetime


class ChannelStateUpdate(BaseModel):
    state: str = Field(description="draft, active or paused")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class SourceOut(BaseModel):
    id: str
    title: str
    kind: str
    url: str = ""
    creator: str = ""
    language: str = "en"
    topics: list[str]
    duration_s: float
    has_transcript: bool
    rights_basis: str
    rights_expires_at: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime
    #: From `acquisition_runs`, when there is one.
    acquisition_state: str = ""
    media_path: str = ""
    #: From `transcription_runs`, when there is one.
    transcription_state: str = ""
    word_count: int = 0


class SubmitSourceRequest(BaseModel):
    url: str
    channel_id: str = ""


# ---------------------------------------------------------------------------
# Upload queue and published videos
# ---------------------------------------------------------------------------


class UploadOut(BaseModel):
    id: str
    channel_id: str
    channel_name: str = ""
    account_id: str
    platform: str
    state: str
    title: str = ""
    caption: str = ""
    visibility: str
    run_at: datetime
    next_attempt_at: datetime | None = None
    attempt_count: int
    last_error: str = ""
    remote_post_id: str = ""
    published_at: datetime | None = None
    clip_id: str | None = None


class PublishedVideoOut(BaseModel):
    upload_id: str
    channel_id: str
    channel_name: str = ""
    platform: str
    title: str = ""
    remote_post_id: str
    published_at: datetime | None = None
    permalink: str = ""
    #: Latest snapshot, when analytics has collected one. Null is honest:
    #: nothing has been collected, which is different from zero views.
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    avg_watch_pct: float | None = None
    measured_at: datetime | None = None
    age_hours: float | None = None


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


class SeriesPointOut(BaseModel):
    at: datetime
    value: float


class MetricSeriesOut(BaseModel):
    key: str
    label: str
    unit: str = ""
    points: list[SeriesPointOut]


class PlatformBreakdownOut(BaseModel):
    platform: str
    posts: int
    views: int
    likes: int
    avg_watch_pct: float | None = None


class AnalyticsResponse(BaseModel):
    #: Explicit, because every number below is scoped to it.
    window_days: int
    posts_measured: int
    #: Null when nothing has been collected. Zero would claim a real reading.
    total_views: int | None = None
    total_likes: int | None = None
    avg_watch_pct: float | None = None
    series: list[MetricSeriesOut]
    by_platform: list[PlatformBreakdownOut]
    top: list[PublishedVideoOut]
    #: Set when there is no measurement at all, so the UI can say why rather
    #: than drawing an empty chart.
    note: str = ""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SessionOut(BaseModel):
    session_id: str
    ip: str = ""
    user_agent: str = ""
    issued_at: datetime
    expires_at: datetime | None = None
    revoked: bool
    rotations: int
    current: bool = False


class SocialAccountOut(BaseModel):
    id: str
    platform: str
    handle: str = ""
    channel_id: str | None = None
    connected: bool
    needs_reauth: bool = False
    detail: str = ""


class CapabilityOut(BaseModel):
    """What this deployment can actually do.

    Reported rather than assumed, because most of the honest answers are
    negative: no object storage, no live metric source, no email transport.
    A dashboard that hides them shows an upload queue that will never drain
    and gives no clue why.
    """

    key: str
    label: str
    available: bool
    detail: str = ""


class StorageUsageOut(BaseModel):
    """What this workspace is storing, and what the backend is doing."""

    backend: str
    #: Null when the backend cannot answer cheaply or is not configured.
    objects: int | None = None
    bytes: int | None = None
    gigabytes: float | None = None
    largest_key: str = ""
    largest_bytes: int | None = None
    #: Per-operation counters since the process started. Reset on restart,
    #: which is right for what they are — an exporter scrapes them.
    operations: dict[str, dict[str, float]] = Field(default_factory=dict)
    total_calls: int = 0
    total_failures: int = 0
    total_retries: int = 0
    #: Set when usage could not be measured, so the UI says why rather than
    #: rendering a zero that reads as "you are storing nothing".
    note: str = ""


class SettingsResponse(BaseModel):
    identity_id: str
    email: str
    verified: bool
    tenant_id: str
    role: str
    memberships: list[MembershipOut]
    sessions: list[SessionOut]
    accounts: list[SocialAccountOut]
    capabilities: list[CapabilityOut]


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
