"""Channels: the unit that owns a niche, a budget and a posting cadence."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import ContextDep, require_role
from ..schemas import ChannelOut, ChannelStateUpdate, Page

router = APIRouter(prefix="/channels", tags=["channels"])

#: What an operator may move a channel between. `circuit_open` is absent
#: deliberately: a breaker is tripped by the system observing repeated
#: failures, and letting a human set it by hand would make the state mean two
#: different things.
SETTABLE = frozenset({"draft", "active", "paused"})


def _out(record) -> ChannelOut:
    return ChannelOut(
        id=record.id,
        name=record.name,
        niche=record.niche,
        state=record.state,
        timezone=record.timezone,
        topics=list(record.topics or []),
        monetised=record.monetised,
        budget_monthly_cents=record.budget_monthly_cents,
        budget_spent_cents=record.budget_spent_cents,
        budget_remaining_cents=max(
            0, record.budget_monthly_cents - record.budget_spent_cents
        ),
        consecutive_failures=record.consecutive_failures,
        circuit_opened_at=record.circuit_opened_at,
        last_error=record.last_error,
        total_items=record.total_items,
        total_published=record.total_published,
        total_blocked=record.total_blocked,
        total_failed=record.total_failed,
        created_at=record.created_at,
    )


@router.get("", response_model=Page[ChannelOut])
async def list_channels(
    context: ContextDep,
    state: str = Query("", description="filter to one state"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ChannelOut]:
    with context.unit_of_work() as uow:
        rows = list(uow.channels.all())

    if state:
        rows = [r for r in rows if r.state == state]
    rows.sort(key=lambda r: (r.state != "active", r.name or r.id))
    window = rows[offset:offset + limit]
    return Page[ChannelOut](
        items=[_out(r) for r in window],
        total=len(rows), limit=limit, offset=offset,
    )


@router.get("/{channel_id}", response_model=ChannelOut)
async def get_channel(channel_id: str, context: ContextDep) -> ChannelOut:
    with context.unit_of_work() as uow:
        record = uow.channels.get(channel_id)
    if record is None:
        # 404 rather than 403 for another tenant's id, and they are the same
        # answer on purpose: the store cannot see across the boundary, so
        # "not yours" and "does not exist" are genuinely indistinguishable
        # here — which is also what stops this becoming an id oracle.
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "No such channel."},
        )
    return _out(record)


@router.patch(
    "/{channel_id}/state",
    response_model=ChannelOut,
    dependencies=[Depends(require_role("operator"))],
)
async def set_state(
    channel_id: str, body: ChannelStateUpdate, context: ContextDep
) -> ChannelOut:
    """Activate or pause a channel.

    Operator or above: pausing a channel stops a customer's publishing, which
    is not something a viewer or an analyst should be able to do by finding
    the endpoint.
    """

    if body.state not in SETTABLE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_STATE",
                "message": f"State must be one of {', '.join(sorted(SETTABLE))}.",
            },
        )
    with context.unit_of_work() as uow:
        record = uow.channels.get(channel_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "No such channel."},
            )
        record.state = body.state
        if body.state == "active":
            # Resuming clears the breaker's counter. Leaving it would trip the
            # channel again on its next single failure.
            record.consecutive_failures = 0
            record.circuit_opened_at = None
        saved = uow.channels.save(record)
    return _out(saved)
