"""Platform constraints: media, quota, tokens, and what blocks automation.

Everything in this file is a fact about somebody else's product, which means
two things. It changes without warning, so it lives in one file with a version
stamp rather than being sprinkled through the adapters. And it is the reason
"fully automated publishing" is a claim that needs qualifying rather than a
feature that needs building.

### The three things that actually stop full automation

**TikTok requires an audit before unattended posting.** An unaudited client can
only push to the creator's *inbox* as a draft — the creator then opens the app
and finishes the post by hand. Unaudited clients are additionally restricted to
private visibility. Until that audit clears, TikTok is not automated; it is a
draft courier. The system reports this per account rather than discovering it
at post time.

**YouTube's quota is per project, not per channel.** The default allowance is
10,000 units a day and `videos.insert` costs 1,600, so a project can upload six
videos a day in total — six across *every* connected channel of *every*
customer. Nothing about connecting more accounts changes that number. A
multi-tenant publisher needs a quota increase before it has a product, and this
is the single most common thing people discover in production.

**Instagram pulls; it does not receive.** The Graph API takes a public URL and
fetches the file itself, so the media has to be hosted somewhere reachable and
stay reachable for the whole transcode. It also requires a Business or Creator
account linked to a Facebook Page — a personal account cannot publish through
the API at all.

### Scheduling months ahead is a credentials problem

Only YouTube offers real server-side scheduling (`status.publishAt`). For the
other two, "schedule" means this system holds the job and fires it at the
moment, which puts every long-dated post at the mercy of a token staying alive
for the whole wait. An Instagram long-lived token is good for sixty days. A
post scheduled ninety days out will fail on a silently dead credential unless
something refreshed it in between — see `refresh_grace_days`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import Account, MediaAsset, Platform, PostSpec, Visibility

#: Bump when any figure below is re-checked against live documentation. These
#: are third-party facts with no changelog; treat anything older than a quarter
#: as suspect.
LIMITS_VERSION = "2026-08-verify-quarterly"


@dataclass(frozen=True, slots=True)
class MediaLimits:
    max_bytes: int
    min_duration_s: float
    max_duration_s: float
    max_caption_chars: int
    max_title_chars: int = 0
    max_hashtags: int = 0
    #: Vertical short-form cut-off, where the platform has a distinct surface.
    short_form_max_s: float = 0.0


@dataclass(frozen=True, slots=True)
class RateLimits:
    #: Posts one account may publish in a rolling 24 hours.
    posts_per_day: int
    #: API cost of one upload, in whatever unit the platform bills.
    upload_cost: int = 0
    #: Daily budget of that unit. Scoped by `quota_scope`.
    daily_budget: int = 0
    #: "account" or "project" — the difference between a limit that grows with
    #: customers and one that does not.
    quota_scope: str = "account"
    #: Minimum spacing the platform enforces or strongly prefers, in seconds.
    min_spacing_s: int = 0


@dataclass(frozen=True, slots=True)
class TokenPolicy:
    access_ttl_s: int
    #: How long after the last successful refresh the credentials stay usable.
    #: This — not the access-token TTL — is what decides whether a post
    #: scheduled for next quarter will still have a live account to post to.
    refresh_grace_days: int
    #: True when the platform can be handed a future publish time and will fire
    #: it itself. Only YouTube can.
    server_side_scheduling: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class PlatformLimits:
    platform: Platform
    media: MediaLimits
    rate: RateLimits
    tokens: TokenPolicy
    #: Human-readable statement of what stands between this platform and
    #: unattended posting. Empty when nothing does.
    automation_blocker: str = ""
    scopes: tuple[str, ...] = ()


LIMITS: dict[Platform, PlatformLimits] = {
    Platform.TIKTOK: PlatformLimits(
        platform=Platform.TIKTOK,
        media=MediaLimits(
            max_bytes=4 * 1024**3,
            min_duration_s=3.0,
            max_duration_s=600.0,
            max_caption_chars=2200,
            max_title_chars=0,
            max_hashtags=0,
            short_form_max_s=600.0,
        ),
        rate=RateLimits(
            posts_per_day=6,
            quota_scope="account",
            min_spacing_s=0,
        ),
        tokens=TokenPolicy(
            access_ttl_s=24 * 3600,
            refresh_grace_days=365,
            server_side_scheduling=False,
            note="Access token lasts a day; the refresh token lasts a year. "
                 "A schedule outliving that year needs the creator to "
                 "reauthorise before it does.",
        ),
        automation_blocker=(
            "Direct Post requires TikTok's app audit. Until it clears, uploads "
            "land in the creator's inbox as a draft that a human must finish, "
            "and visibility is forced to private."
        ),
        scopes=("video.upload", "video.publish"),
    ),
    Platform.YOUTUBE: PlatformLimits(
        platform=Platform.YOUTUBE,
        media=MediaLimits(
            max_bytes=256 * 1024**3,
            min_duration_s=1.0,
            max_duration_s=12 * 3600.0,
            max_caption_chars=5000,     # description
            max_title_chars=100,
            max_hashtags=15,
            short_form_max_s=180.0,     # Shorts surface
        ),
        rate=RateLimits(
            posts_per_day=6,            # 10,000 / 1,600, floored
            upload_cost=1600,
            daily_budget=10_000,
            quota_scope="project",
            min_spacing_s=0,
        ),
        tokens=TokenPolicy(
            access_ttl_s=3600,
            refresh_grace_days=3650,
            server_side_scheduling=True,
            note="Refresh tokens do not expire for a published app — but they "
                 "expire after 7 days while the OAuth consent screen is still "
                 "in Testing, which is where most projects sit during "
                 "development.",
        ),
        automation_blocker="",
        scopes=("https://www.googleapis.com/auth/youtube.upload",),
    ),
    Platform.INSTAGRAM: PlatformLimits(
        platform=Platform.INSTAGRAM,
        media=MediaLimits(
            max_bytes=1 * 1024**3,
            min_duration_s=3.0,
            max_duration_s=900.0,
            max_caption_chars=2200,
            max_title_chars=0,
            max_hashtags=30,
            short_form_max_s=900.0,
        ),
        rate=RateLimits(
            posts_per_day=25,
            quota_scope="account",
            min_spacing_s=0,
        ),
        tokens=TokenPolicy(
            access_ttl_s=60 * 24 * 3600,
            refresh_grace_days=60,
            server_side_scheduling=False,
            note="Long-lived tokens last 60 days and must be refreshed inside "
                 "that window. This is the shortest grace period of the three "
                 "and the one that quietly kills long-dated schedules.",
        ),
        automation_blocker=(
            "Requires a Business or Creator account linked to a Facebook Page, "
            "and the media must be fetchable from a public URL for the whole "
            "of Instagram's transcode — the API pulls the file rather than "
            "accepting bytes."
        ),
        scopes=("instagram_content_publish", "pages_read_engagement"),
    ),
}

#: TikTok chunked upload. Chunks below the minimum are rejected outright, and
#: the total count is capped, which together set a floor on chunk size for
#: large files.
TIKTOK_MIN_CHUNK = 5 * 1024**2
TIKTOK_MAX_CHUNK = 64 * 1024**2
TIKTOK_MAX_CHUNKS = 1000

#: Google resumable upload. Chunks must be a multiple of 256 KiB except the
#: last one; a chunk that is not will be rejected mid-transfer, after the bytes
#: have already been sent.
GOOGLE_CHUNK_MULTIPLE = 256 * 1024
GOOGLE_DEFAULT_CHUNK = 8 * 1024**2

#: An Instagram media container is discarded if it is not published within a
#: day of creation.
INSTAGRAM_CONTAINER_TTL_S = 24 * 3600


def limits_for(platform: Platform) -> PlatformLimits:
    return LIMITS[platform]


def tiktok_chunking(size_bytes: int) -> tuple[int, int]:
    """Chunk size and count for a TikTok upload.

    Sizes below the platform minimum are sent whole: TikTok accepts a
    single-chunk upload smaller than the minimum chunk size, but rejects a
    *multi*-chunk upload with an undersized chunk in it.
    """
    if size_bytes <= TIKTOK_MIN_CHUNK:
        return size_bytes, 1

    chunk = TIKTOK_MIN_CHUNK
    while size_bytes / chunk > TIKTOK_MAX_CHUNKS and chunk < TIKTOK_MAX_CHUNK:
        chunk = min(chunk * 2, TIKTOK_MAX_CHUNK)

    count = max(1, size_bytes // chunk)
    return chunk, count


def google_chunk_size(preferred: int = GOOGLE_DEFAULT_CHUNK) -> int:
    """Round a preferred chunk size to Google's required 256 KiB multiple."""
    multiples = max(1, round(preferred / GOOGLE_CHUNK_MULTIPLE))
    return multiples * GOOGLE_CHUNK_MULTIPLE


def automation_gap(account: Account) -> str:
    """What stands between this account and unattended posting, or empty.

    Called before scheduling rather than at post time, because "your TikTok
    app was never audited" is a fact worth knowing when a customer queues
    ninety posts, not ninety times over the following month.
    """
    entry = LIMITS[account.platform]

    if account.platform is Platform.TIKTOK and not account.direct_post_approved:
        return entry.automation_blocker
    if account.platform is Platform.INSTAGRAM and not account.business_account:
        return (
            "Instagram account is not a Business or Creator account linked to "
            "a Facebook Page — the Content Publishing API cannot post to it."
        )
    if account.platform is Platform.YOUTUBE and not account.direct_post_approved:
        return (
            "YouTube app has not passed verification; uploads from an "
            "unverified client are locked to private regardless of the "
            "requested visibility."
        )
    return ""


def effective_visibility(account: Account, requested: Visibility) -> Visibility:
    """What the platform will actually apply, which may not be what was asked.

    Returning the truth here rather than the request is the difference between
    a customer seeing "scheduled: public" and later finding ninety private
    videos, and seeing "scheduled: private (app not yet audited)" up front.
    """
    if automation_gap(account):
        return Visibility.PRIVATE
    return requested


def validate(spec: PostSpec, account: Account) -> list[str]:
    """Everything wrong with this post for this account.

    Checked at schedule time. A validation failure discovered three weeks later
    when the job fires is a validation failure discovered in the worst possible
    place.
    """
    entry = LIMITS[account.platform]
    asset = spec.asset
    problems: list[str] = []

    if asset.size_bytes > entry.media.max_bytes:
        problems.append(
            f"file is {asset.size_bytes / 1024**3:.2f} GB; "
            f"{account.platform.value} accepts up to "
            f"{entry.media.max_bytes / 1024**3:.0f} GB"
        )
    if asset.duration_s and asset.duration_s < entry.media.min_duration_s:
        problems.append(
            f"{asset.duration_s:.1f}s is below "
            f"{account.platform.value}'s {entry.media.min_duration_s:.0f}s "
            f"minimum"
        )
    if asset.duration_s > entry.media.max_duration_s:
        problems.append(
            f"{asset.duration_s:.0f}s exceeds "
            f"{account.platform.value}'s {entry.media.max_duration_s:.0f}s "
            f"maximum"
        )

    caption = spec.caption_for(account.platform)
    if len(caption) > entry.media.max_caption_chars:
        problems.append(
            f"caption is {len(caption)} characters; "
            f"{account.platform.value} allows {entry.media.max_caption_chars}"
        )
    if entry.media.max_title_chars and len(spec.title) > entry.media.max_title_chars:
        problems.append(
            f"title is {len(spec.title)} characters; "
            f"{account.platform.value} allows {entry.media.max_title_chars}"
        )
    if entry.media.max_hashtags and len(spec.hashtags) > entry.media.max_hashtags:
        problems.append(
            f"{len(spec.hashtags)} hashtags; "
            f"{account.platform.value} allows {entry.media.max_hashtags}"
        )

    if account.platform is Platform.INSTAGRAM and not asset.public_url:
        problems.append(
            "Instagram fetches the file from a URL rather than accepting an "
            "upload — MediaAsset.public_url is required and must stay live "
            "through the transcode"
        )
    if account.platform is not Platform.INSTAGRAM and not asset.path:
        problems.append("MediaAsset.path is required to upload bytes")

    if not account.enabled:
        problems.append("account is disabled")

    if account.platform is Platform.YOUTUBE and not spec.title:
        problems.append("YouTube requires a title")

    return problems


def is_short_form(asset: MediaAsset, platform: Platform) -> bool:
    """Whether this lands on the platform's short-form surface.

    Not a flag anyone sets — it is inferred from the file. A 61-second vertical
    video is not a Short, and nothing in the upload request can make it one.
    """
    entry = LIMITS[platform]
    if not entry.media.short_form_max_s:
        return False
    return asset.is_vertical and asset.duration_s <= entry.media.short_form_max_s


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether an account can actually be published to unattended."""

    account_id: str
    platform: Platform
    automated: bool
    degraded_to: str = ""
    blocker: str = ""
    posts_per_day: int = 0
    quota_scope: str = "account"
    server_side_scheduling: bool = False
    safe_horizon_days: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "platform": self.platform.value,
            "automated": self.automated,
            "degraded_to": self.degraded_to,
            "blocker": self.blocker,
            "posts_per_day": self.posts_per_day,
            "quota_scope": self.quota_scope,
            "server_side_scheduling": self.server_side_scheduling,
            "safe_horizon_days": self.safe_horizon_days,
            "notes": list(self.notes),
        }


def readiness(account: Account) -> Readiness:
    """A straight answer about what this account can and cannot do."""
    entry = LIMITS[account.platform]
    blocker = automation_gap(account)

    degraded = ""
    if blocker:
        degraded = (
            "draft in creator inbox"
            if account.platform is Platform.TIKTOK
            else "private upload"
            if account.platform is Platform.YOUTUBE
            else "cannot publish"
        )

    notes = [entry.tokens.note]
    if entry.rate.quota_scope == "project":
        notes.append(
            f"Quota is per API project, not per channel: "
            f"{entry.rate.daily_budget:,} units a day at "
            f"{entry.rate.upload_cost:,} per upload is "
            f"{entry.rate.posts_per_day} uploads a day across every connected "
            f"account. Connecting more accounts does not raise it."
        )
    if not entry.tokens.server_side_scheduling:
        notes.append(
            "No server-side scheduling: this system holds the job and fires "
            "it, so a long-dated post depends on the credential surviving the "
            "wait."
        )

    return Readiness(
        account_id=account.account_id,
        platform=account.platform,
        automated=not blocker and account.enabled,
        degraded_to=degraded,
        blocker=blocker,
        posts_per_day=entry.rate.posts_per_day,
        quota_scope=entry.rate.quota_scope,
        server_side_scheduling=entry.tokens.server_side_scheduling,
        safe_horizon_days=entry.tokens.refresh_grace_days,
        notes=tuple(note for note in notes if note),
    )
