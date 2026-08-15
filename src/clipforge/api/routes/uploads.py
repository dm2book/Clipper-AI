"""The upload queue, and the posts that made it out.

Two views of one table. The queue is everything not yet live — scheduled,
retrying, uploading, or stuck needing a human — ordered by when it is due.
Published is everything that reached a platform, newest first, joined to its
most recent measurement.

They are separate endpoints rather than one filtered list because they answer
different questions and want different orders, and a shared endpoint with a
`state` parameter ends up with two callers that each ignore half the fields.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ...auth.types import utcnow
from ..deps import ContextDep, require_role
from ..schemas import Page, PublishedVideoOut, UploadOut

router = APIRouter(tags=["uploads"])

#: Everything that is not live yet, including the ones that have stopped.
#:
#: `failed` belongs here even though it has given up: the queue is where an
#: operator goes to find work that is not moving, and the retry endpoint only
#: accepts `failed` or `needs_attention`. Leaving it out makes the one action
#: the page offers unreachable from the page that offers it.
QUEUE_STATES = ("scheduled", "retrying", "uploading", "processing",
                "needs_attention", "awaiting_creator", "failed")

PERMALINKS = {
    "youtube": "https://www.youtube.com/watch?v={id}",
    "tiktok": "https://www.tiktok.com/video/{id}",
    "instagram": "https://www.instagram.com/reel/{id}",
}


def _channel_names(uow) -> dict[str, str]:
    return {c.id: c.name or c.id for c in uow.channels.all()}


@router.get("/uploads", response_model=Page[UploadOut])
async def upload_queue(
    context: ContextDep,
    state: str = Query("", description="one state, or blank for the queue"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[UploadOut]:
    with context.unit_of_work() as uow:
        rows = list(uow.uploads.all())
        names = _channel_names(uow)

    rows = [r for r in rows if r.state == state] if state else [
        r for r in rows if r.state in QUEUE_STATES
    ]
    # Soonest first: a queue is read to find out what happens next.
    rows.sort(key=lambda r: (r.next_attempt_at or r.run_at))
    window = rows[offset:offset + limit]

    return Page[UploadOut](
        items=[
            UploadOut(
                id=r.id, channel_id=r.channel_id,
                channel_name=names.get(r.channel_id, ""),
                account_id=r.account_id, platform=r.platform, state=r.state,
                title=r.title, caption=r.caption, visibility=r.visibility,
                run_at=r.run_at, next_attempt_at=r.next_attempt_at,
                attempt_count=r.attempt_count, last_error=r.last_error,
                remote_post_id=r.remote_post_id, published_at=r.published_at,
                clip_id=r.clip_id,
            )
            for r in window
        ],
        total=len(rows), limit=limit, offset=offset,
    )


@router.post(
    "/uploads/{upload_id}/retry",
    response_model=UploadOut,
    dependencies=[Depends(require_role("editor"))],
)
async def retry_upload(upload_id: str, context: ContextDep) -> UploadOut:
    """Put a failed or stuck post back in the queue, due now.

    Only from a state that has stopped moving. Re-queueing something that is
    mid-upload is how the same video goes out twice, and the idempotency key
    is the last line of defence rather than the first.
    """

    with context.unit_of_work() as uow:
        record = uow.uploads.get(upload_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "No such upload."},
            )
        if record.state not in ("failed", "needs_attention"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "NOT_RETRYABLE",
                    "message": (
                        f"This post is {record.state}. Only a failed or "
                        f"stalled post can be retried; re-queueing one that "
                        f"is in flight risks publishing it twice."
                    ),
                },
            )
        record.state = "scheduled"
        record.next_attempt_at = utcnow()
        record.last_error = ""
        saved = uow.uploads.save(record)
        names = _channel_names(uow)

    return UploadOut(
        id=saved.id, channel_id=saved.channel_id,
        channel_name=names.get(saved.channel_id, ""),
        account_id=saved.account_id, platform=saved.platform,
        state=saved.state, title=saved.title, caption=saved.caption,
        visibility=saved.visibility, run_at=saved.run_at,
        next_attempt_at=saved.next_attempt_at,
        attempt_count=saved.attempt_count, last_error=saved.last_error,
        remote_post_id=saved.remote_post_id, published_at=saved.published_at,
        clip_id=saved.clip_id,
    )


@router.get("/published", response_model=Page[PublishedVideoOut])
async def published(
    context: ContextDep,
    platform: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[PublishedVideoOut]:
    with context.unit_of_work() as uow:
        rows = [u for u in uow.uploads.all() if u.state == "published"]
        names = _channel_names(uow)
        latest = {}
        for upload in rows:
            snapshot = uow.metrics.latest_for_upload(upload.id)
            if snapshot is not None:
                latest[upload.id] = snapshot

    if platform:
        rows = [r for r in rows if r.platform == platform]
    rows.sort(key=lambda r: (r.published_at or r.created_at), reverse=True)
    window = rows[offset:offset + limit]

    return Page[PublishedVideoOut](
        items=[_published_out(r, names, latest.get(r.id)) for r in window],
        total=len(rows), limit=limit, offset=offset,
    )


def _published_out(record, names, snapshot) -> PublishedVideoOut:
    template = PERMALINKS.get(record.platform, "")
    return PublishedVideoOut(
        upload_id=record.id,
        channel_id=record.channel_id,
        channel_name=names.get(record.channel_id, ""),
        platform=record.platform,
        title=record.title,
        remote_post_id=record.remote_post_id,
        published_at=record.published_at,
        permalink=(
            template.format(id=record.remote_post_id)
            if template and record.remote_post_id else ""
        ),
        # Null rather than zero throughout. Nothing has been collected is a
        # different claim from "this post got no views", and the dashboard
        # renders them differently on purpose.
        views=getattr(snapshot, "views", None),
        likes=getattr(snapshot, "likes", None),
        comments=getattr(snapshot, "comments", None),
        shares=getattr(snapshot, "shares", None),
        avg_watch_pct=getattr(snapshot, "avg_watch_pct", None),
        measured_at=getattr(snapshot, "taken_at", None),
        age_hours=getattr(snapshot, "age_hours", None),
    )
