"""Analytics over whatever has actually been measured.

The honest shape of this endpoint is unusual and deliberate: most of its
numbers are nullable, and it carries a `note` explaining itself when there is
nothing to report.

`RecordedSource` is still the only `MetricSource` in the repository — no live
platform reporting is wired up — so a deployment that has published posts may
well have zero snapshots. Charting that as a flat line at zero would be a
claim: "your videos got no views". The truth is "nobody has collected any
numbers", and the two lead to opposite decisions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Query

from ...auth.types import utcnow
from ..deps import ContextDep
from ..schemas import (
    AnalyticsResponse,
    MetricSeriesOut,
    PlatformBreakdownOut,
    SeriesPointOut,
)
from .uploads import _channel_names, _published_out

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("", response_model=AnalyticsResponse)
async def analytics(
    context: ContextDep,
    window_days: int = Query(30, ge=1, le=365),
) -> AnalyticsResponse:
    now = utcnow()
    since = now - timedelta(days=window_days)

    with context.unit_of_work() as uow:
        uploads = [u for u in uow.uploads.all() if u.state == "published"]
        names = _channel_names(uow)
        latest = {}
        for upload in uploads:
            snapshot = uow.metrics.latest_for_upload(upload.id)
            if snapshot is not None:
                latest[upload.id] = snapshot

    in_window = [
        u for u in uploads
        if (u.published_at or u.created_at) and
        (u.published_at or u.created_at) >= since
    ]
    measured = [u for u in in_window if u.id in latest]

    if not measured:
        return AnalyticsResponse(
            window_days=window_days,
            posts_measured=0,
            series=[],
            by_platform=_platform_rows(in_window, latest),
            top=[],
            note=(
                f"{len(in_window)} post(s) published in this window and none "
                f"has been measured. No live metric source is configured — "
                f"`RecordedSource` is the only implementation in this build — "
                f"so nothing is collecting counters from the platforms."
                if in_window else
                "Nothing has been published in this window."
            ),
        )

    total_views = sum(latest[u.id].views for u in measured)
    total_likes = sum(latest[u.id].likes for u in measured)
    watched = [latest[u.id].avg_watch_pct for u in measured
               if latest[u.id].avg_watch_pct]

    series = _series(measured, latest)
    ranked = sorted(measured, key=lambda u: latest[u.id].views, reverse=True)

    return AnalyticsResponse(
        window_days=window_days,
        posts_measured=len(measured),
        total_views=total_views,
        total_likes=total_likes,
        avg_watch_pct=round(sum(watched) / len(watched), 2) if watched else None,
        series=series,
        by_platform=_platform_rows(in_window, latest),
        top=[_published_out(u, names, latest.get(u.id)) for u in ranked[:10]],
        note="",
    )


def _series(measured, latest) -> list[MetricSeriesOut]:
    """Views and likes per day of publication.

    Keyed by the day the post went out rather than the day it was measured:
    the question is "how did last Tuesday's posts do", and bucketing by
    collection time answers a question nobody asked.
    """

    buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"views": 0.0, "likes": 0.0, "at": None}
    )
    for upload in measured:
        moment = upload.published_at or upload.created_at
        day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        bucket = buckets[day.isoformat()]
        bucket["at"] = day
        bucket["views"] += latest[upload.id].views
        bucket["likes"] += latest[upload.id].likes

    ordered = sorted(buckets.values(), key=lambda b: b["at"])
    return [
        MetricSeriesOut(
            key="views", label="Views by publication day",
            points=[SeriesPointOut(at=b["at"], value=b["views"]) for b in ordered],
        ),
        MetricSeriesOut(
            key="likes", label="Likes by publication day",
            points=[SeriesPointOut(at=b["at"], value=b["likes"]) for b in ordered],
        ),
    ]


def _platform_rows(uploads, latest) -> list[PlatformBreakdownOut]:
    grouped: dict[str, list] = defaultdict(list)
    for upload in uploads:
        grouped[upload.platform].append(upload)

    rows = []
    for platform, held in sorted(grouped.items()):
        snapshots = [latest[u.id] for u in held if u.id in latest]
        watched = [s.avg_watch_pct for s in snapshots if s.avg_watch_pct]
        rows.append(PlatformBreakdownOut(
            platform=platform,
            posts=len(held),
            views=sum(s.views for s in snapshots),
            likes=sum(s.likes for s in snapshots),
            avg_watch_pct=(
                round(sum(watched) / len(watched), 2) if watched else None
            ),
        ))
    return rows
