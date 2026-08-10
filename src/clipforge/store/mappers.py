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
from .records import SocialAccountRecord, UploadRecord

__all__ = [
    "to_upload_record",
    "to_scheduled_post",
    "to_account_record",
    "to_account",
    "apply_tokens",
    "to_token_set",
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
