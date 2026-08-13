"""Core types for scheduled multi-platform publishing.

Everything here is timezone-aware and UTC-normalised at the boundary. A
publishing system whose internal clock is naive will work for months and then
post an entire back catalogue an hour early on the last Sunday in October.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


UTC = timezone.utc


class Platform(str, enum.Enum):
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"


class PostState(str, enum.Enum):
    """Lifecycle of one scheduled post.

    `PROCESSING` is distinct from `UPLOADING` because on all three platforms
    the bytes landing is not the post existing: each transcodes asynchronously
    and can reject the video after accepting every byte of it.

    `NEEDS_ATTENTION` is distinct from `FAILED` because they need different
    people. `FAILED` is the system's problem; `NEEDS_ATTENTION` means a human
    has to reconnect an account or edit a caption, and no amount of retrying
    will help.
    """

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    PUBLISHED = "published"
    #: The platform accepted the file but the post is **not live** — TikTok's
    #: unaudited path drops a draft in the creator's inbox for a human to
    #: finish. Calling that `PUBLISHED` would put a green tick on a calendar
    #: next to something nobody can watch, which is the one lie a publishing
    #: tool must not tell.
    AWAITING_CREATOR = "awaiting_creator"
    RETRYING = "retrying"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


#: States from which no further work will be attempted.
TERMINAL_STATES = frozenset({
    PostState.PUBLISHED, PostState.AWAITING_CREATOR, PostState.FAILED,
    PostState.CANCELLED, PostState.NEEDS_ATTENTION,
})

#: States where the file reached the platform, whether or not it is live.
DELIVERED_STATES = frozenset({
    PostState.PUBLISHED, PostState.AWAITING_CREATOR,
})

#: States where the platform has been told to create something. A post in one
#: of these must be **reconciled** rather than blindly retried — see
#: `retry.Disposition.RECONCILE`.
IN_FLIGHT_STATES = frozenset({
    PostState.UPLOADING, PostState.PROCESSING,
})


class Visibility(str, enum.Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNLISTED = "unlisted"        # YouTube only
    FOLLOWERS = "followers"      # TikTok only


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """The rendered file to be published.

    `public_url` is not optional for Instagram. The Graph API does not accept
    uploaded bytes for Reels — it *pulls* the file from a URL you provide, and
    that URL has to stay reachable for the whole of the platform's transcode.
    A signed URL that expires in five minutes is the classic way to discover
    this in production rather than in staging.
    """

    asset_id: str
    path: str = ""
    public_url: str = ""
    size_bytes: int = 0
    duration_s: float = 0.0
    width: int = 1080
    height: int = 1920
    fps: int = 60
    checksum: str = ""

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


@dataclass(frozen=True, slots=True)
class Account:
    """One connected social account.

    A user may hold several per platform — the multi-account requirement — and
    each carries its own tokens, its own quota counters and its own audit
    status. Quota is per *account* on TikTok and Instagram but per *API
    project* on YouTube, which is why `limits.py` tracks both.
    """

    account_id: str
    platform: Platform
    org_id: str
    handle: str = ""
    #: Platform's own identifier: channel id, open_id, ig user id.
    external_id: str = ""
    timezone: str = "UTC"
    #: False until the platform has approved the app for unattended posting.
    #: See `limits.automation_gap` — this is the field that decides whether
    #: "fully automated" is actually available.
    direct_post_approved: bool = False
    #: Instagram only: Reels publishing requires a Business or Creator account
    #: linked to a Facebook Page. Personal accounts cannot publish via API.
    business_account: bool = False
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PostSpec:
    """What to publish, independent of when.

    Captions differ per platform by necessity, not by preference: TikTok's
    limit is far shorter than YouTube's description, hashtag conventions
    differ, and @-mentions do not port. `caption_for` falls back so a caller
    can supply one and get sensible behaviour everywhere.
    """

    asset: MediaAsset
    title: str = ""
    caption: str = ""
    hashtags: tuple[str, ...] = ()
    per_platform_caption: dict[str, str] = field(default_factory=dict)
    visibility: Visibility = Visibility.PUBLIC
    #: YouTube category id; 22 is "People & Blogs".
    category_id: str = "22"
    made_for_kids: bool = False
    #: Free-form, persisted with the post for the caller's own bookkeeping.
    metadata: dict[str, Any] = field(default_factory=dict)

    def caption_for(self, platform: Platform) -> str:
        text = self.per_platform_caption.get(platform.value, self.caption)
        if self.hashtags:
            tags = " ".join(
                tag if tag.startswith("#") else f"#{tag}" for tag in self.hashtags
            )
            text = f"{text}\n\n{tags}".strip()
        return text


@dataclass(slots=True)
class Attempt:
    """One try at publishing, kept forever.

    The history is the audit trail. When a post lands twice, the only way to
    find out why is to see every request the system believed it was making.
    """

    number: int
    started_at: datetime
    finished_at: datetime | None = None
    state: PostState = PostState.UPLOADING
    error_code: str = ""
    error_message: str = ""
    disposition: str = ""
    #: Platform-side handle: publish_id, upload session URI, container id.
    remote_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "started_at": self.started_at.isoformat(),
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "state": self.state.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "disposition": self.disposition,
            "remote_ref": self.remote_ref,
        }


@dataclass(slots=True)
class ScheduledPost:
    """One post to one account at one time."""

    post_id: str
    account_id: str
    platform: Platform
    spec: PostSpec
    #: Always UTC. Local time is a presentation concern; storing it is how a
    #: system ends up posting at the wrong hour twice a year.
    run_at: datetime
    state: PostState = PostState.SCHEDULED
    attempts: list[Attempt] = field(default_factory=list)
    #: Set once the platform confirms. Presence of this is the *only* proof a
    #: post exists, and the reason a retry must reconcile first.
    remote_post_id: str = ""
    #: Stable across retries, so a platform that supports it can deduplicate
    #: server-side and so reconciliation has something to match on.
    idempotency_key: str = ""
    #: Which recurring rule produced this, if any.
    series_id: str = ""
    lease_until: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error: str = ""

    def __post_init__(self) -> None:
        if self.run_at.tzinfo is None:
            raise ValueError("run_at must be timezone-aware")
        self.run_at = self.run_at.astimezone(UTC)
        if not self.idempotency_key:
            self.idempotency_key = make_idempotency_key(
                self.account_id, self.spec.asset.asset_id, self.run_at
            )

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def in_flight(self) -> bool:
        return self.state in IN_FLIGHT_STATES

    def local_time(self, tz_name: str) -> datetime:
        from zoneinfo import ZoneInfo

        return self.run_at.astimezone(ZoneInfo(tz_name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "account_id": self.account_id,
            "platform": self.platform.value,
            "run_at": self.run_at.isoformat(),
            "state": self.state.value,
            "title": self.spec.title,
            "asset_id": self.spec.asset.asset_id,
            "attempts": [a.to_dict() for a in self.attempts],
            "remote_post_id": self.remote_post_id,
            "idempotency_key": self.idempotency_key,
            "series_id": self.series_id,
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else None
            ),
            "last_error": self.last_error,
        }


def make_idempotency_key(
    account_id: str, asset_id: str, run_at: datetime
) -> str:
    """A stable key for one logical post.

    Derived rather than random so that the *same* logical post recreated after
    a crash — same account, same asset, same slot — produces the same key and
    can be recognised as the post that may already have landed.
    """
    raw = f"{account_id}|{asset_id}|{run_at.astimezone(UTC).isoformat()}"
    return hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()


@dataclass(frozen=True, slots=True)
class Request:
    """An outbound HTTP call, built but not made.

    Adapters are request builders and response interpreters, not HTTP clients.
    That keeps every platform state machine exercisable offline — the same
    reason the gameplay engine emits a filtergraph rather than shelling out to
    ffmpeg.
    """

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    form_body: dict[str, str] | None = None
    #: Byte range of the asset to send, for chunked uploads.
    byte_range: tuple[int, int] | None = None
    #: Where the bytes named by `byte_range` live. Carried on the request
    #: rather than handed to the transport separately, so the transport stays
    #: stateless and one client can serve every worker. Empty on every request
    #: that sends no media, which is most of them.
    asset_path: str = ""
    description: str = ""

    def redacted(self) -> dict[str, Any]:
        """The request with credentials removed, for logs."""
        safe = {
            key: ("<redacted>" if key.lower() in _SECRET_HEADERS else value)
            for key, value in self.headers.items()
        }
        body = dict(self.json_body or self.form_body or {})
        for key in list(body):
            if key.lower() in _SECRET_FIELDS:
                body[key] = "<redacted>"
        return {
            "method": self.method,
            "url": self.url,
            "headers": safe,
            "body": body or None,
            "byte_range": list(self.byte_range) if self.byte_range else None,
            "description": self.description,
        }


_SECRET_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})
_SECRET_FIELDS = frozenset({
    "access_token", "refresh_token", "client_secret", "code_verifier", "code",
})


@dataclass(frozen=True, slots=True)
class Response:
    """A platform's reply, normalised."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Action(str, enum.Enum):
    """What the adapter wants to happen next."""

    REQUEST = "request"     # make this call
    WAIT = "wait"           # platform is processing; poll again later
    DONE = "done"           # published
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Step:
    """One move in a platform's upload state machine."""

    action: Action
    request: Request | None = None
    wait_s: float = 0.0
    remote_post_id: str = ""
    error_code: str = ""
    error_message: str = ""
    #: Opaque adapter state carried between steps.
    context: dict[str, Any] = field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime — every time in this system is aware")
    return moment.astimezone(UTC)


def humanise(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if abs(seconds) < 60:
        return f"{seconds}s"
    if abs(seconds) < 3600:
        return f"{seconds // 60}m"
    if abs(seconds) < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
