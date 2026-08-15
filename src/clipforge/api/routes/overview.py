"""The landing page: what exists, what is moving, what is stuck.

Every number here is counted from the tenant's own rows at request time. There
is no pre-aggregated summary table and no cache, which is the right trade at
this size — the counts are indexed and the tenant scope is in the index — and
the wrong one later, at which point this is the module that gains a rollup.

## The pipeline is the point

Four stages, in the order material actually moves: acquisition, transcription,
clips, uploads. A stage that shows `total` far above `done` with nothing
`in_flight` is a stage where work has stopped, and that is the single most
useful thing this page can tell an operator. The alternative — four unrelated
counts — makes the reader do the join in their head.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter

from ...auth.types import utcnow
from ..deps import ContextDep
from ..schemas import ActivityOut, OverviewResponse, PipelineStageOut, StatOut

router = APIRouter(prefix="/overview", tags=["overview"])


@router.get("", response_model=OverviewResponse)
async def overview(context: ContextDep) -> OverviewResponse:
    now = utcnow()

    with context.unit_of_work() as uow:
        channels = list(uow.channels.all())
        sources = list(uow.sources.all())
        clips = list(uow.clips.all())
        uploads = list(uow.uploads.all())
        acquisitions = list(uow.acquisitions.all())
        transcriptions = list(uow.transcriptions.all())
        jobs = list(uow.jobs.all())

    published = [u for u in uploads if u.state == "published"]
    failed_uploads = [u for u in uploads if u.state == "failed"]
    pending = [u for u in uploads
               if u.state in ("scheduled", "retrying", "uploading")]

    stats = [
        StatOut(
            key="channels", label="Channels",
            value=len([c for c in channels if c.state == "active"]),
            detail=f"{len(channels)} total",
        ),
        StatOut(
            key="sources", label="Sources", value=len(sources),
            detail=f"{sum(1 for s in sources if s.has_transcript)} transcribed",
        ),
        StatOut(
            key="clips", label="Clips", value=len(clips),
            detail=(
                f"top score {max((c.virality_score for c in clips), default=0):.0f}"
                if clips else "none yet"
            ),
        ),
        StatOut(
            key="published", label="Published", value=len(published),
            detail=f"{len(pending)} queued",
        ),
        StatOut(
            key="spend", label="Spend this month",
            value=sum(c.budget_spent_cents for c in channels) / 100,
            unit="USD",
            detail=(
                f"of {sum(c.budget_monthly_cents for c in channels) / 100:.0f} "
                f"budgeted"
            ),
        ),
        StatOut(
            key="library_hours", label="Library",
            value=round(sum(s.duration_s for s in sources) / 3600, 1),
            unit="h", detail="of source material",
        ),
    ]

    pipeline = [
        # `ready`, not `complete`: that is the terminal success the engine
        # actually writes (`engine.py:282`) and the only one the enum allows.
        _stage("acquisition", "Acquired", acquisitions,
               done={"ready"}, failed={"failed", "cancelled"}),
        _stage("transcription", "Transcribed", transcriptions,
               done={"succeeded"},
               failed={"failed_permanent", "failed_retryable"}),
        PipelineStageOut(
            stage="clips", label="Clipped", total=len(clips),
            done=len(clips), failed=0, in_flight=0,
        ),
        PipelineStageOut(
            stage="uploads", label="Published", total=len(uploads),
            done=len(published), failed=len(failed_uploads),
            in_flight=len(pending),
        ),
    ]

    activity = _activity(uploads, sources, now)
    attention = _attention(channels, jobs, sources, uploads, now)

    return OverviewResponse(
        tenant_id=context.tenant_id,
        stats=stats,
        pipeline=pipeline,
        activity=activity,
        attention=attention,
        generated_at=now,
    )


def _stage(key: str, label: str, rows, *, done: set[str], failed: set[str]
           ) -> PipelineStageOut:
    states = [getattr(r, "state", "") for r in rows]
    finished = sum(1 for s in states if s in done)
    broken = sum(1 for s in states if s in failed)
    return PipelineStageOut(
        stage=key, label=label, total=len(states), done=finished,
        failed=broken, in_flight=len(states) - finished - broken,
    )


def _activity(uploads, sources, now) -> list[ActivityOut]:
    """The most recent things that happened, newest first.

    Built from the rows themselves rather than an event table, because there
    is no event table for tenant data — only the auth audit log, which is a
    different thing and deliberately not readable from here.
    """

    events: list[ActivityOut] = []
    for upload in uploads:
        moment = upload.published_at or upload.updated_at or upload.created_at
        if moment is None:
            continue
        events.append(ActivityOut(
            at=moment,
            kind="upload",
            summary=(upload.title or upload.id)[:120],
            state=upload.state,
            reference=upload.id,
        ))
    for source in sources:
        moment = source.created_at
        if moment is None:
            continue
        events.append(ActivityOut(
            at=moment,
            kind="source",
            summary=(source.title or source.url or source.id)[:120],
            state="transcribed" if source.has_transcript else "acquired",
            reference=source.id,
        ))
    events.sort(key=lambda e: e.at, reverse=True)
    return events[:20]


def _attention(channels, jobs, sources, uploads, now) -> list[str]:
    """Only things a person has to do something about.

    A dashboard that lists everything slightly wrong is a dashboard nobody
    reads, so this stays empty unless something is genuinely stuck.
    """

    notes: list[str] = []

    tripped = [c for c in channels if c.state == "circuit_open"]
    if tripped:
        notes.append(
            f"{len(tripped)} channel(s) stopped themselves after repeated "
            f"failures: {', '.join(c.name or c.id for c in tripped[:3])}"
        )

    dead = [j for j in jobs if j.state == "dead"]
    if dead:
        kinds = sorted({j.kind for j in dead})
        notes.append(
            f"{len(dead)} job(s) gave up after every retry ({', '.join(kinds)})"
        )

    lapsing = [
        s for s in sources
        if s.rights_expires_at is not None
        and s.rights_expires_at <= now + timedelta(days=30)
    ]
    if lapsing:
        notes.append(
            f"{len(lapsing)} source(s) have rights expiring within 30 days"
        )

    stuck = [u for u in uploads if u.state == "needs_attention"]
    if stuck:
        notes.append(
            f"{len(stuck)} post(s) need a human — most often a reconnect"
        )

    exhausted = [
        c for c in channels
        if c.budget_monthly_cents and
        c.budget_spent_cents >= c.budget_monthly_cents
    ]
    if exhausted:
        notes.append(
            f"{len(exhausted)} channel(s) have spent their monthly budget"
        )

    return notes
