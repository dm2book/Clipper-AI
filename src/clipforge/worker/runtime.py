"""The loop that turns a queue into work being done.

The queue itself was already here — `jobs` has leases, attempt counting done
in SQL, dedupe keys and `FOR UPDATE SKIP LOCKED` claiming. What was missing
was any process that called it. Nothing drained the queue, so nothing rendered
and nothing published.

## The lease is the crash-recovery mechanism

A claim sets `lease_until`. A background thread extends it while the handler
runs. If this process is killed — OOM, a reclaimed spot instance, `kill -9` —
nothing extends the lease, it expires, and `reap()` returns the job to
`queued` for somebody else. There is no cleanup path that has to run on the
way down, which is the point: a recovery mechanism that depends on the crashed
process doing something is not a recovery mechanism.

The consequence is **at-least-once delivery**, and it is not negotiable: the
alternative is a job that a crash loses for ever. Every handler is therefore
required to be idempotent, and each says how in its own docstring.

## Heartbeating is a thread, not a callback

The obvious design asks handlers to call `heartbeat()` periodically. It fails
the moment a handler blocks in something that cannot be interrupted — ffmpeg
on a 90-second render, a multipart upload, a model loading. Those are exactly
the long jobs whose leases most need extending. So a daemon thread does it on
a timer, and `JobContext.heartbeat` is additionally available for a handler
that wants to beat at a point it chooses.

If a heartbeat comes back False the lease has been lost — another worker owns
this job now. The runtime stops the current job rather than racing: two
workers writing the same render is exactly what leases exist to prevent.

## Shutdown finishes the job in hand

SIGTERM stops the claim loop and lets the running handler finish. A worker
that abandoned work on SIGTERM would make every deploy produce a burst of
reaped jobs and duplicated effort. Only if the grace period expires does the
process exit with the job still leased — and then the lease expiry recovers
it, which is the same path as a crash.

Two signals means "I meant it": the second one exits immediately.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from ..store.records import utcnow
from .types import Disposition, Handler, JobContext, Outcome, WorkerStats

log = logging.getLogger("clipforge.worker")

__all__ = ["WorkerConfig", "Worker", "backoff_seconds"]


#: Base for exponential backoff. Attempt 1 waits ~15s, attempt 5 ~4 minutes,
#: attempt 8 ~32 minutes — which reaches roughly a day of total retry window
#: at the default `max_attempts`, long enough to ride out a platform outage
#: without a job sitting queued for a week.
BACKOFF_BASE_S = 15.0
BACKOFF_CAP_S = 3600.0


def backoff_seconds(attempt: int, *, jitter: float = 0.25) -> float:
    """Exponential backoff with jitter.

    The jitter is the part people skip and then rediscover. Without it, a
    hundred jobs failed by one outage all retry at the same instant, hit the
    recovering service together, and fail together — a thundering herd that
    turns a brief outage into a sustained one.
    """

    raw = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempt - 1)))
    spread = raw * jitter
    return max(1.0, raw + random.uniform(-spread, spread))


@dataclass
class WorkerConfig:
    """How one worker process behaves."""

    #: Which job kinds to claim. Empty means all of them, which is right for a
    #: single-machine deployment and wrong as soon as renders and API calls
    #: want different hardware — a render box should not be holding a lease on
    #: a metrics job while ffmpeg saturates its CPU.
    kinds: tuple[str, ...] = ()
    #: How long a claim is good for before it must be extended.
    lease_s: int = 300
    #: Beat at a third of the lease, so two consecutive failures can be
    #: survived without losing it.
    heartbeat_s: float = 100.0
    #: Sleep when the queue is empty. Short enough to feel responsive, long
    #: enough that an idle fleet is not a self-inflicted database load.
    idle_sleep_s: float = 1.0
    #: Jobs per claim. One keeps latency even across workers; more amortises
    #: the round trip when jobs are tiny.
    batch: int = 1
    #: How often to return expired leases to the queue. Any worker may do it;
    #: it is a cheap, idempotent sweep.
    reap_every_s: float = 30.0
    #: Seconds to let a running handler finish after SIGTERM.
    shutdown_grace_s: float = 60.0
    #: Stop after this many jobs. For tests and for one-shot drains.
    max_jobs: int = 0
    #: Stop after this long. Zero means run for ever.
    max_seconds: float = 0.0
    #: Which tenants to serve. Empty means every tenant with queued work.
    tenants: tuple[str, ...] = ()
    services: Any = None
    name: str = ""

    def owner_id(self) -> str:
        """Identifies the lease holder, and says where to look when it hangs.

        Host and pid, not a bare UUID: the first question about a stuck lease
        is which machine is holding it, and a UUID cannot answer that.
        """

        if self.name:
            return self.name
        return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    """Claims jobs for one or more tenants and runs their handlers."""

    def __init__(
        self,
        database: Any,
        handlers: Mapping[str, Handler],
        config: WorkerConfig | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        tenant_source: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self.database = database
        self.handlers = dict(handlers)
        self.config = config or WorkerConfig()
        self.clock = clock
        self.owner = self.config.owner_id()
        self.stats = WorkerStats()
        #: Where the list of tenants to poll comes from. Injected because a
        #: single-tenant deployment names one, a test names two, and a real
        #: control plane reads them from a table the worker role can see.
        self._tenant_source = tenant_source

        self._stop = threading.Event()
        self._hard_stop = threading.Event()
        self._current: tuple[str, str] | None = None      # (tenant, job id)
        self._last_reap = 0.0
        self._signals_installed = False

    # -- lifecycle ---------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """SIGTERM and SIGINT ask for a graceful stop; a second one insists.

        Only callable from the main thread — Python refuses otherwise — so it
        is a separate call rather than something the constructor does. A test
        that runs a worker in a thread must not install these.
        """

        def handle(signum: int, _frame: Any) -> None:
            if self._stop.is_set():
                log.warning("second signal %s — exiting now", signum)
                self._hard_stop.set()
                raise SystemExit(1)
            log.info("signal %s — finishing the job in hand", signum)
            self._stop.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, handle)
        self._signals_installed = True

    def stop(self) -> None:
        """Ask the loop to finish the current job and return."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- the loop ----------------------------------------------------------

    def run(self) -> WorkerStats:
        """Claim and run until told to stop, or until a limit is reached."""

        self.stats.started_at = self.clock()
        started = time.monotonic()
        log.info("worker %s started; kinds=%s", self.owner,
                 ",".join(self.config.kinds) or "all")

        while not self._stop.is_set():
            if self.config.max_jobs and self.stats.claimed >= self.config.max_jobs:
                break
            if self.config.max_seconds and (
                time.monotonic() - started >= self.config.max_seconds
            ):
                break

            self._maybe_reap()
            worked = self.poll_once()
            if not worked and not self._stop.is_set():
                # `wait` rather than `sleep`: a stop signal during the idle
                # pause should be acted on immediately, not up to a second
                # later. On a rolling deploy that difference is the whole
                # shutdown budget.
                self._stop.wait(self.config.idle_sleep_s)

        log.info("worker %s stopped: %s", self.owner, self.stats.to_dict())
        return self.stats

    def poll_once(self) -> bool:
        """One claim-and-run cycle. Returns True if anything was done."""

        did_work = False
        for tenant_id in self._tenants():
            if self._stop.is_set():
                break
            for job in self._claim(tenant_id):
                did_work = True
                self._run_job(tenant_id, job)
                if self._stop.is_set():
                    break
        return did_work

    def _tenants(self) -> Sequence[str]:
        if self.config.tenants:
            return self.config.tenants
        if self._tenant_source is not None:
            return list(self._tenant_source())
        return ()

    def _claim(self, tenant_id: str) -> list[Any]:
        try:
            with self.database.unit_of_work(tenant_id) as uow:
                claimed = list(uow.jobs.claim(
                    owner=self.owner,
                    now=self.clock(),
                    lease_s=self.config.lease_s,
                    kinds=self.config.kinds,
                    limit=self.config.batch,
                ))
        except Exception:                                   # noqa: BLE001
            # A database blip must not kill the worker. The loop sleeps and
            # tries again; a crash here would take the whole fleet down during
            # a failover that the pool would otherwise ride out.
            log.exception("claim failed for tenant %s", tenant_id)
            return []
        self.stats.claimed += len(claimed)
        return claimed

    # -- running one job ---------------------------------------------------

    def _run_job(self, tenant_id: str, job: Any) -> None:
        handler = self.handlers.get(job.kind)
        started = time.monotonic()
        self._current = (tenant_id, job.id)

        if handler is None:
            # Not this worker's kind, and nobody else claimed it either.
            # Releasing is better than holding: some other deployment may have
            # the handler, and a job nobody can run should sit visible in the
            # queue rather than churn one worker's lease for ever.
            log.warning("no handler for kind %r; releasing job %s",
                        job.kind, job.id)
            self._release(tenant_id, job, f"no handler for {job.kind}")
            self._current = None
            return

        beat = _Heartbeat(self, tenant_id, job)
        context = JobContext(
            job=job,
            tenant_id=tenant_id,
            database=self.database,
            services=self.config.services,
            heartbeat=beat.once,
            attempt=job.attempts + 1,
            max_attempts=job.max_attempts,
            logger=log.getChild(job.kind),
        )

        beat.start()
        try:
            outcome = handler(context)
            if not isinstance(outcome, Outcome):            # pragma: no cover
                outcome = Outcome(
                    Disposition.DONE, f"handler returned {type(outcome).__name__}"
                )
        except Exception as error:                          # noqa: BLE001
            # Retry rather than dead: bounded by max_attempts, and the cost of
            # killing a transient failure is a lost clip.
            log.exception("job %s (%s) raised", job.id, job.kind)
            outcome = Outcome(
                Disposition.RETRY, f"{type(error).__name__}: {error}"[:500]
            )
        finally:
            beat.stop()

        elapsed = time.monotonic() - started
        if beat.lost:
            # The lease was taken while this ran. Whatever happened, it is not
            # this worker's job to record — another worker owns the row and
            # writing to it would clobber their result.
            log.warning("lost the lease on %s mid-flight; not recording",
                        job.id)
            self._current = None
            return

        self._record(tenant_id, job, outcome, elapsed)
        self._current = None

    def _record(
        self, tenant_id: str, job: Any, outcome: Outcome, elapsed: float,
    ) -> None:
        now = self.clock()
        try:
            with self.database.unit_of_work(tenant_id) as uow:
                if outcome.disposition is Disposition.DONE:
                    result = dict(outcome.result or {})
                    if outcome.detail:
                        result.setdefault("detail", outcome.detail)
                    result.setdefault("elapsed_s", round(elapsed, 3))
                    uow.jobs.succeed(job.id, result, now)
                elif outcome.disposition is Disposition.RETRY:
                    delay = outcome.after_s or backoff_seconds(job.attempts + 1)
                    uow.jobs.fail(
                        job.id, outcome.detail or "retrying",
                        now + timedelta(seconds=delay), now,
                    )
                else:
                    # `retry_at=None` is what the store reads as "dead now",
                    # regardless of how many attempts are left.
                    uow.jobs.fail(job.id, outcome.detail or "fatal", None, now)
        except Exception:                                   # noqa: BLE001
            # The work may well have succeeded; only the bookkeeping failed.
            # The lease expires and the job is retried, which is why handlers
            # must be idempotent — this is the exact path that makes it matter.
            log.exception("could not record the outcome of %s", job.id)
            return

        self.stats.record(job.kind, outcome, elapsed, now)
        if outcome.disposition is Disposition.FATAL or (
            job.attempts + 1 >= job.max_attempts
            and outcome.disposition is Disposition.RETRY
        ):
            log.error("job %s (%s) is dead: %s", job.id, job.kind,
                      outcome.detail)

    def _release(self, tenant_id: str, job: Any, reason: str) -> None:
        """Put a job back without counting an attempt against it."""
        try:
            with self.database.unit_of_work(tenant_id) as uow:
                held = uow.jobs.get(job.id)
                if held is None:
                    return
                held.state = "queued"
                held.lease_owner = ""
                held.lease_until = None
                held.last_error = reason
                uow.jobs.save(held)
        except Exception:                                   # noqa: BLE001
            log.exception("could not release job %s", job.id)

    # -- reaping -----------------------------------------------------------

    def _maybe_reap(self) -> None:
        now = time.monotonic()
        if now - self._last_reap < self.config.reap_every_s:
            return
        self._last_reap = now
        for tenant_id in self._tenants():
            try:
                with self.database.unit_of_work(tenant_id) as uow:
                    count = uow.jobs.reap(self.clock())
            except Exception:                               # noqa: BLE001
                log.exception("reap failed for tenant %s", tenant_id)
                continue
            if count:
                self.stats.reaped += count
                log.info("returned %d expired lease(s) to the queue for %s",
                         count, tenant_id)

    # -- shutdown ----------------------------------------------------------

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Block until no job is running. True if it went quiet in time."""
        deadline = time.monotonic() + (
            self.config.shutdown_grace_s if timeout is None else timeout
        )
        while self._current is not None:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True


class _Heartbeat:
    """Extends one job's lease on a timer, in a daemon thread.

    Daemon so it can never keep a dying process alive. It is stopped
    explicitly in a `finally`, and if that is somehow missed the interpreter
    exiting takes it with it.
    """

    __slots__ = ("_worker", "_tenant", "_job", "_thread", "_stop", "lost")

    def __init__(self, worker: Worker, tenant_id: str, job: Any) -> None:
        self._worker = worker
        self._tenant = tenant_id
        self._job = job
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set when a beat reports the lease is gone.
        self.lost = False

    def start(self) -> None:
        interval = max(1.0, self._worker.config.heartbeat_s)
        self._thread = threading.Thread(
            target=self._loop, args=(interval,),
            name=f"heartbeat-{self._job.id[:12]}", daemon=True,
        )
        self._thread.start()

    def _loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            if not self.once():
                return

    def once(self) -> bool:
        """One beat. False when the lease is no longer ours."""
        if self.lost:
            return False
        until = self._worker.clock() + timedelta(
            seconds=self._worker.config.lease_s
        )
        try:
            with self._worker.database.unit_of_work(self._tenant) as uow:
                held = uow.jobs.heartbeat(self._job.id, self._worker.owner, until)
        except Exception:                                   # noqa: BLE001
            # A failed beat is not a lost lease — the database may simply be
            # briefly unreachable, and the lease has time left on it. Marking
            # it lost here would abandon work that is still legitimately ours.
            log.warning("heartbeat for %s failed; lease still has time",
                        self._job.id)
            return True
        if not held:
            self.lost = True
        return held

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
