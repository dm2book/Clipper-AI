"""Domain objects to rows, and back.

The two layers are kept apart on purpose. A `ScheduledPost` knows how to back
off after a rate-limit; an `UploadRecord` knows what a row of `uploads` looks
like. Keeping them separate means the schema can gain a column without touching
the publishing engine, and the engine can gain a computed property without a
migration.

The price is this file, and it is worth paying explicitly rather than by
letting the engine write SQL.

Two rules hold throughout:

* **A round trip is lossless.** `to_post(to_upload(post, ...))` gives back an
  equal post. `tests/test_store_mapping.py` asserts it field by field rather
  than on a couple of examples, so a field added to either side without a
  mapping fails a test instead of silently vanishing at the next restart.
* **Nothing is invented on the way back.** A column the domain object has no
  place for is carried in `metadata` under a reserved key, not dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..factory.channel import Budget, Channel, ChannelHealth, ChannelState
from ..factory.niches import Niche
from ..factory.sources import RightsBasis, Rights, SourceKind
from ..factory.sources import Source as FactorySource
from ..publish.oauth import TokenSet
from ..publish.types import (
    Account,
    Attempt,
    MediaAsset,
    Platform,
    PostSpec,
    PostState,
    ScheduledPost,
    Visibility,
    ensure_utc,
)
from .records import (
    ChannelRecord,
    SocialAccountRecord,
    SourceRecord,
    UploadRecord,
)

__all__ = [
    "to_upload_record",
    "to_scheduled_post",
    "to_account_record",
    "to_account",
    "apply_tokens",
    "to_token_set",
    "to_source_record",
    "to_source",
    "to_channel_record",
    "to_channel",
]

#: Keys `metadata` reserves for things the domain object carries but the table
#: has no column for. Namespaced so a caller's own metadata cannot collide.
_SPEC = "_spec"
_SERIES = "_series_id"


# ---------------------------------------------------------------------------
# ScheduledPost <-> UploadRecord
# ---------------------------------------------------------------------------


def _asset_to_dict(asset: MediaAsset) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "path": asset.path,
        "public_url": asset.public_url,
        "size_bytes": asset.size_bytes,
        "duration_s": asset.duration_s,
        "width": asset.width,
        "height": asset.height,
        "fps": asset.fps,
        "checksum": asset.checksum,
    }


def _asset_from_dict(raw: dict[str, Any]) -> MediaAsset:
    return MediaAsset(
        asset_id=raw.get("asset_id", ""),
        path=raw.get("path", ""),
        public_url=raw.get("public_url", ""),
        size_bytes=int(raw.get("size_bytes", 0)),
        duration_s=float(raw.get("duration_s", 0.0)),
        width=int(raw.get("width", 1080)),
        height=int(raw.get("height", 1920)),
        fps=int(raw.get("fps", 60)),
        checksum=raw.get("checksum", ""),
    )


def _attempt_from_dict(raw: dict[str, Any]) -> Attempt:
    finished = raw.get("finished_at")
    return Attempt(
        number=int(raw["number"]),
        started_at=datetime.fromisoformat(raw["started_at"]),
        finished_at=datetime.fromisoformat(finished) if finished else None,
        state=PostState(raw.get("state", "uploading")),
        error_code=raw.get("error_code", ""),
        error_message=raw.get("error_message", ""),
        disposition=raw.get("disposition", ""),
        remote_ref=raw.get("remote_ref", ""),
    )


def to_upload_record(
    post: ScheduledPost,
    *,
    tenant_id: str,
    channel_id: str,
    video_id: str | None = None,
    clip_id: str | None = None,
) -> UploadRecord:
    """A post as a row.

    `tenant_id` and `channel_id` are arguments rather than fields on the post:
    the publishing engine is deliberately unaware of both, and giving it a
    tenant would be the first step toward it deciding which one it is looking
    at. The caller — which already knows, because it opened the unit of work —
    supplies them.
    """

    spec = post.spec
    metadata: dict[str, Any] = dict(spec.metadata)
    # The columns hold what is queried. Everything else the spec carries lives
    # here rather than becoming eleven columns nothing filters on.
    metadata[_SPEC] = {
        "asset": _asset_to_dict(spec.asset),
        "hashtags": list(spec.hashtags),
        "per_platform_caption": dict(spec.per_platform_caption),
        "category_id": spec.category_id,
        "made_for_kids": spec.made_for_kids,
    }
    if post.series_id:
        # Not `schedule_id`, which is a foreign key into `schedules`. A series
        # id from the publishing engine's own recurrence is not always backed
        # by a Schedule row, and pointing the column at one that does not exist
        # trades a lost field for a failed insert.
        metadata[_SERIES] = post.series_id

    return UploadRecord(
        id=post.post_id,
        tenant_id=tenant_id,
        channel_id=channel_id,
        account_id=post.account_id,
        clip_id=clip_id,
        video_id=video_id,
        platform=post.platform.value,
        state=post.state.value,
        run_at=ensure_utc(post.run_at),
        next_attempt_at=(
            ensure_utc(post.next_attempt_at) if post.next_attempt_at else None
        ),
        lease_until=ensure_utc(post.lease_until) if post.lease_until else None,
        title=spec.title,
        caption=spec.caption,
        visibility=spec.visibility.value,
        metadata=metadata,
        idempotency_key=post.idempotency_key,
        remote_post_id=post.remote_post_id,
        attempt_count=post.attempt_count,
        attempts=[a.to_dict() for a in post.attempts],
        last_error=post.last_error,
        published_at=_published_at(post),
    )


def _published_at(post: ScheduledPost) -> datetime | None:
    """When the platform confirmed, taken from the attempt that did it.

    Derived rather than tracked on the post, because the post has no such
    field and inventing one would put two answers in the system.
    """

    for attempt in reversed(post.attempts):
        if attempt.state in (PostState.PUBLISHED, PostState.AWAITING_CREATOR):
            return attempt.finished_at or attempt.started_at
    return None


def to_scheduled_post(record: UploadRecord) -> ScheduledPost:
    """A row as a post."""

    metadata = dict(record.metadata or {})
    packed = metadata.pop(_SPEC, {}) or {}
    series_id = metadata.pop(_SERIES, "")

    spec = PostSpec(
        asset=_asset_from_dict(packed.get("asset", {})),
        title=record.title,
        caption=record.caption,
        hashtags=tuple(packed.get("hashtags", ())),
        per_platform_caption=dict(packed.get("per_platform_caption", {})),
        visibility=Visibility(record.visibility),
        category_id=packed.get("category_id", "22"),
        made_for_kids=bool(packed.get("made_for_kids", False)),
        metadata=metadata,
    )
    return ScheduledPost(
        post_id=record.id,
        account_id=record.account_id,
        platform=Platform(record.platform),
        spec=spec,
        run_at=record.run_at,
        state=PostState(record.state),
        attempts=[_attempt_from_dict(a) for a in (record.attempts or [])],
        remote_post_id=record.remote_post_id,
        idempotency_key=record.idempotency_key,
        series_id=series_id,
        lease_until=record.lease_until,
        next_attempt_at=record.next_attempt_at,
        last_error=record.last_error,
    )


# ---------------------------------------------------------------------------
# Account <-> SocialAccountRecord
# ---------------------------------------------------------------------------


def to_account_record(
    account: Account, *, tenant_id: str, channel_id: str | None = None
) -> SocialAccountRecord:
    """An account as a row, credentials untouched.

    Deliberately does not write the token columns. Credentials arrive through
    `apply_tokens`, sealed, and a mapper that could write plaintext into them
    by accident is a mapper that eventually does.
    """

    return SocialAccountRecord(
        id=account.account_id,
        tenant_id=tenant_id,
        channel_id=channel_id,
        platform=account.platform.value,
        handle=account.handle,
        external_id=account.external_id,
        timezone=account.timezone,
        direct_post_approved=account.direct_post_approved,
        business_account=account.business_account,
        enabled=account.enabled,
    )


def to_account(record: SocialAccountRecord) -> Account:
    return Account(
        account_id=record.id,
        platform=Platform(record.platform),
        org_id=record.tenant_id,
        handle=record.handle,
        external_id=record.external_id,
        timezone=record.timezone,
        direct_post_approved=record.direct_post_approved,
        business_account=record.business_account,
        enabled=record.enabled,
    )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def apply_tokens(
    record: SocialAccountRecord, tokens: TokenSet, *, seal
) -> SocialAccountRecord:
    """Write sealed credentials onto an account row.

    `seal` is required, not optional, and there is no default. A refresh token
    is a long-lived credential to someone else's audience; a signature that
    lets it be omitted is one an exhausted person fills in with `lambda s: s`
    at two in the morning. The key belongs in a KMS, held by something other
    than the process that publishes.
    """

    record.access_token_sealed = seal(tokens.access_token) if tokens.access_token else ""
    record.refresh_token_sealed = (
        seal(tokens.refresh_token) if tokens.refresh_token else ""
    )
    record.token_expires_at = tokens.expires_at
    record.refresh_valid_until = tokens.refresh_valid_until
    record.token_obtained_at = tokens.obtained_at
    record.scopes = list(tokens.scopes)
    return record


def to_token_set(record: SocialAccountRecord, *, unseal) -> TokenSet:
    return TokenSet(
        account_id=record.id,
        platform=Platform(record.platform),
        access_token=(
            unseal(record.access_token_sealed) if record.access_token_sealed else ""
        ),
        refresh_token=(
            unseal(record.refresh_token_sealed) if record.refresh_token_sealed else ""
        ),
        expires_at=record.token_expires_at,
        scopes=tuple(record.scopes),
        refresh_valid_until=record.refresh_valid_until,
        obtained_at=record.token_obtained_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Source <-> SourceRecord
# ---------------------------------------------------------------------------


def to_source_record(source: FactorySource, *, tenant_id: str) -> SourceRecord:
    """Licensed raw material as a row.

    The rights fields become columns rather than a JSON blob because they are
    *queried* — the expiry sweep and the clearance gate both filter on them,
    and a filter over JSON is a sequential scan of every source the tenant has
    ever registered.
    """

    rights = source.rights
    return SourceRecord(
        id=source.source_id,
        tenant_id=tenant_id,
        title=source.title,
        kind=source.kind.value,
        url=source.url,
        creator=source.creator,
        language=source.language,
        topics=list(source.topics),
        duration_s=source.duration_s,
        has_transcript=source.has_transcript,
        published_at=source.published_at,
        # Derived from creator + id, so the same upload reappearing under a new
        # URL is recognised rather than clipped a second time.
        fingerprint=source.fingerprint,
        rights_basis=rights.basis.value,
        rights_reference=rights.reference,
        rights_attribution=rights.attribution,
        commercial_use=rights.commercial_use,
        derivatives=rights.derivatives,
        rights_verified_at=rights.verified_at,
        rights_expires_at=rights.expires_at,
    )


def to_source(record: SourceRecord) -> FactorySource:
    return FactorySource(
        source_id=record.id,
        title=record.title,
        kind=SourceKind(record.kind),
        rights=Rights(
            basis=RightsBasis(record.rights_basis),
            reference=record.rights_reference,
            attribution=record.rights_attribution,
            commercial_use=record.commercial_use,
            derivatives=record.derivatives,
            verified_at=record.rights_verified_at,
            expires_at=record.rights_expires_at,
        ),
        url=record.url,
        creator=record.creator,
        duration_s=record.duration_s,
        published_at=record.published_at,
        language=record.language,
        topics=tuple(record.topics),
        has_transcript=record.has_transcript,
    )


# ---------------------------------------------------------------------------
# Channel <-> ChannelRecord
# ---------------------------------------------------------------------------


def to_channel_record(
    channel: Channel, *, tenant_id: str, project_id: str
) -> ChannelRecord:
    """A channel as a row.

    `accounts` is not written here: it is a platform-to-account map, and the
    account rows already carry `channel_id`. Storing it twice is storing two
    answers, and they disagree the first time an account is disconnected.

    `used_fingerprints` is likewise absent. It is the `channel_source_uses`
    join table — an array column would be rewritten in full on every append,
    and that set grows for as long as the channel runs.
    """

    health = channel.health
    budget = channel.budget
    return ChannelRecord(
        id=channel.channel_id,
        tenant_id=tenant_id,
        project_id=project_id,
        name=channel.name,
        niche=channel.niche.value,
        state=channel.state.value,
        timezone=channel.timezone,
        topics=list(channel.topics),
        accepted_rights=sorted(r.value for r in channel.accepted_rights),
        monetised=channel.monetised,
        cadence_override=channel.cadence_override,
        quality_floor_override=channel.quality_floor_override,
        budget_monthly_cents=budget.monthly_cents,
        budget_spent_cents=budget.spent_cents,
        budget_period=budget.period,
        # Circuit-breaker state, persisted rather than recomputed: a channel
        # that tripped before a restart must stay tripped, or a deploy silently
        # retries every failing channel at once.
        consecutive_failures=health.consecutive_failures,
        circuit_opened_at=health.opened_at,
        last_error=health.last_error,
        total_items=health.total_items,
        total_published=health.total_published,
        total_blocked=health.total_blocked,
        total_failed=health.total_failed,
        created_at=channel.created_at,
    )


def to_channel(
    record: ChannelRecord,
    *,
    accounts: dict[Platform, str] | None = None,
    used_fingerprints: set[str] | None = None,
) -> Channel:
    """A row as a channel.

    `accounts` and `used_fingerprints` come from their own tables, so the
    caller passes them in rather than this function reaching for a database it
    was not given.
    """

    return Channel(
        channel_id=record.id,
        name=record.name,
        niche=Niche(record.niche),
        org_id=record.tenant_id,
        accounts=dict(accounts or {}),
        topics=tuple(record.topics),
        accepted_rights=frozenset(
            RightsBasis(value) for value in record.accepted_rights
        ),
        monetised=record.monetised,
        timezone=record.timezone,
        state=ChannelState(record.state),
        budget=Budget(
            monthly_cents=record.budget_monthly_cents,
            spent_cents=record.budget_spent_cents,
            period=record.budget_period,
        ),
        health=ChannelHealth(
            consecutive_failures=record.consecutive_failures,
            total_items=record.total_items,
            total_published=record.total_published,
            total_blocked=record.total_blocked,
            total_failed=record.total_failed,
            opened_at=record.circuit_opened_at,
            last_error=record.last_error,
        ),
        cadence_override=record.cadence_override,
        quality_floor_override=record.quality_floor_override,
        used_fingerprints=set(used_fingerprints or ()),
        created_at=record.created_at,
    )
