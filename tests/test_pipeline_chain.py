"""The pipeline, end to end, with nobody queuing anything by hand.

Before this, every stage worked and the pipeline did not: acquisition wrote a
source and stopped, transcription wrote a transcript and stopped, clip
selection existed only inside one process's memory, and a rendered file
pointed at nothing that would publish it. The stages were connected by a
person opening a queue console.

So the assertion that matters in this file is negative. Each test enqueues
**one** job and then never touches the queue again; everything afterwards —
the clip row, the booked upload, the render, the publish, the read-back — has
to arrive because a handler queued its own successor. A test that had to
enqueue a second job would be proving the opposite of what it claims.

Three layers, for the same reason `test_worker.py` has three:

* `ChainMechanicsTest` drives the runtime with trivial handlers. Whether a
  successor is queued, deduplicated, delayed or suppressed is queue mechanics
  and is tested as mechanics — no ffmpeg, milliseconds.
* The stage tests each drive one handler against a real database and assert
  the row it wrote and the job it queued.
* `WholeChainTest` runs a real transcript through a real ffmpeg render to a
  scripted TikTok and back, and asserts every row along the way.
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

from clipforge.captions.types import TimedWord
from clipforge.factory import DEFAULT_ACCEPTED_RIGHTS
from clipforge.gameplay import Game, GameplayAsset
from clipforge.publish import Account, Platform, PublishConfig, TokenSet
from clipforge.publish.sealing import Sealer
from clipforge.publish.types import PostState, Response
from clipforge.store import MemoryDatabase, TenantRecord
from clipforge.store.durable import DurableAccountBook, DurableTokenStore
from clipforge.store.records import (
    AcquisitionRunRecord,
    ChannelRecord,
    JobRecord,
    ProjectRecord,
    SourceRecord,
    TranscriptionRunRecord,
)
from clipforge.transcribe.types import ProviderInfo, Segment, Transcript, Word
from clipforge.worker import (
    Done,
    Fatal,
    Retry,
    Worker,
    WorkerConfig,
    WorkerServices,
    chain,
    default_handlers,
    snapshot,
)
from clipforge.worker.handlers import (
    publish_handler,
    verification_handler,
)
from clipforge.worker.selection import selection_handler
from clipforge.worker.services import durable_publisher_factory

FFMPEG = os.environ.get("CLIPFORGE_FFMPEG", "") or shutil.which("ffmpeg") or ""

TENANT = "ten_chain"
PROJECT = "prj_chain"
CHANNEL = "ch_chain"
SOURCE = "src_chain"
ACCOUNT = "acc_tiktok"
KEY = b"k" * 32


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Deliberately the shape the viral engine is built to find: a concrete
#: reversal with numbers in it. A transcript of filler produces no moment above
#: the quality floor, and then the chain would be untested rather than broken.
MOMENT_TEXT = (
    "The raise was the mistake. We went from twelve people to ninety in "
    "seven months and we almost went bankrupt doing it. We burned fourteen "
    "million dollars in nineteen months and had almost nothing to show for "
    "it. Nobody tells you that headcount is not progress. I confused the two "
    "for two years and it nearly killed the company. The day we cut back to "
    "thirty people was the day the business started working again."
)
FILLER = "I do not have a strong view on that one way or the other. "


def _words(text: str, start_s: float = 0.0) -> list[Word]:
    """Word timings at a plausible speaking rate."""

    out: list[Word] = []
    cursor = start_s
    for raw in text.split():
        span = 0.24 + len(raw) * 0.022
        out.append(Word(raw, cursor, cursor + span))
        cursor += span + (0.42 if raw.endswith((".", "?", "!")) else 0.045)
    return out


def transcript_fixture() -> Transcript:
    words = _words(FILLER * 2 + MOMENT_TEXT + " " + FILLER * 2)
    return Transcript(
        text=" ".join(w.text for w in words),
        segments=(Segment(
            text=" ".join(w.text for w in words),
            start_s=words[0].start_s, end_s=words[-1].end_s, words=tuple(words),
        ),),
        words=tuple(words),
        language="en",
        duration_s=words[-1].end_s,
        provider=ProviderInfo(name="fixture", model="fixture"),
    )


def timed_words(transcript: Transcript) -> list[TimedWord]:
    from clipforge.transcribe.pipeline import to_timed_words

    return to_timed_words(transcript)


def make_media(directory: str, seconds: float, name: str = "source.mp4",
               size: str = "1280x720") -> str:
    path = os.path.join(directory, name)
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"testsrc2=size={size}:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
         # `-crf 34` and 24fps because these are fixtures, not output: at the
         # defaults `testsrc2` encodes to tens of megabytes and the fixture
         # costs more than the render under test.
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "34",
         "-r", "24", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", path],
        check=True,
    )
    return path


class TikTokTransport:
    """Answers TikTok's direct-post protocol, however many chunks it takes.

    A scripted response list would have to know the chunk count, which depends
    on the size of a file ffmpeg produced — so the test would break whenever
    the encoder got better. This answers by endpoint instead.
    """

    def __init__(self, *, status: str = "PUBLISH_COMPLETE",
                 fail_reason: str = "") -> None:
        self.sent: list = []
        self.status = status
        self.fail_reason = fail_reason
        #: What the *verification* read reports, once it happens. Separate
        #: from `status` because the interesting case is a publish that
        #: succeeded and a video that later did not survive moderation.
        self.verify_status = ""

    def send(self, request):
        self.sent.append(request)
        url = request.url
        if "video/init" in url:
            return Response(200, {}, {"data": {
                "publish_id": "pub_1", "upload_url": "https://up/x",
            }})
        if "status/fetch" in url:
            # Publishing polls the same endpoint verification reads, so the
            # two are told apart by the adapter's own description rather than
            # by call order — otherwise a scripted late rejection would fire
            # during the upload instead of after it.
            verifying = "verify" in (request.description or "")
            status = (
                self.verify_status if (verifying and self.verify_status)
                else self.status
            )
            body = {"status": status}
            if status == "PUBLISH_COMPLETE":
                body["publicaly_available_post_id"] = ["v_9"]
            if self.fail_reason:
                body["fail_reason"] = self.fail_reason
            return Response(200, {}, {"data": body})
        return Response(200)

    @property
    def verified(self) -> bool:
        return any("verify" in (r.description or "") for r in self.sent)


class Fixture(unittest.TestCase):
    """A tenant with one channel, one source and one connected account."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="clipforge-chain-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = MemoryDatabase()
        #: Where the gameplay bed's file is, once a test has made one. The
        #: business niche wants a bed and selection blocks without one, so the
        #: library is always present; only rendering needs the bytes.
        self.bed_path = ""
        self.transcript = transcript_fixture()
        self.duration = self.transcript.duration_s

        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Chain Co"))
            uow.projects.save(ProjectRecord(
                id=PROJECT, tenant_id=TENANT, name="Brand"))
            uow.channels.save(ChannelRecord(
                id=CHANNEL, tenant_id=TENANT, project_id=PROJECT,
                name="Business", niche="business", state="active",
                # A channel row created by hand accepts no rights basis at
                # all, and then every source is blocked at the gate. The
                # factory sets these when it creates one; the fixture has to.
                accepted_rights=[r.value for r in DEFAULT_ACCEPTED_RIGHTS],
                topics=["business", "startups"],
            ))
            uow.sources.save(SourceRecord(
                id=SOURCE, tenant_id=TENANT, title="A founder talk",
                kind="podcast", duration_s=self.duration,
                fingerprint="fp_chain", rights_basis="owned",
                rights_reference="first-party",
                rights_verified_at=datetime.now(UTC) - timedelta(days=1),
                topics=["business", "startups"], has_transcript=False,
            ))

        self.tokens = DurableTokenStore(
            self.db, TENANT, seal=Sealer(KEY).seal, unseal=Sealer(KEY).unseal,
        )
        accounts = DurableAccountBook(self.db, TENANT, channel_id=CHANNEL)
        accounts[ACCOUNT] = Account(
            account_id=ACCOUNT, platform=Platform.TIKTOK, org_id=TENANT,
            handle="@founder", direct_post_approved=True,
        )
        now = datetime.now(UTC)
        self.tokens.put(TokenSet(
            account_id=ACCOUNT, platform=Platform.TIKTOK,
            access_token="at-secret", refresh_token="rt-secret",
            expires_at=now + timedelta(hours=6),
            refresh_valid_until=now + timedelta(days=300),
            scopes=("video.publish",), obtained_at=now,
        ))

    # -- helpers -----------------------------------------------------------

    def store_transcript(self) -> None:
        with self.db.unit_of_work(TENANT) as uow:
            uow.transcriptions.add(TranscriptionRunRecord(
                id="trn_1", tenant_id=TENANT, source_id=SOURCE,
                state="succeeded", provider="fixture",
                text=self.transcript.text,
                transcript=self.transcript.to_dict(),
                language="en", word_count=len(self.transcript.words),
                duration_s=self.duration,
            ))
            source = uow.sources.get(SOURCE)
            source.has_transcript = True
            uow.sources.save(source)

    def acquisition(self, media_path: str = "") -> None:
        with self.db.unit_of_work(TENANT) as uow:
            uow.acquisitions.add(AcquisitionRunRecord(
                id="acq_1", tenant_id=TENANT, source_id=SOURCE,
                channel_id=CHANNEL, kind="podcast", state="ready",
                ref_key=SOURCE, media_path=media_path,
                duration_s=self.duration, width=1280, height=720,
                has_audio=True, has_video=True,
            ))

    def library(self) -> tuple:
        return (GameplayAsset(
            "bed_sat", Game.SATISFYING, 240.0, 1440, 1440, 30.0,
            path=self.bed_path,
        ),)

    def services(self, transport=None, **overrides) -> WorkerServices:
        settings = dict(
            gameplay_library=self.library(),
            publisher_factory=durable_publisher_factory(
                Sealer(KEY),
                # Spacing is a platform-safety floor measured in hours; a test
                # booking one post cannot trip it, and turning it off would
                # hide the slot search that selection relies on.
                PublishConfig(worker_id="test"),
            ),
            transport=transport,
            # Everything downstream of the render is queued to fire at the
            # slot the calendar chose, so a six-hour lead is a six-hour test.
            lead_time_s=1.0,
            verify_first_s=0.0,
            verify_second_s=0.0,
            metrics_delay_s=0.0,
        )
        settings.update(overrides)
        return WorkerServices(**settings)

    def jobs(self, kind: str = "") -> list:
        with self.db.unit_of_work(TENANT) as uow:
            rows = list(uow.jobs.all())
        return [r for r in rows if not kind or r.kind == kind]

    def uploads(self) -> list:
        with self.db.unit_of_work(TENANT) as uow:
            return list(uow.uploads.all())

    def clips(self) -> list:
        with self.db.unit_of_work(TENANT) as uow:
            return list(uow.clips.all())

    def enqueue(self, kind: str, **payload) -> JobRecord:
        with self.db.unit_of_work(TENANT) as uow:
            return uow.jobs.enqueue(JobRecord(
                id=f"job_seed_{kind}", tenant_id=TENANT, kind=kind,
                payload=payload,
            ))

    def drain(self, services, *, timeout: float = 60.0, until=None,
              kinds: tuple = ()) -> None:
        """Run one worker until the queue goes quiet, or `until` is true.

        Deliberately a real worker on a real thread rather than calling the
        handlers in order: the order is the thing under test, and a test that
        chose it would be asserting its own arrangement.
        """

        worker = Worker(self.db, default_handlers(), WorkerConfig(
            tenants=(TENANT,), kinds=kinds, idle_sleep_s=0.05, lease_s=120,
            heartbeat_s=30.0, reap_every_s=5.0, services=services,
            max_seconds=timeout,
        ))
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + timeout
            quiet = 0
            while time.monotonic() < deadline:
                if until is not None and until():
                    return
                held = snapshot(self.db, TENANT)
                quiet = quiet + 1 if not held.queued and not held.leased else 0
                if quiet >= 3 and until is None:
                    return
                time.sleep(0.1)
        finally:
            worker.stop()
            thread.join(timeout=10)


# ---------------------------------------------------------------------------
# The mechanics
# ---------------------------------------------------------------------------


class ChainMechanicsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Mechanics"))

    def seed(self, kind: str = "render_video", **fields) -> None:
        with self.db.unit_of_work(TENANT) as uow:
            uow.jobs.enqueue(JobRecord(
                id="j1", tenant_id=TENANT, kind=kind, **fields
            ))

    def run_one(self, handler, kind: str = "render_video"):
        worker = Worker(self.db, {kind: handler}, WorkerConfig(
            tenants=(TENANT,), max_jobs=1, idle_sleep_s=0.01, heartbeat_s=1.0,
        ))
        return worker.run()

    def all_jobs(self) -> list:
        with self.db.unit_of_work(TENANT) as uow:
            return list(uow.jobs.all())

    def test_a_successor_is_queued_when_a_job_succeeds(self) -> None:
        self.seed()
        self.run_one(lambda c: Done(
            "ok", follow_on=[chain.publish_spec("up_1", channel_id=CHANNEL)]
        ))
        successors = [j for j in self.all_jobs() if j.kind == "publish_upload"]
        self.assertEqual(len(successors), 1)
        self.assertEqual(successors[0].payload["upload_id"], "up_1")
        self.assertEqual(successors[0].state, "queued")

    def test_the_successor_is_named_on_the_finished_job(self) -> None:
        self.seed()
        self.run_one(lambda c: Done(
            "ok", follow_on=[chain.publish_spec("up_1")]
        ))
        with self.db.unit_of_work(TENANT) as uow:
            done = uow.jobs.get("j1")
            queued = [j for j in uow.jobs.all() if j.kind == "publish_upload"]
        # The link is on the row, so a stalled pipeline can be walked forwards
        # from whichever job last succeeded.
        self.assertEqual(done.result["follow_on"], [queued[0].id])

    def test_a_retry_queues_nothing(self) -> None:
        self.seed()
        self.run_one(lambda c: Retry("later"))
        self.assertEqual([j.kind for j in self.all_jobs()], ["render_video"])

    def test_a_fatal_queues_nothing(self) -> None:
        self.seed()
        self.run_one(lambda c: Fatal("never"))
        self.assertEqual([j.kind for j in self.all_jobs()], ["render_video"])

    def test_the_same_successor_twice_is_one_job(self) -> None:
        """The crash window: work done, success unrecorded, lease reaped."""

        for attempt in ("a", "b"):
            with self.db.unit_of_work(TENANT) as uow:
                uow.jobs.enqueue(JobRecord(
                    id=f"j_{attempt}", tenant_id=TENANT, kind="render_video"
                ))
            worker = Worker(
                self.db,
                {"render_video": lambda c: Done(
                    "ok", follow_on=[chain.publish_spec("up_1")]
                )},
                WorkerConfig(tenants=(TENANT,), max_jobs=1, idle_sleep_s=0.01,
                             heartbeat_s=1.0),
            )
            worker.run()

        successors = [j for j in self.all_jobs() if j.kind == "publish_upload"]
        self.assertEqual(len(successors), 1, "the chain fanned out on a replay")

    def test_a_delayed_successor_is_not_claimable_yet(self) -> None:
        self.seed()
        before = datetime.now(UTC)
        self.run_one(lambda c: Done(
            "ok", follow_on=[chain.verify_spec("up_1", delay_s=900)]
        ))
        verify = [j for j in self.all_jobs() if j.kind == "verify_upload"][0]
        self.assertGreater(verify.run_after, before + timedelta(seconds=800))

    def test_the_two_verification_passes_are_separate_jobs(self) -> None:
        self.seed()
        self.run_one(lambda c: Done("ok", follow_on=[
            chain.verify_spec("up_1", pass_number=1),
            chain.verify_spec("up_1", pass_number=2),
        ]))
        passes = [j for j in self.all_jobs() if j.kind == "verify_upload"]
        self.assertEqual(len(passes), 2)
        self.assertEqual(
            sorted(j.payload["pass_number"] for j in passes), [1, 2]
        )

    def test_chained_successors_are_counted(self) -> None:
        self.seed()
        stats = self.run_one(lambda c: Done("ok", follow_on=[
            chain.publish_spec("up_1"), chain.publish_spec("up_2"),
        ]))
        self.assertEqual(stats.chained, 2)
        self.assertEqual(stats.to_dict()["chained"], 2)

    def test_an_unqueueable_successor_takes_the_success_with_it(self) -> None:
        """Atomicity, from the failing side.

        If the successor cannot be written the predecessor must not be
        recorded done, or the pipeline halts with every row claiming success
        and nothing left to walk forwards from.
        """

        from unittest import mock

        self.seed()
        worker = Worker(
            self.db,
            {"render_video": lambda c: Done(
                "ok", follow_on=[chain.publish_spec("up_1")]
            )},
            WorkerConfig(tenants=(TENANT,), max_jobs=1, idle_sleep_s=0.01,
                         heartbeat_s=1.0),
        )
        with mock.patch.object(
            chain, "enqueue", side_effect=RuntimeError("the queue is full")
        ):
            worker.run()

        held = [j for j in self.all_jobs() if j.id == "j1"][0]
        self.assertNotEqual(held.state, "succeeded")
        # And no half-written successor was left behind.
        self.assertEqual(
            [j.kind for j in self.all_jobs()], ["render_video"]
        )


# ---------------------------------------------------------------------------
# Clip selection
# ---------------------------------------------------------------------------


class SelectionTest(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.store_transcript()
        self.acquisition()

    def select(self, services=None, **payload):
        from clipforge.worker.types import JobContext

        job = JobRecord(id="j_sel", tenant_id=TENANT, kind="detect_clips",
                        payload={"source_id": SOURCE, "channel_id": CHANNEL,
                                 **payload})
        context = JobContext(
            job=job, tenant_id=TENANT, database=self.db,
            services=services or self.services(),
        )
        return selection_handler(context)

    def test_a_clip_row_is_written_with_the_moment_it_chose(self) -> None:
        outcome = self.select()
        self.assertTrue(outcome.ok, outcome.detail)

        clips = self.clips()
        self.assertEqual(len(clips), 1)
        clip = clips[0]
        self.assertEqual(clip.source_id, SOURCE)
        self.assertEqual(clip.channel_id, CHANNEL)
        self.assertGreater(clip.end_ms, clip.start_ms)
        self.assertGreater(clip.virality_score, 0)
        self.assertTrue(clip.hook_text, "no hook was written")
        self.assertTrue(clip.caption_track, "no caption track was written")

    def test_the_post_is_booked_but_not_yet_publishable(self) -> None:
        self.select()
        uploads = self.uploads()
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        # Draft, because the file it points at does not exist yet. This is
        # what stops a publisher claiming a post before its render finishes.
        self.assertEqual(upload.state, PostState.DRAFT.value)
        self.assertEqual(upload.clip_id, self.clips()[0].id)
        self.assertEqual(upload.platform, Platform.TIKTOK.value)

    def test_selection_does_not_queue_the_render_itself(self) -> None:
        """The handler names its successor; the runtime queues it.

        Keeping those apart is what makes the queueing atomic with the
        success — a handler that enqueued directly would commit the successor
        before anything had recorded that this job finished.
        """

        self.select()
        self.assertEqual(self.jobs("render_video"), [])

    def test_the_render_follow_on_names_the_clip(self) -> None:
        outcome = self.select()
        self.assertEqual(len(outcome.follow_on), 1)
        spec = outcome.follow_on[0]
        self.assertEqual(spec.kind, "render_video")
        self.assertEqual(spec.payload["clip_id"], self.clips()[0].id)
        self.assertEqual(spec.dedupe_key, f"render:{self.clips()[0].id}")

    def test_running_it_twice_produces_one_clip_and_the_same_render(self) -> None:
        first = self.select()
        second = self.select()
        self.assertEqual(len(self.clips()), 1)
        self.assertEqual(len(self.uploads()), 1)
        self.assertEqual(
            [s.dedupe_key for s in first.follow_on],
            [s.dedupe_key for s in second.follow_on],
            "a replay must chain to the same render, not a second one",
        )
        self.assertTrue(second.result.get("replayed"))

    def test_the_source_is_marked_used_by_the_channel(self) -> None:
        self.select()
        with self.db.unit_of_work(TENANT) as uow:
            used = uow.sources.used_by(CHANNEL)
        self.assertEqual([u.source_id for u in used], [SOURCE])

    def test_the_channel_is_charged_for_the_work(self) -> None:
        with self.db.unit_of_work(TENANT) as uow:
            before = uow.channels.get(CHANNEL).budget_spent_cents
        self.select()
        with self.db.unit_of_work(TENANT) as uow:
            after = uow.channels.get(CHANNEL)
        self.assertGreater(after.budget_spent_cents, before)
        self.assertEqual(after.total_published, 1)

    def test_no_transcript_is_fatal_rather_than_retried(self) -> None:
        db = MemoryDatabase()
        with db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Empty"))
            uow.channels.save(ChannelRecord(
                id=CHANNEL, tenant_id=TENANT, name="C", niche="business"))
            uow.sources.save(SourceRecord(
                id=SOURCE, tenant_id=TENANT, title="T", kind="podcast",
                fingerprint="fp"))
        from clipforge.worker.types import JobContext

        outcome = selection_handler(JobContext(
            job=JobRecord(id="j", tenant_id=TENANT, kind="detect_clips",
                          payload={"source_id": SOURCE,
                                   "channel_id": CHANNEL}),
            tenant_id=TENANT, database=db, services=self.services(),
        ))
        self.assertEqual(outcome.disposition.value, "fatal")
        self.assertIn("transcription", outcome.detail)

    def test_a_channel_with_no_accounts_is_blocked_not_failed(self) -> None:
        with self.db.unit_of_work(TENANT) as uow:
            uow.accounts.delete(ACCOUNT)
        outcome = self.select()
        # Blocked is the gate working, and must not look like a crash.
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result.get("blocked"))
        self.assertEqual(outcome.follow_on, ())
        self.assertEqual(self.clips(), [])

    def test_a_niche_that_wants_a_bed_is_blocked_without_one(self) -> None:
        """Four of the seven niches composite a gameplay bed.

        A worker with no library produces nothing at all for them — not a
        plainer clip, no clip — so the absence has to be visible rather than
        discovered from a channel that never posts.
        """

        outcome = self.select(services=self.services(gameplay_library=()))
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result.get("blocked"))
        self.assertIn("bed", outcome.result["reason"])
        self.assertEqual(self.clips(), [])

    def test_without_a_publisher_the_clip_and_render_still_happen(self) -> None:
        outcome = self.select(services=self.services(
            publisher_factory=None,
        ))
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(len(self.clips()), 1)
        self.assertEqual(self.uploads(), [])
        self.assertEqual(outcome.follow_on[0].kind, "render_video")
        self.assertIn("no publisher", outcome.detail)


# ---------------------------------------------------------------------------
# Render, publish, verify
# ---------------------------------------------------------------------------


@unittest.skipUnless(FFMPEG, "the chain test needs ffmpeg")
class RenderPromotionTest(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.store_transcript()
        # Long enough to contain whatever window the viral engine picks. A
        # source shorter than the clip renders short and the engine's own
        # output check rejects it — correctly, and unhelpfully as a fixture.
        self.media = make_media(self.dir, self.duration + 2.0)
        # A real bed, because the business niche composites one and handing
        # ffmpeg a plan with a gameplay panel and no footage renders nothing.
        self.bed_path = make_media(self.dir, 30.0, "bed.mp4", "1440x1440")
        self.acquisition(self.media)

    def render_services(self, transport=None) -> WorkerServices:
        from clipforge.render import RenderConfig, RenderEngine

        return self.services(
            transport=transport,
            render_factory=lambda db, tenant: RenderEngine(
                db, tenant,
                config=RenderConfig(workspace=self.dir, ffmpeg=FFMPEG,
                                    preset="ultrafast"),
            ),
        )

    def test_the_render_makes_the_booked_post_publishable(self) -> None:
        """One drain, several assertions, because a render costs a minute.

        Split across three test methods this would encode the same clip three
        times to check three facts about one transition.
        """

        # A ten-minute lead, so the publish job's delay is the real distance
        # to the booked slot rather than zero — with a one-second lead the
        # render itself outlasts the slot and the assertion below would be
        # measuring the clamp instead of the schedule.
        services = self.render_services()
        services.lead_time_s = 600.0

        self.enqueue("detect_clips", source_id=SOURCE, channel_id=CHANNEL)
        self.drain(
            services, timeout=240.0,
            # Selection and render only: this case is about the transition,
            # and letting the publish job run would kill it on a transport
            # that is deliberately absent.
            kinds=("detect_clips", "render_video"),
            until=lambda: bool(
                [u for u in self.uploads() if u.state != PostState.DRAFT.value]
            ),
        )

        uploads = self.uploads()
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertEqual(upload.state, PostState.SCHEDULED.value)
        self.assertTrue(upload.video_id, "the upload was not linked to a video")

        # The spec now points at a file that really exists.
        from clipforge.store.mappers import to_scheduled_post

        asset = to_scheduled_post(upload).spec.asset
        self.assertTrue(os.path.isfile(asset.path), asset.path)
        self.assertGreater(asset.size_bytes, 10_000)

        # And a publish job is waiting for the slot the calendar chose, not
        # for the moment ffmpeg happened to finish — a channel's cadence is
        # not a rendering detail.
        publishes = self.jobs("publish_upload")
        self.assertEqual(len(publishes), 1)
        self.assertEqual(publishes[0].payload["upload_id"], upload.id)
        self.assertLessEqual(
            abs((publishes[0].run_after - upload.run_at).total_seconds()), 5.0
        )

    def test_the_whole_chain_reaches_a_verified_post(self) -> None:
        transport = TikTokTransport()
        services = self.render_services(transport)
        self.enqueue("detect_clips", source_id=SOURCE, channel_id=CHANNEL)
        self.drain(services, timeout=240.0, until=lambda: bool(
            self.jobs("collect_metrics")
        ))

        upload = self.uploads()[0]
        self.assertEqual(upload.state, PostState.PUBLISHED.value)
        self.assertEqual(upload.remote_post_id, "v_9")

        verified = [j for j in self.jobs("verify_upload")
                    if j.state == "succeeded"]
        self.assertTrue(verified, "nothing verified the post")
        self.assertTrue(verified[0].result["verified"])

        # And the chain carried on into measurement without being asked.
        self.assertTrue(self.jobs("collect_metrics"))

    def test_a_post_the_platform_later_rejects_needs_attention(self) -> None:
        transport = TikTokTransport()
        transport.verify_status = "FAILED"
        transport.fail_reason = "picture_size_check_failed"
        services = self.render_services(transport)
        self.enqueue("detect_clips", source_id=SOURCE, channel_id=CHANNEL)
        self.drain(services, timeout=240.0, until=lambda: bool(
            [u for u in self.uploads()
             if u.state == PostState.NEEDS_ATTENTION.value]
        ))

        upload = self.uploads()[0]
        self.assertEqual(upload.state, PostState.NEEDS_ATTENTION.value)
        self.assertIn("picture_size_check_failed", upload.last_error)
        # Nothing is queued to measure a video that is not there.
        self.assertEqual(self.jobs("collect_metrics"), [])


# ---------------------------------------------------------------------------
# Publish and verification, without ffmpeg
# ---------------------------------------------------------------------------


class PublishHandlerTest(Fixture):
    def context(self, services, **payload):
        from clipforge.worker.types import JobContext

        return JobContext(
            job=JobRecord(id="j_pub", tenant_id=TENANT, kind="publish_upload",
                          payload=payload),
            tenant_id=TENANT, database=self.db, services=services,
        )

    def test_an_in_memory_publisher_cannot_see_the_booked_rows(self) -> None:
        """Why `publisher_factory` exists, asserted rather than assumed."""

        from clipforge.publish import PublishingSystem

        services = self.services(
            transport=TikTokTransport(), publisher_factory=None,
            publisher=PublishingSystem(),
        )
        outcome = publish_handler(self.context(services, channel_id=CHANNEL))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["published"], 0)

    def test_no_transport_is_fatal(self) -> None:
        outcome = publish_handler(
            self.context(self.services(transport=None), channel_id=CHANNEL)
        )
        self.assertEqual(outcome.disposition.value, "fatal")
        self.assertIn("transport", outcome.detail)


class VerificationHandlerTest(Fixture):
    def context(self, services, **payload):
        from clipforge.worker.types import JobContext

        return JobContext(
            job=JobRecord(id="j_ver", tenant_id=TENANT, kind="verify_upload",
                          payload=payload),
            tenant_id=TENANT, database=self.db, services=services,
        )

    def upload(self, **fields):
        from clipforge.store.records import UploadRecord

        defaults = dict(
            id="up_1", tenant_id=TENANT, channel_id=CHANNEL,
            account_id=ACCOUNT, platform=Platform.TIKTOK.value,
            state=PostState.PUBLISHED.value, remote_post_id="v_9",
            run_at=datetime.now(UTC), idempotency_key="idem-1",
        )
        defaults.update(fields)
        with self.db.unit_of_work(TENANT) as uow:
            return uow.uploads.save(UploadRecord(**defaults))

    def test_a_live_post_chains_into_measurement(self) -> None:
        self.upload()
        transport = TikTokTransport()
        outcome = verification_handler(
            self.context(self.services(transport), upload_id="up_1",
                         channel_id=CHANNEL, pass_number=1)
        )
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertTrue(outcome.result["verified"])
        kinds = sorted(s.kind for s in outcome.follow_on)
        self.assertEqual(kinds, ["collect_metrics", "verify_upload"])

    def test_the_second_pass_does_not_schedule_a_third(self) -> None:
        self.upload()
        outcome = verification_handler(
            self.context(self.services(TikTokTransport()), upload_id="up_1",
                         channel_id=CHANNEL, pass_number=2)
        )
        self.assertEqual([s.kind for s in outcome.follow_on],
                         ["collect_metrics"])

    def test_a_rejected_post_is_taken_back(self) -> None:
        self.upload()
        transport = TikTokTransport(status="FAILED",
                                    fail_reason="spam_risk_too_many_posts")
        outcome = verification_handler(
            self.context(self.services(transport), upload_id="up_1",
                         channel_id=CHANNEL)
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["rejected"])
        self.assertEqual(outcome.follow_on, ())
        self.assertEqual(
            self.uploads()[0].state, PostState.NEEDS_ATTENTION.value
        )

    def test_an_unreachable_platform_is_a_retry_not_a_rejection(self) -> None:
        """An outage must never be read as a missing post."""

        class Dead:
            def send(self, request):
                raise TimeoutError("no route to host")

        self.upload()
        outcome = verification_handler(
            self.context(self.services(Dead()), upload_id="up_1",
                         channel_id=CHANNEL)
        )
        self.assertEqual(outcome.disposition.value, "retry")
        self.assertEqual(
            self.uploads()[0].state, PostState.PUBLISHED.value,
            "an outage downgraded a live post",
        )

    def test_a_post_that_never_published_is_not_verified(self) -> None:
        self.upload(state=PostState.SCHEDULED.value, remote_post_id="")
        outcome = verification_handler(
            self.context(self.services(TikTokTransport()), upload_id="up_1",
                         channel_id=CHANNEL)
        )
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.result["verified"])


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
