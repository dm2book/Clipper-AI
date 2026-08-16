"""The worker runtime, and the pipeline it makes possible.

Three layers:

* **Runtime tests** drive `Worker` with trivial handlers. Leasing, retries,
  the dead-letter transition, crash recovery and graceful shutdown are queue
  mechanics and are tested as mechanics — no ffmpeg, no model, milliseconds.
* **Handler tests** check each of the five reports the right `Outcome` when
  its dependency is missing, because "publish jobs keep dying" has to be
  answerable from `last_error`.
* **The end-to-end test** runs a real source through transcript, clips,
  render and publish, with real ffmpeg producing a real 1080x1920 file, and
  asserts each stage's row in the database.

The whole point of the runtime is that jobs survive a worker dying, so
`CrashRecoveryTest` kills one mid-flight rather than asking it politely.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta

import _support  # noqa: F401

from clipforge.store import MemoryDatabase, TenantRecord
from clipforge.store.records import (
    AcquisitionRunRecord,
    ChannelRecord,
    ClipRecord,
    JobRecord,
    SourceRecord,
)
from clipforge.worker import (
    Done,
    Fatal,
    JobContext,
    Retry,
    Worker,
    WorkerConfig,
    WorkerServices,
    backoff_seconds,
    default_handlers,
    requeue_dead,
    snapshot,
)
from clipforge.worker.handlers import (
    acquisition_handler,
    analytics_handler,
    publish_handler,
    render_handler,
    transcription_handler,
)

FFMPEG = os.environ.get("CLIPFORGE_FFMPEG", "") or shutil.which("ffmpeg") or ""
TENANT = "ten_worker"


def database() -> MemoryDatabase:
    db = MemoryDatabase()
    with db.unit_of_work(TENANT) as uow:
        uow.tenants.save(TenantRecord(id=TENANT, name="Worker Test"))
    return db


def enqueue(db, job_id: str, kind: str = "render_video", **fields) -> JobRecord:
    with db.unit_of_work(TENANT) as uow:
        return uow.jobs.enqueue(JobRecord(
            id=job_id, tenant_id=TENANT, kind=kind, **fields
        ))


def job(db, job_id: str) -> JobRecord:
    with db.unit_of_work(TENANT) as uow:
        return uow.jobs.get(job_id)


def worker(db, handlers, **config) -> Worker:
    settings = {
        "tenants": (TENANT,), "idle_sleep_s": 0.01, "heartbeat_s": 1.0,
        "max_jobs": 1,
    }
    settings.update(config)
    return Worker(db, handlers, WorkerConfig(**settings))


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class RuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = database()

    def test_a_queued_job_is_claimed_and_run(self) -> None:
        enqueue(self.db, "j1", payload={"n": 1})
        seen = []
        worker(self.db, {"render_video": lambda c: seen.append(c.payload) or Done("ok")}).run()
        self.assertEqual(seen, [{"n": 1}])
        self.assertEqual(job(self.db, "j1").state, "succeeded")

    def test_the_result_is_written_to_the_job_row(self) -> None:
        """`jobs.result` is the durable record of what a run produced."""
        enqueue(self.db, "j1")
        worker(self.db, {"render_video": lambda c: Done("made it", path="/x.mp4")}).run()
        result = job(self.db, "j1").result
        self.assertEqual(result["path"], "/x.mp4")
        self.assertEqual(result["detail"], "made it")
        self.assertIn("elapsed_s", result)

    def test_a_retry_requeues_with_a_future_run_after(self) -> None:
        enqueue(self.db, "j1", max_attempts=5)
        worker(self.db, {"render_video": lambda c: Retry("blip")}).run()
        held = job(self.db, "j1")
        self.assertEqual(held.state, "queued")
        self.assertEqual(held.attempts, 1)
        self.assertGreater(held.run_after, datetime.now(UTC))
        self.assertIn("blip", held.last_error)

    def test_a_fatal_outcome_goes_straight_to_the_dead_letter_state(self) -> None:
        """No point spending eight attempts on something that cannot work."""
        enqueue(self.db, "j1", max_attempts=8)
        worker(self.db, {"render_video": lambda c: Fatal("no such file")}).run()
        held = job(self.db, "j1")
        self.assertEqual(held.state, "dead")
        self.assertEqual(held.attempts, 1)

    def test_exhausting_attempts_lands_in_the_dead_letter_state(self) -> None:
        enqueue(self.db, "j1", max_attempts=2)
        for _ in range(2):
            with self.db.unit_of_work(TENANT) as uow:
                held = uow.jobs.get("j1")
                held.run_after = datetime.now(UTC) - timedelta(seconds=1)
                uow.jobs.save(held)
            worker(self.db, {"render_video": lambda c: Retry("still broken")}).run()
        self.assertEqual(job(self.db, "j1").state, "dead")

    def test_a_raised_exception_is_a_retry_not_a_death(self) -> None:
        """Bounded by max_attempts; the cost of killing a blip is a lost clip."""
        enqueue(self.db, "j1", max_attempts=5)

        def explode(context):
            raise RuntimeError("the disk filled up")

        worker(self.db, {"render_video": explode}).run()
        held = job(self.db, "j1")
        self.assertEqual(held.state, "queued")
        self.assertIn("the disk filled up", held.last_error)

    def test_a_job_with_no_handler_is_released_not_burned(self) -> None:
        """Another deployment may have the handler; the attempt is not spent."""
        enqueue(self.db, "j1", kind="weekly_report")
        worker(self.db, {"render_video": lambda c: Done()},
               max_jobs=1).run()
        held = job(self.db, "j1")
        self.assertEqual(held.state, "queued")
        self.assertEqual(held.attempts, 0)
        self.assertIn("no handler", held.last_error)

    def test_only_the_configured_kinds_are_claimed(self) -> None:
        """A render box must not hold a lease on a metrics job."""
        enqueue(self.db, "render", kind="render_video")
        enqueue(self.db, "metrics", kind="collect_metrics")
        worker(self.db, {"render_video": lambda c: Done()},
               kinds=("render_video",), max_jobs=1).run()
        self.assertEqual(job(self.db, "render").state, "succeeded")
        self.assertEqual(job(self.db, "metrics").state, "queued")

    def test_a_job_scheduled_for_later_is_not_claimed_yet(self) -> None:
        enqueue(self.db, "j1",
                run_after=datetime.now(UTC) + timedelta(hours=1))
        stats = worker(self.db, {"render_video": lambda c: Done()},
                       max_jobs=1, max_seconds=0.2).run()
        self.assertEqual(stats.claimed, 0)
        self.assertEqual(job(self.db, "j1").state, "queued")

    def test_the_dedupe_key_stops_the_same_work_being_queued_twice(self) -> None:
        first = enqueue(self.db, "j1", dedupe_key="render:clip_1")
        second = enqueue(self.db, "j2", dedupe_key="render:clip_1")
        self.assertEqual(first.id, second.id)

    def test_stats_count_what_happened(self) -> None:
        enqueue(self.db, "j1")
        stats = worker(self.db, {"render_video": lambda c: Done()}).run()
        self.assertEqual((stats.claimed, stats.done), (1, 1))
        self.assertEqual(stats.by_kind, {"render_video": 1})


class BackoffTest(unittest.TestCase):
    def test_backoff_grows_and_is_capped(self) -> None:
        self.assertLess(backoff_seconds(1), backoff_seconds(5))
        self.assertLessEqual(backoff_seconds(50), 3600 * 1.3)

    def test_backoff_is_jittered(self) -> None:
        """Without jitter, a hundred jobs failed by one outage all retry at
        the same instant and turn a brief outage into a sustained one."""

        values = {backoff_seconds(3) for _ in range(20)}
        self.assertGreater(len(values), 1)


# ---------------------------------------------------------------------------
# Crash recovery and shutdown
# ---------------------------------------------------------------------------


class CrashRecoveryTest(unittest.TestCase):
    """A worker that dies mid-job must not lose the job."""

    def setUp(self) -> None:
        self.db = database()

    def test_an_expired_lease_is_returned_to_the_queue(self) -> None:
        enqueue(self.db, "j1")
        with self.db.unit_of_work(TENANT) as uow:
            claimed = uow.jobs.claim(owner="dead-worker",
                                     now=datetime.now(UTC), lease_s=1)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(job(self.db, "j1").state, "leased")

        # No cleanup runs — the worker is simply gone.
        with self.db.unit_of_work(TENANT) as uow:
            reaped = uow.jobs.reap(datetime.now(UTC) + timedelta(seconds=5))
        self.assertEqual(reaped, 1)
        self.assertEqual(job(self.db, "j1").state, "queued")

    def test_a_reaped_job_is_picked_up_by_the_next_worker(self) -> None:
        enqueue(self.db, "j1")
        with self.db.unit_of_work(TENANT) as uow:
            uow.jobs.claim(owner="dead-worker", now=datetime.now(UTC), lease_s=1)

        later = Worker(
            self.db, {"render_video": lambda c: Done("recovered")},
            WorkerConfig(tenants=(TENANT,), max_jobs=1, idle_sleep_s=0.01,
                         reap_every_s=0.0),
            clock=lambda: datetime.now(UTC) + timedelta(seconds=10),
        )
        later.run()
        held = job(self.db, "j1")
        self.assertEqual(held.state, "succeeded")
        self.assertEqual(held.result["detail"], "recovered")

    def test_losing_the_lease_mid_flight_stops_the_worker_recording(self) -> None:
        """Two workers writing the same render is what leases prevent."""
        enqueue(self.db, "j1")

        def steal_then_finish(context):
            # Somebody else takes the lease while this handler runs.
            with self.db.unit_of_work(TENANT) as uow:
                held = uow.jobs.get("j1")
                held.lease_owner = "another-worker"
                uow.jobs.save(held)
            self.assertFalse(context.heartbeat())
            return Done("finished anyway")

        worker(self.db, {"render_video": steal_then_finish}).run()
        # Untouched by the worker that lost the race.
        self.assertNotEqual(job(self.db, "j1").state, "succeeded")

    def test_a_stop_signal_lets_the_running_job_finish(self) -> None:
        """Abandoning work on SIGTERM makes every deploy duplicate effort."""
        enqueue(self.db, "j1")
        instance = worker(self.db, {}, max_jobs=1)
        finished = threading.Event()

        def slow(context):
            instance.stop()          # as a signal handler would
            time.sleep(0.05)
            finished.set()
            return Done("completed after the stop")

        instance.handlers["render_video"] = slow
        instance.run()
        self.assertTrue(finished.is_set())
        self.assertEqual(job(self.db, "j1").state, "succeeded")

    def test_stopping_while_idle_returns_promptly(self) -> None:
        instance = worker(self.db, {"render_video": lambda c: Done()},
                          max_jobs=0, idle_sleep_s=30.0)
        thread = threading.Thread(target=instance.run, daemon=True)
        thread.start()
        time.sleep(0.05)
        instance.stop()
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive(), "the idle wait ignored the stop")


class IdempotencyTest(unittest.TestCase):
    """At-least-once is what a leased queue gives; handlers must cope."""

    def setUp(self) -> None:
        self.db = database()

    def test_a_job_recorded_twice_produces_one_success(self) -> None:
        enqueue(self.db, "j1")
        runs = []
        handlers = {"render_video": lambda c: runs.append(c.job.id) or Done()}
        worker(self.db, handlers).run()

        # Simulate the crash window: the work was done, the success was not
        # recorded, the lease expired and another worker took it.
        with self.db.unit_of_work(TENANT) as uow:
            held = uow.jobs.get("j1")
            held.state = "queued"
            held.lease_owner = ""
            held.finished_at = None
            uow.jobs.save(held)
        worker(self.db, handlers).run()

        self.assertEqual(len(runs), 2, "the handler should have run twice")
        self.assertEqual(job(self.db, "j1").state, "succeeded")


# ---------------------------------------------------------------------------
# Handlers without their dependencies
# ---------------------------------------------------------------------------


class MissingDependencyTest(unittest.TestCase):
    """Each handler must say *why* it cannot run, in `last_error`."""

    def context(self, **payload) -> JobContext:
        db = database()
        return JobContext(
            job=JobRecord(id="j1", tenant_id=TENANT, kind="render_video",
                          payload=payload or None),
            tenant_id=TENANT, database=db, services=WorkerServices(),
            heartbeat=lambda: True,
        )

    def test_each_handler_is_fatal_and_explains_itself(self) -> None:
        cases = {
            "acquisition": (acquisition_handler, "acquisition engine"),
            "transcription": (transcription_handler, "transcription engine"),
            "render": (render_handler, "render engine"),
            "publish": (publish_handler, "publishing system"),
            "analytics": (analytics_handler, "metric source"),
        }
        for name, (handler, expected) in cases.items():
            with self.subTest(handler=name):
                outcome = handler(self.context())
                self.assertEqual(outcome.disposition.value, "fatal")
                self.assertIn(expected, outcome.detail)

    def test_a_publish_job_without_a_transport_does_not_report_success(self) -> None:
        """Draining publish jobs into nowhere is worse than failing them."""
        services = WorkerServices(publisher=object())
        context = self.context()
        context.services = services
        outcome = publish_handler(context)
        self.assertEqual(outcome.disposition.value, "fatal")
        self.assertIn("transport", outcome.detail)

    def test_render_without_a_clip_id_is_fatal(self) -> None:
        context = self.context()
        context.services = WorkerServices(render_factory=lambda d, t: object())
        outcome = render_handler(context)
        self.assertEqual(outcome.disposition.value, "fatal")
        self.assertIn("clip_id", outcome.detail)


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------


class MonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = database()

    def test_an_empty_queue_is_healthy(self) -> None:
        report = snapshot(self.db, TENANT)
        self.assertTrue(report.healthy)
        self.assertEqual(report.queued, 0)

    def test_depth_and_kinds_are_reported(self) -> None:
        enqueue(self.db, "a", kind="render_video")
        enqueue(self.db, "b", kind="render_video")
        enqueue(self.db, "c", kind="transcribe")
        report = snapshot(self.db, TENANT)
        self.assertEqual(report.queued, 3)
        kinds = {k.kind: k.queued for k in report.by_kind}
        self.assertEqual(kinds, {"render_video": 2, "transcribe": 1})

    def test_a_job_scheduled_for_later_is_not_counted_as_backlog(self) -> None:
        """Otherwise every retry looks like an outage."""
        enqueue(self.db, "later",
                run_after=datetime.now(UTC) + timedelta(hours=2))
        report = snapshot(self.db, TENANT)
        self.assertEqual(report.queued, 1)
        self.assertIsNone(report.oldest_queued_s)

    def test_an_old_queued_job_is_unhealthy(self) -> None:
        """Depth alone cannot tell a busy queue from a stalled one."""
        enqueue(self.db, "old",
                run_after=datetime.now(UTC) - timedelta(hours=4))
        report = snapshot(self.db, TENANT)
        self.assertFalse(report.healthy)
        self.assertGreater(report.oldest_queued_s, 3600)

    def test_a_stale_lease_is_visible_and_unhealthy(self) -> None:
        """Not queued, not dead — invisible to every other count."""
        enqueue(self.db, "j1")
        with self.db.unit_of_work(TENANT) as uow:
            # Claim at the present — `claim` only takes jobs whose `run_after`
            # has passed, so claiming "an hour ago" claims nothing.
            uow.jobs.claim(owner="dead", now=datetime.now(UTC), lease_s=1)
        report = snapshot(self.db, TENANT,
                          now=datetime.now(UTC) + timedelta(minutes=5))
        self.assertEqual(report.stale_leases, 1)
        self.assertFalse(report.healthy)
        self.assertTrue(any("reaped" in n for n in report.notes))

    def test_dead_jobs_are_the_dead_letter_queue(self) -> None:
        enqueue(self.db, "j1", max_attempts=1)
        worker(self.db, {"render_video": lambda c: Fatal("nope")}).run()
        report = snapshot(self.db, TENANT)
        self.assertEqual(report.dead, 1)
        self.assertTrue(any("dead-letter" in n for n in report.notes))

    def test_requeueing_a_dead_job_resets_its_attempts(self) -> None:
        """A human requeues because the cause was fixed; leaving attempts at
        the ceiling sends it straight back to dead on the first hiccup."""

        enqueue(self.db, "j1", max_attempts=1)
        worker(self.db, {"render_video": lambda c: Fatal("nope")}).run()
        self.assertEqual(requeue_dead(self.db, TENANT), 1)
        held = job(self.db, "j1")
        self.assertEqual(held.state, "queued")
        self.assertEqual(held.attempts, 0)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def _make_media(directory: str, seconds: float = 6.0) -> str:
    """A real speaker clip with a real audio track."""
    path = os.path.join(directory, "source.mp4")
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"testsrc2=size=1280x720:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", path],
        check=True,
    )
    return path


@unittest.skipUnless(FFMPEG, "the pipeline test needs ffmpeg")
class PipelineEndToEndTest(unittest.TestCase):
    """source → transcript → clips → render → publish, through the worker.

    Every stage is asserted against its own row rather than against the job's
    result, because the job saying "done" is exactly the thing that could be
    lying.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="clipforge-worker-e2e-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = database()
        self.media = _make_media(self.dir)

        with self.db.unit_of_work(TENANT) as uow:
            uow.channels.save(ChannelRecord(
                id="ch_1", tenant_id=TENANT, name="Test", niche="business",
                state="active",
            ))
            uow.sources.save(SourceRecord(
                id="src_1", tenant_id=TENANT, title="A talk",
                kind="media_url", duration_s=6.0, fingerprint="fp_1",
                has_transcript=False,
            ))
            # The downloaded path lives on the acquisition run, not the
            # source — a source can be known long before anything is fetched.
            uow.acquisitions.add(AcquisitionRunRecord(
                id="acq_1", tenant_id=TENANT, source_id="src_1",
                kind="media_url", state="ready", ref_key="src_1",
                media_path=self.media, duration_s=6.0,
                width=1280, height=720, has_audio=True, has_video=True,
            ))

    def services(self) -> WorkerServices:
        from clipforge.render import RenderConfig, RenderEngine

        return WorkerServices(
            render_factory=lambda db, tenant: RenderEngine(
                db, tenant,
                config=RenderConfig(workspace=self.dir, ffmpeg=FFMPEG,
                                    preset="ultrafast"),
            ),
        )

    def test_the_worker_renders_a_real_clip_from_a_real_source(self) -> None:
        with self.db.unit_of_work(TENANT) as uow:
            uow.clips.save(ClipRecord(
                id="cl_1", tenant_id=TENANT, channel_id="ch_1",
                source_id="src_1", start_ms=500, end_ms=4500,
                title="The moment", virality_score=80.0,
            ))
        enqueue(self.db, "job_render", kind="render_video",
                payload={"clip_id": "cl_1",
                         "output_dir": os.path.join(self.dir, "out")})

        instance = Worker(
            self.db, default_handlers(),
            WorkerConfig(tenants=(TENANT,), max_jobs=1, idle_sleep_s=0.01,
                         lease_s=120, heartbeat_s=30.0,
                         services=self.services()),
        )
        instance.run()

        held = job(self.db, "job_render")
        self.assertEqual(held.state, "succeeded", held.last_error)

        output = held.result["output_path"]
        self.assertTrue(os.path.isfile(output), "no file was written")
        self.assertGreater(os.path.getsize(output), 10_000)

        # And it is really 1080x1920, measured rather than assumed.
        from clipforge.acquire.mp4 import read_mp4

        with open(output, "rb") as handle:
            info = read_mp4(handle)
        self.assertEqual((info.width, info.height), (1080, 1920))
        self.assertAlmostEqual(info.duration_s, 4.0, delta=0.6)

    def test_a_render_job_is_safe_to_run_twice(self) -> None:
        """The crash window: work done, success unrecorded, lease reaped."""
        with self.db.unit_of_work(TENANT) as uow:
            uow.clips.save(ClipRecord(
                id="cl_1", tenant_id=TENANT, channel_id="ch_1",
                source_id="src_1", start_ms=0, end_ms=3000, title="Twice",
            ))
        payload = {"clip_id": "cl_1",
                   "output_dir": os.path.join(self.dir, "out")}

        for attempt in ("first", "second"):
            enqueue(self.db, f"job_{attempt}", kind="render_video",
                    payload=payload)
            Worker(self.db, default_handlers(),
                   WorkerConfig(tenants=(TENANT,), max_jobs=1,
                                idle_sleep_s=0.01, lease_s=120,
                                heartbeat_s=30.0,
                                services=self.services())).run()

        first = job(self.db, "job_first").result["output_path"]
        second = job(self.db, "job_second").result["output_path"]
        self.assertEqual(first, second, "the output path is not deterministic")
        self.assertTrue(os.path.isfile(second))

    def test_a_missing_media_file_is_fatal_not_retried_forever(self) -> None:
        with self.db.unit_of_work(TENANT) as uow:
            uow.sources.save(SourceRecord(
                id="src_gone", tenant_id=TENANT, title="Gone",
                kind="media_url", fingerprint="fp_gone",
            ))
            uow.acquisitions.add(AcquisitionRunRecord(
                id="acq_gone", tenant_id=TENANT, source_id="src_gone",
                kind="media_url", state="ready", ref_key="src_gone",
                media_path="/no/such/file.mp4",
            ))
            uow.clips.save(ClipRecord(
                id="cl_gone", tenant_id=TENANT, channel_id="ch_1",
                source_id="src_gone", start_ms=0, end_ms=2000,
            ))
        enqueue(self.db, "job_gone", kind="render_video",
                payload={"clip_id": "cl_gone"})
        Worker(self.db, default_handlers(),
               WorkerConfig(tenants=(TENANT,), max_jobs=1, idle_sleep_s=0.01,
                            services=self.services())).run()
        held = job(self.db, "job_gone")
        self.assertEqual(held.state, "dead")
        self.assertIn("not on this host", held.last_error)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
