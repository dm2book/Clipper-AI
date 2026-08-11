"""Rendering, by actually rendering.

Every test in `RenderEngineTest` spawns real ffmpeg and produces a real MP4,
then measures it with the container reader from `acquire.mp4`. That is the
point: the gameplay engine's filtergraph is built by string concatenation, and
the only way to know a graph is valid is to hand it to ffmpeg.

Fixtures are deliberately tiny — two seconds, 320-wide sources, a 480x854
output — because a 1080x1920 60fps encode takes about a minute and a test
suite that takes a minute per case does not get run. The composition being
exercised is identical; only the pixel count differs. One test renders at the
real 1080x1920 60fps and is marked slow.

`RenderQueueTest` injects a fake runner, because the queue, the verification
and the persistence paths have nothing to do with x264 and should not cost a
minute each to test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from clipforge.acquire.mp4 import read_mp4
from clipforge.acquire.probe import MediaProber
from clipforge.acquire.types import MediaProbe
from clipforge.gameplay import compose
from clipforge.gameplay.render import filtergraph, link_check
from clipforge.gameplay.types import (
    Box,
    FaceSample,
    Game,
    GameplayAsset,
    SpeakerTrack,
)
from clipforge.render.engine import (
    RENDER_JOB,
    RenderConfig,
    RenderEngine,
    verify_output,
)
from clipforge.render.types import (
    OutputRejected,
    RenderFailed,
    RenderRequest,
    RenderState,
)
from clipforge.store import ClipRecord, MemoryDatabase, TenantRecord

TENANT = "ten_render"
FFMPEG = os.environ.get("CLIPFORGE_FFMPEG", "") or shutil.which("ffmpeg") or ""

#: Short on purpose, but the *real* 1080x1920 60fps geometry. Shrinking the
#: canvas would desync the camera path — it crops the source, and its
#: coordinates are in source pixels — so the honest way to make these
#: affordable is fewer frames and a faster preset, not a different shape.
CLIP_S = 1.2
SPEAKER_SIZE = "1280x720"
GAMEPLAY_SIZE = "1080x1920"


def _make(argv: list[str]) -> None:
    result = subprocess.run([FFMPEG, "-y", "-loglevel", "error", *argv],
                            capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-500:])


class _Fixtures:
    """Real media, generated once for the whole module."""

    directory = ""
    speaker = ""
    gameplay = ""
    silent = ""

    @classmethod
    def build(cls) -> None:
        if cls.directory or not FFMPEG:
            return
        cls.directory = tempfile.mkdtemp(prefix="clipforge-rf-")
        cls.speaker = os.path.join(cls.directory, "speaker.mp4")
        cls.gameplay = os.path.join(cls.directory, "gameplay.mp4")
        cls.silent = os.path.join(cls.directory, "silent.mp4")
        _make(["-f", "lavfi", "-i", f"testsrc2=size={SPEAKER_SIZE}:rate=30:duration=4",
               "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-shortest", cls.speaker])
        _make(["-f", "lavfi", "-i", f"smptebars=size={GAMEPLAY_SIZE}:rate=30:duration=3",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", cls.gameplay])
        # No audio at all — the case that exits zero and produces a silent
        # video nobody notices until it is posted.
        _make(["-f", "lavfi", "-i", f"testsrc2=size={SPEAKER_SIZE}:rate=30:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", cls.silent])

    @classmethod
    def teardown(cls) -> None:
        if cls.directory:
            shutil.rmtree(cls.directory, ignore_errors=True)
            cls.directory = ""


def setUpModule() -> None:  # noqa: N802 - unittest's name
    _Fixtures.build()


def tearDownModule() -> None:  # noqa: N802
    _Fixtures.teardown()


def _track(duration_s: float = CLIP_S) -> SpeakerTrack:
    """A speaker track over the *real* fixture dimensions.

    `SpeakerTrack` defaults to 1920x1080, so composing without one against a
    1280x720 file plans a crop taller than the frame. That is a real trap and
    `RenderEngine._preflight` catches it — but a test fixture should not be
    walking into it on every case.
    """

    samples = tuple(
        FaceSample(
            t=t / 10,
            box=Box(x=520 + (t % 5) * 8, y=180, width=260, height=260),
            confidence=0.9,
        )
        for t in range(int(duration_s * 10) + 1)
    )
    return SpeakerTrack(samples=samples, source_width=1280, source_height=720,
                        detector_fps=10.0)


def _plan(duration_s: float = CLIP_S, *, with_gameplay: bool = True):
    """A real composition at the product's real output format."""

    assets = []
    if with_gameplay:
        assets = [GameplayAsset(
            asset_id="g1", game=Game.SUBWAY_SURFERS, path=_Fixtures.gameplay,
            duration_s=2.0, width=1080, height=1920, fps=60,
        )]
    return compose(duration_s, track=_track(duration_s), assets=assets,
                   game=Game.SUBWAY_SURFERS if with_gameplay else None,
                   word_count=max(2, int(duration_s * 3)),
                   speech=[(0.1, max(0.2, duration_s - 0.1))])


@unittest.skipUnless(FFMPEG, "rendering needs ffmpeg — set CLIPFORGE_FFMPEG")
class RenderEngineTest(unittest.TestCase):
    """Real encodes."""

    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="clipforge-rw-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Renderer"))
        self.engine = RenderEngine(
            self.db, TENANT,
            # ultrafast/28 for the suite. The composition, the graph and the
            # verification are identical; only the encoder's effort differs.
            config=RenderConfig(workspace=self.workspace, ffmpeg=FFMPEG,
                                timeout_s=300, preset="ultrafast", crf=30),
        )

    def _request(self, plan=None, **kwargs) -> RenderRequest:
        defaults = dict(
            render_id="rnd_1",
            plan=plan if plan is not None else _plan(),
            speaker_path=_Fixtures.speaker,
            gameplay_path=_Fixtures.gameplay,
            output_path=os.path.join(self.workspace, "clip.mp4"),
        )
        defaults.update(kwargs)
        return RenderRequest(**defaults)

    # -- the graph actually runs -------------------------------------------

    def test_a_plan_renders_to_a_real_video(self) -> None:
        """The filtergraph is built by string concatenation, and the only way
        to know one is valid is to hand it to ffmpeg."""

        result = self.engine.render(self._request())

        self.assertEqual(result.state, RenderState.READY)
        self.assertTrue(os.path.exists(result.output_path))
        self.assertGreater(result.size_bytes, 0)
        self.assertTrue(result.checksum)

        with open(result.output_path, "rb") as handle:
            info = read_mp4(handle)
        self.assertAlmostEqual(info.duration_s, CLIP_S, delta=0.25)
        self.assertEqual((info.width, info.height), (1080, 1920))
        self.assertTrue(info.has_video)
        self.assertTrue(info.has_audio)

    def test_the_speakers_audio_is_carried_into_the_output(self) -> None:
        """`-map 0:a?` is optional by design, so a broken audio chain does not
        fail the render — it produces a silent video that looks fine until it
        is on someone's feed."""

        result = self.engine.render(self._request())
        self.assertTrue(result.probe.has_audio)
        # `mp4a` from the box reader, `aac` from ffmpeg — the same codec under
        # its container four-cc and its own name.
        self.assertIn(result.probe.audio_codec, ("mp4a", "aac"))

    def test_a_speaker_only_plan_renders(self) -> None:
        """An empty gameplay library is a worse clip, not a failed render."""

        result = self.engine.render(
            self._request(plan=_plan(with_gameplay=False), gameplay_path="")
        )
        self.assertEqual(result.state, RenderState.READY)
        self.assertTrue(result.probe.has_video)

    def test_captions_are_burned_in_not_muxed(self) -> None:
        """TikTok, Reels and Shorts all play with soft subtitles off, so a
        soft track is a caption nobody sees. Burning changes the pixels, which
        is what this checks — the same frame with and without."""

        plain = self.engine.render(self._request(
            output_path=os.path.join(self.workspace, "plain.mp4")))

        ass_path = os.path.join(self.workspace, "captions.ass")
        with open(ass_path, "w", encoding="utf-8") as handle:
            handle.write(_ASS)
        captioned = self.engine.render(self._request(
            render_id="rnd_2",
            output_path=os.path.join(self.workspace, "captioned.mp4"),
            subtitles_path=ass_path))

        self.assertEqual(captioned.state, RenderState.READY)
        self.assertNotEqual(
            plain.checksum, captioned.checksum,
            "burning subtitles did not change a single pixel — the filter "
            "was almost certainly not applied",
        )
        # And the output is still a valid, correctly-shaped video.
        self.assertEqual(
            (captioned.probe.width, captioned.probe.height), (1080, 1920)
        )

    def test_the_clip_is_seeked_to_rather_than_encoded_from_zero(self) -> None:
        """Without the seek the encoder walks the whole two-hour podcast to
        reach a clip forty minutes in."""

        result = self.engine.render(self._request(start_s=1.0))
        self.assertEqual(result.state, RenderState.READY)
        self.assertAlmostEqual(result.probe.duration_s, CLIP_S, delta=0.25)

    # -- what must not be accepted -----------------------------------------

    def test_a_silent_speaker_is_rejected_rather_than_shipped(self) -> None:
        """The most likely silent failure in the whole layer: ffmpeg exits
        zero, the video is perfectly valid, and it has no sound."""

        with self.assertRaises(OutputRejected) as caught:
            self.engine.render(self._request(speaker_path=_Fixtures.silent))
        self.assertIn("audio", str(caught.exception))

    def test_a_rejected_render_leaves_no_output_behind(self) -> None:
        """A `.tmp` nobody reads, not a file the publisher uploads."""

        output = os.path.join(self.workspace, "rejected.mp4")
        with self.assertRaises(OutputRejected):
            self.engine.render(
                self._request(speaker_path=_Fixtures.silent, output_path=output)
            )
        self.assertFalse(os.path.exists(output))
        self.assertFalse(os.path.exists(f"{output}.tmp.mp4"))

    def test_a_missing_speaker_file_fails_before_ffmpeg_is_spawned(self) -> None:
        with self.assertRaises(RenderFailed):
            self.engine.render(self._request(speaker_path="/no/such/clip.mp4"))

    def test_ffmpeg_refusing_is_reported_with_its_own_diagnosis(self) -> None:
        """ffmpeg buries the reason in a wall of banner and codec statistics,
        so the tail of stderr is not reliably it."""

        broken = self._request()
        broken.gameplay_path = "/no/such/gameplay.mp4"
        with self.assertRaises(RenderFailed) as caught:
            self.engine.render(broken)
        message = str(caught.exception).lower()
        self.assertTrue(
            "no such file" in message or "error" in message, message
        )

    # -- artifacts and persistence -----------------------------------------

    def test_the_graph_and_camera_script_are_kept_beside_the_output(self) -> None:
        """The difference between "the render looks wrong" being diagnosable
        and being a shrug."""

        result = self.engine.render(self._request())
        directory = os.path.dirname(result.output_path)
        self.assertTrue(os.path.exists(os.path.join(directory, "camera.cmd")))
        self.assertTrue(os.path.exists(os.path.join(directory, "ffmpeg.args")))

    def test_a_queued_render_produces_a_video_row(self) -> None:
        with self.db.unit_of_work(TENANT) as uow:
            uow.clips.save(ClipRecord(id="cl_1", tenant_id=TENANT,
                                      channel_id="ch_1", duration_s=CLIP_S))

        self.engine.enqueue("cl_1", _plan(), _Fixtures.speaker,
                            gameplay_path=_Fixtures.gameplay)
        results = self.engine.run(limit=1)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok, results[0].error)
        with self.db.unit_of_work(TENANT) as uow:
            videos = uow.videos.all()
        self.assertEqual(len(videos), 1)
        video = videos[0]
        self.assertEqual(video.state, "ready")
        self.assertEqual(video.clip_id, "cl_1")
        self.assertEqual((video.width, video.height), (1080, 1920))
        self.assertTrue(video.checksum)
        self.assertGreater(video.size_bytes, 0)
        # The plan is kept with the asset, so a bad output can be traced to
        # the composition that produced it.
        self.assertIn("panels", video.render_plan)

    @unittest.skipUnless(
        os.environ.get("CLIPFORGE_SLOW_TESTS"),
        "set CLIPFORGE_SLOW_TESTS=1 to render at the product's own quality",
    )
    def test_a_production_quality_encode_renders(self) -> None:
        """The same composition at `medium`/CRF 18 — what actually ships.
        Slow, and skipped by default; the rest of the suite proves the graph
        and the geometry, this proves the production encoder settings."""

        self.engine.config.preset = ""
        self.engine.config.crf = 0

        plan = _plan(duration_s=3.0)
        result = self.engine.render(self._request(
            plan=plan, output_path=os.path.join(self.workspace, "full.mp4")))

        self.assertTrue(result.ok)
        self.assertEqual((result.probe.width, result.probe.height), (1080, 1920))
        self.assertAlmostEqual(result.probe.fps, 60.0, delta=0.5)
        self.assertTrue(result.probe.has_audio)
        self.assertGreater(result.realtime_ratio, 0)


class VerificationTest(unittest.TestCase):
    """The check that decides whether an encode counts. No ffmpeg needed."""

    def setUp(self) -> None:
        self.plan = _plan.__wrapped__() if hasattr(_plan, "__wrapped__") else None

    def _plan_stub(self, **kwargs):
        from types import SimpleNamespace

        defaults = dict(width=1080, height=1920, fps=60, duration_s=30.0)
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def _probe(self, **kwargs) -> MediaProbe:
        defaults = dict(duration_s=30.0, width=1080, height=1920, fps=60.0,
                        has_audio=True, has_video=True)
        defaults.update(kwargs)
        return MediaProbe(**defaults)

    def test_a_correct_render_has_no_problems(self) -> None:
        self.assertEqual(verify_output(self._probe(), self._plan_stub()), [])

    def test_a_frame_of_drift_is_tolerated(self) -> None:
        """Encoders land either side of the requested length. Failing over
        16ms would fail most correct renders."""

        self.assertEqual(
            verify_output(self._probe(duration_s=30.02), self._plan_stub()), []
        )

    def test_a_truncated_encode_is_caught(self) -> None:
        problems = verify_output(self._probe(duration_s=12.0), self._plan_stub())
        self.assertTrue(any("duration" in p for p in problems), problems)

    def test_the_wrong_geometry_is_caught(self) -> None:
        """A `scale` that rounded to an odd number, or a panel that overflowed.
        Both produce a valid file of the wrong shape."""

        problems = verify_output(
            self._probe(width=1080, height=1922), self._plan_stub()
        )
        self.assertTrue(any("1922" in p for p in problems), problems)

    def test_the_wrong_frame_rate_is_caught(self) -> None:
        problems = verify_output(self._probe(fps=30.0), self._plan_stub())
        self.assertTrue(any("fps" in p for p in problems), problems)

    def test_a_silent_output_is_caught(self) -> None:
        problems = verify_output(self._probe(has_audio=False), self._plan_stub())
        self.assertTrue(any("audio" in p for p in problems), problems)

    def test_an_unmeasurable_output_is_caught(self) -> None:
        problems = verify_output(self._probe(duration_s=None), self._plan_stub())
        self.assertTrue(any("duration" in p for p in problems), problems)


@unittest.skipUnless(FFMPEG, "the queue tests still need one real media file")
class RenderQueueTest(unittest.TestCase):
    """The queue, retries and failure paths — with a fake ffmpeg.

    None of this has anything to do with x264, and spending a minute of encode
    per case would mean these tests do not get run.
    """

    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="clipforge-rq-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Renderer"))
        self.calls: list[list[str]] = []
        # Preflight probes the speaker before spawning anything, so these
        # cases need a real file even though ffmpeg itself is faked.
        self.speaker = _Fixtures.speaker or os.path.join(self.workspace, "x.mp4")

    def _engine(self, *, returncode: int = 1, stderr: str = "boom") -> RenderEngine:
        def runner(argv, timeout_s):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, returncode, "", stderr)

        return RenderEngine(
            self.db, TENANT,
            config=RenderConfig(workspace=self.workspace, ffmpeg="/bin/true"),
            runner=runner,
        )

    def test_a_failing_render_goes_back_on_the_queue(self) -> None:
        engine = self._engine(stderr="Error: invalid argument")
        engine.enqueue("cl_1", _plan_stubbed(), self.speaker)
        results = engine.run(limit=1)

        self.assertFalse(results[0].ok)
        with self.db.unit_of_work(TENANT) as uow:
            job = uow.jobs.all()[0]
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.attempts, 1)
        self.assertIn("invalid", job.last_error)

    def test_a_render_dies_after_its_attempts_are_spent(self) -> None:
        engine = self._engine()
        engine.enqueue("cl_1", _plan_stubbed(), self.speaker)
        for _ in range(RenderConfig().max_attempts + 1):
            engine.run(limit=1)
            with self.db.unit_of_work(TENANT) as uow:
                job = uow.jobs.all()[0]
                if job.state == "dead":
                    break
                # Bring the retry forward rather than waiting out the backoff.
                job.run_after = engine.clock()
                job.state = "queued"
                uow.jobs.save(job)
        self.assertEqual(job.state, "dead")

    def test_the_same_clip_queued_twice_is_one_render(self) -> None:
        engine = self._engine()
        first = engine.enqueue("cl_1", _plan_stubbed(), self.speaker)
        again = engine.enqueue("cl_1", _plan_stubbed(), self.speaker)
        self.assertEqual(first, again)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.jobs.count(), 1)

    def test_a_plan_that_cannot_be_rebuilt_fails_loudly_and_permanently(self) -> None:
        """A worker in another process has only the serialised plan. Refusing
        beats rendering a composition reconstructed wrongly — and retrying
        would refuse identically five more times."""

        engine = self._engine()
        engine.enqueue("cl_1", _plan_stubbed(), self.speaker)
        engine._plans.clear()  # as a fresh worker would find it

        results = engine.run(limit=1)
        self.assertFalse(results[0].ok)
        self.assertIn("plan_loader", results[0].error)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.jobs.all()[0].state, "dead")

    def test_a_plan_loader_lets_another_process_pick_the_render_up(self) -> None:
        engine = self._engine()
        engine.enqueue("cl_1", _plan_stubbed(), self.speaker)
        engine._plans.clear()
        engine._plan_loader = lambda payload: _plan_stubbed()

        results = engine.run(limit=1)
        # It got as far as ffmpeg, which is what the loader is for.
        self.assertTrue(self.calls, "the render never reached ffmpeg")
        self.assertFalse(results[0].ok)  # the fake ffmpeg fails on purpose

    def test_the_worker_cap_bounds_a_batch(self) -> None:
        """x264 is already threaded: four renders on four cores is slower than
        four queued behind each other."""

        engine = RenderEngine(
            self.db, TENANT,
            config=RenderConfig(workspace=self.workspace, ffmpeg="/bin/true",
                                workers=2),
            runner=lambda argv, t: subprocess.CompletedProcess(argv, 1, "", "x"),
        )
        for index in range(5):
            engine.enqueue(f"cl_{index}", _plan_stubbed(), self.speaker)
        self.assertEqual(len(engine.run(limit=5)), 2)


def _plan_stubbed():
    """A plan object that needs no gameplay asset. Composed against the same
    source size as the fixtures, so preflight passes and the fake ffmpeg is
    what decides the outcome."""

    return compose(1.2, track=_track(1.2), assets=(), word_count=4,
                   speech=[(0.1, 1.1)])


_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Alignment, MarginV
Style: Default,Arial,48,&H00FFFFFF,-1,2,120

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:00.10,0:00:01.90,Default,THE ONE LINE THAT ENDED IT
"""


if __name__ == "__main__":
    unittest.main()
