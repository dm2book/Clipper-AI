"""Sources: the long-form material, and how far each one has got.

A source row on its own does not say whether it downloaded or transcribed —
that lives in `acquisition_runs` and `transcription_runs`. The page needs all
three, so this joins them here rather than making the dashboard issue three
requests and correlate by id in the browser.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..deps import ContextDep, require_role
from ..schemas import Page, SourceOut, SubmitSourceRequest

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=Page[SourceOut])
async def list_sources(
    context: ContextDep,
    q: str = Query("", description="match against title, creator or url"),
    transcribed: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[SourceOut]:
    with context.unit_of_work() as uow:
        rows = list(uow.sources.all())
        # Read once and index, rather than a lookup per source: a hundred
        # sources would otherwise be two hundred round trips.
        acquisitions = {}
        for run in uow.acquisitions.all():
            if run.source_id:
                acquisitions.setdefault(run.source_id, run)
        transcriptions = {
            run.source_id: run
            for run in uow.transcriptions.all() if run.source_id
        }

    if q:
        needle = q.casefold()
        rows = [
            r for r in rows
            if needle in (r.title or "").casefold()
            or needle in (r.creator or "").casefold()
            or needle in (r.url or "").casefold()
        ]
    if transcribed is not None:
        rows = [r for r in rows if r.has_transcript is transcribed]

    rows.sort(key=lambda r: r.created_at or r.id, reverse=True)
    window = rows[offset:offset + limit]

    items = []
    for record in window:
        acquisition = acquisitions.get(record.id)
        transcription = transcriptions.get(record.id)
        items.append(SourceOut(
            id=record.id,
            title=record.title or record.url or record.id,
            kind=record.kind,
            url=record.url,
            creator=record.creator,
            language=record.language,
            topics=list(record.topics or []),
            duration_s=record.duration_s,
            has_transcript=record.has_transcript,
            rights_basis=record.rights_basis,
            rights_expires_at=record.rights_expires_at,
            published_at=record.published_at,
            created_at=record.created_at,
            acquisition_state=getattr(acquisition, "state", "") or "",
            media_path=getattr(acquisition, "media_path", "") or "",
            transcription_state=getattr(transcription, "state", "") or "",
            word_count=getattr(transcription, "word_count", 0) or 0,
        ))

    return Page[SourceOut](
        items=items, total=len(rows), limit=limit, offset=offset,
    )


@router.post(
    "", status_code=202,
    dependencies=[Depends(require_role("operator"))],
)
async def submit_source(
    body: SubmitSourceRequest, context: ContextDep, request: Request
) -> dict:
    """Queue a URL or an uploaded file for acquisition.

    202 rather than 201: nothing exists yet. The acquisition engine downloads,
    probes and persists on a worker, and the source row appears when it has —
    so returning 201 with an id would be inventing one.

    Refuses when no acquisition engine is configured rather than accepting the
    request into a queue nothing drains, which is the failure mode that looks
    like success for a day.
    """

    factory = context.services.acquisition_factory
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ACQUISITION_UNAVAILABLE",
                "message": (
                    "This deployment has no acquisition worker configured, so "
                    "there is nothing to hand this to. Submissions would sit "
                    "in a queue nothing drains."
                ),
            },
        )
    engine = factory(context.services.database, context.tenant_id)
    job_ids = engine.submit(body.url, channel_id=body.channel_id)
    return {"queued": len(job_ids), "job_ids": job_ids}
