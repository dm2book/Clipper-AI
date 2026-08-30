"""What a handler is handed, and what it may answer.

## Three outcomes, not two

A handler returns `Done`, `Retry` or `Fatal`, and the distinction between the
last two is the whole reason this is an enum rather than a bool. A transcoder
that fell over because the disk filled should come back in ninety seconds; a
transcoder that was handed a URL returning 404 should never come back at all.
Collapsing them means either retrying permanent failures eight times — burning
a paid API call each round — or killing transient ones on the first blip.

An exception that escapes a handler is treated as `Retry`. That is the safer
default of the two: the cost of retrying something permanent is bounded by
`max_attempts`, and the cost of killing something transient is a lost clip.

## Idempotency is the handler's problem, and it is not optional

A leased queue is at-least-once. A worker can finish the work, die before
recording success, and have the lease reaped and the job handed to somebody
else. Every handler in this package therefore has to be safe to run twice, and
each one says in its own docstring *how* — usually a unique index or a
state check, never "it probably will not happen".

## A handler also says what happens next

`Done(..., follow_on=[...])` carries the jobs that this one's success makes
possible, and the runtime queues them **in the same transaction** that marks
this job succeeded. That is the difference between a pipeline and a pile of
stages: before it, a render finished and nothing published, because the only
thing that could queue the publish was a person.

Atomicity matters more than it looks here. Marking a job done and queueing its
successor in two transactions leaves a window between them, and a crash inside
that window is a chain that stops with every row it left behind saying
"succeeded". One transaction means both facts are true or neither is, and
"neither" is a retry the queue already knows how to do.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence

__all__ = [
    "Outcome",
    "Disposition",
    "Done",
    "Retry",
    "Fatal",
    "JobContext",
    "JobSpec",
    "Handler",
    "WorkerStats",
]


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A job a handler wants queued once it has succeeded.

    Not a `JobRecord`: a handler has no business minting ids or knowing which
    tenant it is serving, and the runtime knows both. This is the intent —
    what kind of work, over what payload — and `chain.enqueue` turns it into a
    row.

    `dedupe_key` is the field that must not be left empty. It is what makes a
    chain safe to re-run: a render job retried after a crash chains to the
    publish job that is already queued rather than a second one, because the
    key resolves to the existing row. `chain` derives every key in one place
    for exactly that reason.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""
    #: Seconds before the successor becomes claimable. Verification uses it:
    #: asking YouTube about a video it is still transcoding gets a truthful
    #: "processing" and tells you nothing.
    delay_s: float = 0.0
    priority: int = 100
    channel_id: str = ""
    max_attempts: int = 8


class Disposition(str, enum.Enum):
    DONE = "done"
    RETRY = "retry"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened, and what should happen next."""

    disposition: Disposition
    #: Recorded on the job row. Keep it short and specific — this is what
    #: somebody reads at 3am, and "error" tells them nothing.
    detail: str = ""
    #: Written to `jobs.result`. Must be JSON-serialisable.
    result: dict[str, Any] | None = None
    #: Seconds to wait before the next attempt. Only read for `RETRY`; zero
    #: means "use the runtime's backoff", which is almost always right.
    after_s: float = 0.0
    #: What this job's success makes possible. Queued by the runtime in the
    #: same transaction that records the success, and only on `DONE` — a
    #: failed render must not launch a publish of the file it did not write.
    follow_on: tuple[JobSpec, ...] = ()

    @property
    def ok(self) -> bool:
        return self.disposition is Disposition.DONE


def Done(                                                  # noqa: N802
    detail: str = "",
    *,
    follow_on: Sequence[JobSpec] = (),
    **result: Any,
) -> Outcome:
    return Outcome(
        Disposition.DONE, detail, result or None, 0.0, tuple(follow_on)
    )


def Retry(detail: str, after_s: float = 0.0) -> Outcome:   # noqa: N802
    return Outcome(Disposition.RETRY, detail, None, after_s)


def Fatal(detail: str) -> Outcome:                         # noqa: N802
    """Do not try again. The same input fails identically for ever."""
    return Outcome(Disposition.FATAL, detail)


@dataclass(slots=True)
class JobContext:
    """One job, plus the handles a handler needs to do it.

    `heartbeat()` is on the context rather than left to the runtime because
    only the handler knows where its long operations are. The runtime beats
    on a timer as well; this is for a handler that wants to say "still alive"
    at a point it chooses, such as between ffmpeg passes.
    """

    job: Any
    tenant_id: str
    database: Any
    #: Whatever `WorkerConfig.services` carried — storage, a transport, an
    #: email sender. Typed loosely so a handler declares its own needs rather
    #: than this module knowing about all of them.
    services: Any = None
    #: Extends the lease. Returns False when the lease has already been lost,
    #: which means another worker owns this job now and this one should stop.
    heartbeat: Any = None
    attempt: int = 1
    max_attempts: int = 8
    logger: Any = None

    @property
    def payload(self) -> dict[str, Any]:
        return dict(getattr(self.job, "payload", None) or {})

    @property
    def last_attempt(self) -> bool:
        return self.attempt >= self.max_attempts

    def unit_of_work(self):
        return self.database.unit_of_work(self.tenant_id)


class Handler(Protocol):
    """One kind of job.

    Deliberately a plain callable rather than a class hierarchy: a handler is
    a function of (context) -> Outcome, and everything a base class would have
    provided — leasing, retries, logging — belongs to the runtime, which is
    the only place it can be got right once.
    """

    def __call__(self, context: JobContext) -> Outcome: ...


@dataclass(slots=True)
class WorkerStats:
    """Counters for one process since it started.

    Reset on restart, which is correct for what they are: a scrape target,
    not a ledger. The durable record of what happened is `jobs` itself.
    """

    claimed: int = 0
    done: int = 0
    retried: int = 0
    dead: int = 0
    reaped: int = 0
    #: Successors queued by finished jobs. A chain that has stopped shows up
    #: here as a flat line while `done` keeps climbing.
    chained: int = 0
    #: Wall time inside handlers, so "slow" can be told from "idle".
    busy_s: float = 0.0
    started_at: datetime | None = None
    last_job_at: datetime | None = None
    by_kind: dict[str, int] = field(default_factory=dict)

    def record(self, kind: str, outcome: Outcome, elapsed_s: float,
               now: datetime) -> None:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        self.busy_s += elapsed_s
        self.last_job_at = now
        if outcome.disposition is Disposition.DONE:
            self.done += 1
        elif outcome.disposition is Disposition.RETRY:
            self.retried += 1
        else:
            self.dead += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "done": self.done,
            "retried": self.retried,
            "dead": self.dead,
            "reaped": self.reaped,
            "chained": self.chained,
            "busy_s": round(self.busy_s, 3),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_job_at": (
                self.last_job_at.isoformat() if self.last_job_at else None
            ),
            "by_kind": dict(self.by_kind),
        }
