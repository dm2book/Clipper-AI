"""What the queue looks like from outside, for a human or a scrape.

Four numbers matter, and only one of them is the obvious one.

**Depth** is the obvious one, and on its own it is nearly useless: a queue of
zero means either "everything is done" or "no producer is running", and those
need opposite responses.

**Oldest queued age** is the one that actually detects an outage. A queue that
holds steady at forty jobs is healthy if the oldest is twenty seconds old and
broken if it is four hours old, and depth cannot tell those apart.

**Dead count** is the dead-letter queue. There is no separate DLQ table:
`jobs.state = 'dead'` *is* the dead letter queue, reached when a job exhausts
`max_attempts` or a handler returns `Fatal`. A separate table would need its
own retention, its own access control and its own way of being requeued, to
hold rows that already have all three.

**Leased-but-stale** catches the failure mode nothing else does: jobs held by
a worker that died. They are not queued, so depth misses them; they are not
dead, so the dead count misses them. They sit invisible until a reaper runs,
and if no worker is running there is no reaper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..store.records import utcnow

__all__ = ["QueueSnapshot", "KindDepth", "snapshot", "ACTIVE_STATES"]

#: States a job passes through while it is still somebody's problem.
ACTIVE_STATES = ("queued", "leased", "running")

#: A queued job older than this is reported as stalled. Chosen against the
#: default backoff: an eighth attempt is scheduled ~32 minutes out, so an hour
#: is comfortably past "a job that is merely being retried patiently".
STALLED_AFTER_S = 3600.0


@dataclass(frozen=True, slots=True)
class KindDepth:
    kind: str
    queued: int = 0
    leased: int = 0
    dead: int = 0
    oldest_queued_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "queued": self.queued, "leased": self.leased,
            "dead": self.dead,
            "oldest_queued_s": (
                round(self.oldest_queued_s, 1)
                if self.oldest_queued_s is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """One tenant's queue, at one instant."""

    tenant_id: str
    at: datetime
    queued: int = 0
    leased: int = 0
    dead: int = 0
    succeeded_recently: int = 0
    #: Age of the oldest job still waiting. None when nothing is waiting.
    oldest_queued_s: float | None = None
    #: Leases that have already expired and have not been reaped. Non-zero
    #: means a worker died, or no worker is running to reap.
    stale_leases: int = 0
    by_kind: tuple[KindDepth, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        """A judgement, so a dashboard does not have to invent one.

        Deliberately not "depth is zero". A busy queue is healthy; a queue
        whose oldest job predates lunch is not.
        """

        if self.stale_leases:
            return False
        if self.oldest_queued_s is not None and (
            self.oldest_queued_s > STALLED_AFTER_S
        ):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "at": self.at.isoformat(),
            "queued": self.queued,
            "leased": self.leased,
            "dead": self.dead,
            "succeeded_recently": self.succeeded_recently,
            "oldest_queued_s": (
                round(self.oldest_queued_s, 1)
                if self.oldest_queued_s is not None else None
            ),
            "stale_leases": self.stale_leases,
            "healthy": self.healthy,
            "by_kind": [k.to_dict() for k in self.by_kind],
            "notes": list(self.notes),
        }


def snapshot(
    database: Any, tenant_id: str, *, now: datetime | None = None,
    recent_window_s: float = 3600.0,
) -> QueueSnapshot:
    """Read the queue for one tenant.

    Implemented over `jobs.all()` rather than aggregate SQL so it works
    identically on the in-memory store, which is what the tests and the demo
    run against. At the scale a queue reaches before someone adds an index
    this is fine; past that it wants a `GROUP BY`, and the shape of the
    answer would not change.
    """

    moment = now or utcnow()
    with database.unit_of_work(tenant_id) as uow:
        rows = list(uow.jobs.all())

    per_kind: dict[str, dict[str, Any]] = {}
    queued = leased = dead = recent = stale = 0
    oldest: float | None = None

    for job in rows:
        bucket = per_kind.setdefault(
            job.kind, {"queued": 0, "leased": 0, "dead": 0, "oldest": None}
        )
        state = job.state

        if state == "queued":
            queued += 1
            bucket["queued"] += 1
            # Age from `run_after`, not `created_at`. A job deliberately
            # scheduled for tomorrow is not late, and counting it as backlog
            # would make every retry look like an outage.
            waiting = (moment - _aware(job.run_after)).total_seconds()
            if waiting > 0:
                oldest = waiting if oldest is None else max(oldest, waiting)
                bucket["oldest"] = (
                    waiting if bucket["oldest"] is None
                    else max(bucket["oldest"], waiting)
                )
        elif state in ("leased", "running"):
            leased += 1
            bucket["leased"] += 1
            if job.lease_until is not None and _aware(job.lease_until) < moment:
                stale += 1
        elif state == "dead":
            dead += 1
            bucket["dead"] += 1
        elif state == "succeeded" and job.finished_at is not None:
            if (moment - _aware(job.finished_at)).total_seconds() <= recent_window_s:
                recent += 1

    notes: list[str] = []
    if stale:
        notes.append(
            f"{stale} lease(s) have expired and not been reaped — a worker "
            f"died, or none is running"
        )
    if dead:
        notes.append(
            f"{dead} job(s) in the dead-letter state; they will not be "
            f"retried without being requeued"
        )
    if oldest is not None and oldest > STALLED_AFTER_S:
        notes.append(
            f"the oldest queued job has been waiting {oldest / 3600:.1f}h"
        )
    if queued and not leased and not recent:
        notes.append(
            "work is queued and nothing has been claimed or finished "
            "recently — check that a worker is running"
        )

    return QueueSnapshot(
        tenant_id=tenant_id,
        at=moment,
        queued=queued,
        leased=leased,
        dead=dead,
        succeeded_recently=recent,
        oldest_queued_s=oldest,
        stale_leases=stale,
        by_kind=tuple(
            KindDepth(
                kind=kind, queued=v["queued"], leased=v["leased"],
                dead=v["dead"], oldest_queued_s=v["oldest"],
            )
            for kind, v in sorted(per_kind.items())
        ),
        notes=tuple(notes),
    )


def requeue_dead(
    database: Any, tenant_id: str, *, kind: str = "", limit: int = 50,
    now: datetime | None = None,
) -> int:
    """Move dead jobs back to `queued`, resetting their attempt count.

    The manual half of a dead-letter queue. Attempts are reset because the
    reason a human is requeueing is that the cause has been fixed — leaving
    the count at its ceiling would send the job straight back to dead on the
    first hiccup.
    """

    moment = now or utcnow()
    moved = 0
    with database.unit_of_work(tenant_id) as uow:
        for job in uow.jobs.all():
            if moved >= limit:
                break
            if job.state != "dead" or (kind and job.kind != kind):
                continue
            job.state = "queued"
            job.attempts = 0
            job.run_after = moment
            job.lease_owner = ""
            job.lease_until = None
            job.finished_at = None
            uow.jobs.save(job)
            moved += 1
    return moved


def _aware(value: datetime) -> datetime:
    """Postgres returns tz-aware datetimes; the in-memory store may not."""
    if value.tzinfo is None:
        from datetime import UTC

        return value.replace(tzinfo=UTC)
    return value
